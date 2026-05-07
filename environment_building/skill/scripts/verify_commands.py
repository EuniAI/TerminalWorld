#!/usr/bin/env python3
"""Batch-verify commands against a Docker image or Compose stack.

Runs each command inside the given Docker image (or Compose app service)
through a single persistent shell session, collects structured results
(passed / failed / timed_out), and outputs JSON.

Supports two modes:
  - **Image mode** (default): starts a container with
    `docker run -d <image> sleep infinity`, then opens a persistent
    `docker exec -i <container> sh` session for all commands.
  - **Compose mode** (`--project-dir`): starts the full stack with
    `docker compose up -d`, opens a persistent
    `docker compose exec -i -T app sh` session, then tears down with
    `docker compose down`.

The persistent shell preserves shell state (cd, export, source, etc.)
across commands, enabling verification of stateful command sequences.

Also performs non-blocking authenticity evidence checks (warn mode):
- binary_path (hard, counted in authenticity rate)
- python_import_origin for parseable `python -c "import ..."` commands
  (hard, counted in authenticity rate)
- package_manager_evidence for resolved binary path ownership
  (soft warning, not counted in authenticity rate)

python3 scripts/verify_commands.py \
    --image-tag terminalworld-env-1060 \
    --project-dir /path/to/output \
    --commands-file /path/to/verification_commands.json \
    --output-file /path/to/verification_results.json \
    --timeout 300

The commands file must contain a flat JSON array of command strings to execute. For example:
[
  "python3 --version",
  "node -v",
  "mysql --version"
]

Output JSON schema:
{
  "metadata": {
    "image_tag": "terminalworld-env-1060",
    "compose_mode": false
  },
  "metrics": {
    "commands": {
      "total": 12,
      "passed": 8,
      "failed": 2,
      "timed_out": 1,
      "success_rate": 0.75
    },
    "authenticity": {
      "dependency_rate": 0.9,
      "summary": { ... }
    }
  },
  "details": {
    "commands": {
      "all_results": [ ... ],
      "failed": [ ... ],
      "timed_out": [ ... ]
    },
    "authenticity": {
      "all_checks": [ ... ],
      "failures": [ ... ]
    }
  }
}
"""

from __future__ import annotations

import ast
import argparse
import json
import os
import queue
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

_AUTH_MODE = "warn"
_MAX_AUTH_TARGETS = 30
_BANNED_PREFIXES = ("/opt/mock", "/mock", "/tmp/mock", "/app/mock")
_WRAPPER_WORDS = {"sudo", "env", "nohup"}
_REPLAYABILITY_THRESHOLD = 0.6
_METADATA_FILE = "build_metadata.json"
_MATURITY_LEVEL_KEY = "maturity_level"
_IGNORED_COMMANDS = {
    "cd", "echo", "export", "alias", "exit", "pwd",
    "true", "false", "test", "[", "]", "set", "unset",
    "umask", "shift", "trap", "return", "readonly",
}
_PYTHON_NAME_RE = re.compile(r"^python(?:\d+(?:\.\d+)?)?$")

_COMPOSE_HEALTH_TIMEOUT = 900  # extended: OKD v3.11 DinD startup (docker load + write-config + master) ~10min
_QUEUE_MAXSIZE = 10000


def _check_launchability(shell: "PersistentShell") -> bool:
    result = shell.run_command("echo 'launchability_ok'", timeout=10)
    return result.get("status") == "passed"


def _derive_maturity_level(
    target_success_rate: float | None,
    diagnostic_success_rate: float | None,
    launchability_passed: bool
) -> str:
    if not launchability_passed:
        return "Buildability"

    # Testability is a prerequisite. If diagnostic probes exist but fail,
    # the environment is broken. It cannot progress beyond Launchability.
    if diagnostic_success_rate is not None and diagnostic_success_rate < 1.0:
        return "Launchability"

    # If probes pass (or don't exist), but no business logic commands pass
    if target_success_rate is None or target_success_rate == 0:
        return "Testability"

    if target_success_rate < _REPLAYABILITY_THRESHOLD:
        return "Runnability"
    if target_success_rate < 1.0:
        return "Replayability"
    return "Reproducibility"


def _update_build_metadata(project_dir: str | None, maturity_level: str) -> None:
    if not project_dir:
        return

    metadata_path = Path(project_dir) / _METADATA_FILE
    metadata: dict = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            metadata = {}

    metadata[_MATURITY_LEVEL_KEY] = maturity_level
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# =========================================================================
# Persistent Shell
# =========================================================================

class PersistentShell:
    """A persistent shell session inside a running Docker container.

    Opens a single long-lived `sh` process via `docker exec -i` (or
    `docker compose exec -i -T`). Commands are sent via stdin; output is
    read via a background reader thread that feeds a bounded queue.

    Sentinel lines injected around each command provide clean output
    boundaries and exit-code capture without requiring any PTY or shell
    configuration in the container.
    """

    def __init__(
        self,
        docker_cmd: list[str],
        *,
        cwd: str | None = None,
        env: dict | None = None,
    ) -> None:
        self._docker_cmd = docker_cmd
        self._cwd = cwd
        self._env = env
        # Per-session nonce makes sentinels unique and collision-resistant.
        nonce = secrets.token_hex(4)
        self._sentinel_start = f"___SENTINEL_START_{nonce}___"
        self._sentinel_end = f"___SENTINEL_END_{nonce}___"
        self._spawn()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _spawn(self) -> None:
        """Start (or respawn after timeout) the shell process."""
        self.proc = subprocess.Popen(
            self._docker_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr so we capture all output
            text=True,
            bufsize=1,
            cwd=self._cwd,
            env=self._env,
        )
        self._q: queue.Queue[str | None] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        """Background thread: pump stdout lines into the bounded queue.

        Capture self._q at thread-start time so that a concurrent respawn()
        replacing self._q with a new Queue does not cause this thread to
        signal EOF into the wrong (new) queue.
        """
        assert self.proc.stdout is not None
        q = self._q  # capture once; respawn() may replace self._q later
        for line in iter(self.proc.stdout.readline, ""):
            try:
                q.put(line, timeout=1)
            except queue.Full:
                pass  # drain but discard to prevent OOM
        q.put(None)  # signal EOF to main thread

    def respawn(self) -> None:
        """Kill the current shell and start a fresh one.

        Called after a timeout. The underlying container stays alive;
        only the `docker exec` shell process is replaced.
        """
        try:
            self.proc.kill()
            self.proc.wait(timeout=10)
        except Exception:
            pass
        self._spawn()

    def close(self) -> None:
        """Gracefully close stdin and wait for the shell to exit."""
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def run_command(self, command: str, timeout: int) -> dict:
        """Execute one command in the persistent shell and return a result dict."""
        start = time.monotonic()

        # Build the sentinel-wrapped payload. The exit code capture and
        # end-sentinel are on a single line joined by `;` -- this is
        # critical to prevent backslash continuations or unclosed heredocs
        # in the user command from swallowing the sentinel line.
        payload = (
            f"echo '{self._sentinel_start}'\n"
            f"{command}\n"
            f"___ecode=$? ; echo '' ; echo '{self._sentinel_end}'\"${{___ecode}}\"\n"
        )

        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()
        except BrokenPipeError:
            duration = time.monotonic() - start
            return {
                "command": command,
                "status": "failed",
                "exit_code": -1,
                "output": "Shell stdin broken (container may have died)",
                "duration_s": round(duration, 1),
            }

        # Collect output lines between sentinels.
        output_lines: list[str] = []
        output_chars = 0
        truncated = False
        found_start = False
        exit_code = -1
        deadline = time.monotonic() + timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Timeout: kill and respawn, return timed_out result.
                partial = "\n".join(output_lines)[:500]
                self.respawn()
                duration = time.monotonic() - start
                return {
                    "command": command,
                    "status": "timed_out",
                    "exit_code": 124,
                    "output": partial,
                    "duration_s": round(duration, 1),
                }

            try:
                line = self._q.get(timeout=min(remaining, 2.0))
            except queue.Empty:
                continue

            if line is None:
                # EOF: shell exited unexpectedly.
                self.respawn()
                duration = time.monotonic() - start
                output = "\n".join(output_lines).strip()
                if len(output) > 2000:
                    output = output[:1000] + "\n... (truncated) ...\n" + output[-1000:]
                return {
                    "command": command,
                    "status": "failed",
                    "exit_code": -1,
                    "output": output or "Shell exited unexpectedly",
                    "duration_s": round(duration, 1),
                }

            stripped = line.rstrip("\n").rstrip("\r")

            if not found_start:
                if stripped == self._sentinel_start:
                    found_start = True
                continue

            # Check for end sentinel.
            if stripped.startswith(self._sentinel_end):
                code_str = stripped[len(self._sentinel_end):]
                try:
                    exit_code = int(code_str)
                except ValueError:
                    exit_code = -1
                break

            # Accumulate output with a character cap.
            if not truncated:
                output_chars += len(stripped) + 1
                if output_chars > 2000:
                    truncated = True
                else:
                    output_lines.append(stripped)

        output = "\n".join(output_lines).strip()
        if truncated:
            output = output[:1000] + "\n... (truncated) ...\n" + output[-1000:]

        duration = time.monotonic() - start

        if exit_code == 0:
            status = "passed"
        else:
            status = "failed"

        return {
            "command": command,
            "status": status,
            "exit_code": exit_code,
            "output": output,
            "duration_s": round(duration, 1),
        }


# =========================================================================
# Stateless command execution (for authenticity probes)
# =========================================================================

def _run_command_stateless(
    container_name_or_none: str | None,
    project_dir: str | None,
    image_tag: str,
    command: str,
    timeout: int,
) -> dict:
    """Run a single command via a fresh `docker exec sh -c` call.

    Used exclusively for authenticity probe commands, which are stateless
    by nature and do not need to share shell state.
    """
    start = time.monotonic()

    if project_dir is not None:
        env = os.environ.copy()
        env["IMAGE_TAG"] = image_tag
        env["COMPOSE_PROJECT_NAME"] = image_tag
        cmd = [
            "docker", "compose", "exec", "-T",
            "-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8",
            "app", "sh", "-c", command,
        ]
        kwargs: dict = {"cwd": project_dir, "env": env}
    else:
        assert container_name_or_none is not None
        cmd = [
            "docker", "exec",
            "-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8",
            container_name_or_none, "sh", "-c", command,
        ]
        kwargs = {}

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            **kwargs,
        )
        duration = time.monotonic() - start
        output = (result.stdout + result.stderr).strip()
        if len(output) > 2000:
            output = output[:1000] + "\n... (truncated) ...\n" + output[-1000:]
        return {
            "command": command,
            "status": "passed" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "output": output,
            "duration_s": round(duration, 1),
        }
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return {
            "command": command,
            "status": "timed_out",
            "exit_code": 124,
            "output": "",
            "duration_s": round(duration, 1),
        }
    except Exception as e:
        duration = time.monotonic() - start
        return {
            "command": command,
            "status": "failed",
            "exit_code": -1,
            "output": str(e),
            "duration_s": round(duration, 1),
        }


# =========================================================================
# Image container lifecycle helpers
# =========================================================================

def _container_name(image_tag: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", image_tag)
    return f"verify-{safe}"


def _container_start(image_tag: str) -> tuple[bool, str]:
    """Start a persistent container for image mode verification."""
    name = _container_name(image_tag)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    time.sleep(1)  # let systemd deregister old cgroup scope (avoids E2BIG on cgroupv2+systemd hosts)
    result = subprocess.run(
        ["docker", "run", "-d", "--name", name,
         "--cgroupns=host",  # required on cgroup-v2+systemd hosts
         "-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8",
         image_tag, "sleep", "infinity"],
        capture_output=True, text=True,
    )
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def _container_stop(image_tag: str) -> None:
    """Remove the persistent verification container."""
    try:
        subprocess.run(
            ["docker", "rm", "-f", _container_name(image_tag)],
            capture_output=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


# =========================================================================
# Compose lifecycle helpers
# =========================================================================

def _compose_up(project_dir: str, image_tag: str, timeout: int = 120) -> tuple[bool, str]:
    """Start compose stack and wait for services to become healthy."""
    env = os.environ.copy()
    env["IMAGE_TAG"] = image_tag
    env["COMPOSE_PROJECT_NAME"] = image_tag

    try:
        result = subprocess.run(
            ["docker", "compose", "up", "-d", "--wait",
             "--wait-timeout", str(timeout)],
            capture_output=True, text=True,
            timeout=timeout + 30,
            cwd=project_dir,
            env=env,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"docker compose up timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


def _compose_down(project_dir: str, image_tag: str) -> None:
    """Tear down the compose stack."""
    env = os.environ.copy()
    env["IMAGE_TAG"] = image_tag
    env["COMPOSE_PROJECT_NAME"] = image_tag

    try:
        subprocess.run(
            ["docker", "compose", "down"],
            capture_output=True, text=True, timeout=60,
            cwd=project_dir,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


# =========================================================================
# Command analysis helpers
# =========================================================================

def _is_env_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", token))


def _normalize_tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.strip().split()

    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in _WRAPPER_WORDS or _is_env_assignment(tok):
            idx += 1
            continue
        break
    return tokens[idx:]


def _extract_primary_executable(command: str) -> str:
    tokens = _normalize_tokens(command)
    if not tokens:
        return ""
    head = tokens[0]
    if head in _IGNORED_COMMANDS:
        return ""
    return head


def _is_banned_path(path: str) -> bool:
    path = path.strip()
    if not path:
        return False
    for prefix in _BANNED_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _run_probe(
    run_fn: Callable[[str, int], dict],
    command: str,
    timeout: int = 20,
) -> tuple[int, str]:
    """Run a short probe command; return (exit_code, output)."""
    result = run_fn(command, timeout)
    code = result.get("exit_code")
    return int(code) if isinstance(code, int) else -1, str(result.get("output", ""))


def _extract_python_import_targets(command: str) -> list[tuple[str, str]]:
    """Extract (python_cmd, module) pairs from parseable `python -c` commands."""
    tokens = _normalize_tokens(command)
    if not tokens:
        return []

    py_index = -1
    py_cmd = ""
    for i, tok in enumerate(tokens):
        name = Path(tok).name
        if _PYTHON_NAME_RE.match(name):
            py_index = i
            py_cmd = tok
            break
    if py_index < 0:
        return []

    rest = tokens[py_index + 1:]
    if "-c" not in rest:
        return []
    c_index = rest.index("-c")
    if c_index + 1 >= len(rest):
        return []
    code = rest[c_index + 1]

    modules: dict[str, bool] = {}
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top:
                        modules[top] = True
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                if top:
                    modules[top] = True
    except SyntaxError:
        for m in re.finditer(r"\bimport\s+([A-Za-z_][A-Za-z0-9_\.]*)", code):
            modules[m.group(1).split(".")[0]] = True

    return [(py_cmd, module) for module in modules.keys()]


def _package_manager_evidence(
    run_fn: Callable[[str, int], dict],
    file_path: str,
    timeout: int = 20,
) -> tuple[bool, str]:
    quoted = shlex.quote(file_path)
    probes = [
        ("dpkg -S", f"dpkg -S {quoted} 2>/dev/null | head -n 1"),
        ("rpm -qf", f"rpm -qf {quoted} 2>/dev/null | head -n 1"),
        ("apk info -W", f"apk info -W {quoted} 2>/dev/null | head -n 1"),
        ("pacman -Qo", f"pacman -Qo {quoted} 2>/dev/null | head -n 1"),
        ("zypper wp", f"zypper -q wp {quoted} 2>/dev/null | head -n 2"),
    ]

    for label, cmd in probes:
        code, out = _run_probe(run_fn, cmd, timeout)
        out = out.strip()
        if code == 0 and out:
            return True, f"{label}: {out}"

    return False, f"No package-manager ownership evidence for {file_path}"


def _build_authenticity_checks(
    run_fn: Callable[[str, int], dict],
    results: list[dict],
) -> dict:
    """Generate authenticity checks from commands that were actually executed."""
    runnable_status = {"passed", "failed", "timed_out"}

    executed_cmds: list[str] = []
    for result in results:
        if result.get("status") in runnable_status:
            cmd = str(result.get("command", "")).strip()
            if cmd:
                executed_cmds.append(cmd)

    binaries: list[str] = []
    seen_bins: set[str] = set()
    for cmd in executed_cmds:
        binary = _extract_primary_executable(cmd)
        if not binary or binary in seen_bins:
            continue
        seen_bins.add(binary)
        binaries.append(binary)
        if len(binaries) >= _MAX_AUTH_TARGETS:
            break

    py_targets: list[tuple[str, str]] = []
    seen_py: set[tuple[str, str]] = set()
    for cmd in executed_cmds:
        for py_cmd, module in _extract_python_import_targets(cmd):
            key = (py_cmd, module)
            if key in seen_py:
                continue
            seen_py.add(key)
            py_targets.append(key)

    checks: list[dict] = []

    for binary in binaries:
        quoted_bin = shlex.quote(binary)
        code, out = _run_probe(
            run_fn,
            f"command -v {quoted_bin} 2>/dev/null || true",
            timeout=20,
        )
        path = out.strip().splitlines()[0].strip() if out.strip() else ""

        if code == 124:
            checks.append({
                "kind": "binary_path",
                "target": binary,
                "passed": False,
                "evidence": "Probe timed out while resolving binary path",
                "count_in_rate": True,
            })
            checks.append({
                "kind": "package_manager_evidence",
                "target": binary,
                "passed": False,
                "evidence": "Skipped due to timeout in binary path probe",
                "count_in_rate": False,
            })
            continue

        if not path:
            checks.append({
                "kind": "binary_path",
                "target": binary,
                "passed": False,
                "evidence": "Binary not found in PATH",
                "count_in_rate": True,
            })
            checks.append({
                "kind": "package_manager_evidence",
                "target": binary,
                "passed": False,
                "evidence": "Binary missing; package evidence unavailable",
                "count_in_rate": False,
            })
            continue

        if _is_banned_path(path):
            checks.append({
                "kind": "binary_path",
                "target": binary,
                "passed": False,
                "evidence": f"Binary resolved to banned path: {path}",
                "count_in_rate": True,
            })
        else:
            checks.append({
                "kind": "binary_path",
                "target": binary,
                "passed": True,
                "evidence": f"path={path}",
                "count_in_rate": True,
            })

        evidence_passed, evidence = _package_manager_evidence(run_fn, path, timeout=20)
        checks.append({
            "kind": "package_manager_evidence",
            "target": binary,
            "passed": evidence_passed,
            "evidence": evidence,
            "count_in_rate": False,
        })

    for py_cmd, module in py_targets:
        py_q = shlex.quote(py_cmd)
        module_q = shlex.quote(module)
        code, out = _run_probe(
            run_fn,
            (
                f"{py_q} -c \"import importlib; "
                f"m=importlib.import_module({module_q!r}); "
                "print(getattr(m, '__file__', ''))\""
            ),
            timeout=20,
        )
        module_path = out.strip().splitlines()[-1].strip() if out.strip() else ""

        if code != 0:
            checks.append({
                "kind": "python_import_origin",
                "target": module,
                "passed": False,
                "evidence": f"Import failed via {py_cmd}: {out.strip()[:300]}",
                "count_in_rate": True,
            })
            continue

        if _is_banned_path(module_path):
            checks.append({
                "kind": "python_import_origin",
                "target": module,
                "passed": False,
                "evidence": f"Module loaded from banned path: {module_path}",
                "count_in_rate": True,
            })
        else:
            checks.append({
                "kind": "python_import_origin",
                "target": module,
                "passed": True,
                "evidence": f"python={py_cmd} file={module_path or '<builtin/namespace>'}",
                "count_in_rate": True,
            })

    hard_checks = [item for item in checks if item["count_in_rate"]]
    hard_total = len(hard_checks)
    hard_passed = sum(1 for item in hard_checks if item["passed"])
    hard_failed = hard_total - hard_passed
    warning_total = sum(
        1 for item in checks if (not item["count_in_rate"]) and (not item["passed"])
    )

    return {
        "authenticity_checks": checks,
        "dependency_authenticity_rate": (
            round(hard_passed / hard_total, 4) if hard_total else None
        ),
        "authenticity_summary": {
            "hard_total": hard_total,
            "hard_passed": hard_passed,
            "hard_failed": hard_failed,
            "warning_total": warning_total,
            "mode": _AUTH_MODE,
        },
        "authenticity_failures": [item for item in checks if not item["passed"]],
    }


def _run_all_commands(
    shell: PersistentShell,
    raw_commands: list[str],
    timeout: int,
    max_commands: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Execute commands through the persistent shell session.

    Note: deduplication is intentionally absent -- stateful sequences
    may legitimately repeat the same command (e.g., `cd /app` appearing
    at two different points in a workflow).
    """
    results: list[dict] = []
    verification_timeouts: list[dict] = []
    failed_commands: list[dict] = []

    count = 0
    for cmd in raw_commands:
        if count >= max_commands:
            break

        if not isinstance(cmd, str):
            continue

        cmd = cmd.strip()
        if not cmd:
            continue

        result = shell.run_command(cmd, timeout)
        results.append(result)
        count += 1

        if result["status"] == "timed_out":
            verification_timeouts.append({
                "command": cmd,
                "partial_output": result["output"][:500] if result["output"] else "",
            })
        elif result["status"] == "failed":
            failed_commands.append({
                "command": cmd,
                "exit_code": result["exit_code"],
                "output": result["output"][:500] if result["output"] else "",
            })

    return results, verification_timeouts, failed_commands


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify commands against a Docker image or Compose stack.",
    )
    parser.add_argument("--image-tag", required=True,
                        help="Docker image tag (used as image name and compose project name)")
    parser.add_argument("--project-dir", default="",
                        help="Directory containing docker-compose.yaml (compose mode)")
    parser.add_argument("--diagnostic-commands-file", default="",
                        help="JSON file with a flat array of diagnostic command strings")
    parser.add_argument("--target-commands-file", default="",
                        help="JSON file with a flat array of target command strings")
    parser.add_argument("--output-file", default="",
                        help="Write results JSON here (default: stdout)")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Timeout per command in seconds (default: 300)")
    parser.add_argument("--max-commands", type=int, default=30,
                        help="Max commands to verify (default: 30)")
    args = parser.parse_args()

    # Determine mode: compose mode if project-dir is given and a compose file exists.
    project_dir = Path(args.project_dir) if args.project_dir else None
    compose_mode = project_dir is not None and any(
        (project_dir / n).exists()
        for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
    )
    project_dir_str = str(project_dir) if project_dir else None

    _SHELL_PREAMBLE = {"set -e", "set -x", "set -ex", "set -xe", "set -euo pipefail", "set -eo pipefail"}

    def load_commands(file_path: str) -> list[str]:
        if not file_path:
            return []
        path = Path(file_path)
        if not path.exists():
            return []
        if path.suffix == ".sh":
            cmds = []
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#!") or stripped.startswith("#"):
                    continue
                if stripped in _SHELL_PREAMBLE:
                    continue
                cmds.append(stripped)
            return cmds
        with open(path, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as exc:
                print(json.dumps({"error": f"Invalid JSON in commands file {file_path}: {exc}"}))
                sys.exit(1)
        if not isinstance(data, list):
            print(json.dumps({"error": f"Commands file {file_path} must contain a top-level JSON array of strings"}))
            sys.exit(1)
        return data

    diagnostic_commands = load_commands(args.diagnostic_commands_file)
    target_commands = load_commands(args.target_commands_file)

    # ----------------------------------------------------------------
    # Start container / compose stack and open a persistent shell.
    # ----------------------------------------------------------------
    shell: PersistentShell | None = None

    if compose_mode:
        compose_env = os.environ.copy()
        compose_env["IMAGE_TAG"] = args.image_tag
        compose_env["COMPOSE_PROJECT_NAME"] = args.image_tag

        ok, up_output = _compose_up(project_dir_str, args.image_tag, _COMPOSE_HEALTH_TIMEOUT)
        if not ok:
            _compose_down(project_dir_str, args.image_tag)
            print(json.dumps({
                "error": "docker compose up failed",
                "detail": up_output[:2000],
                "compose_mode": True,
            }))
            sys.exit(1)

        shell = PersistentShell(
            ["docker", "compose", "exec", "-i", "-T",
             "-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8",
             "app", "sh"],
            cwd=project_dir_str,
            env=compose_env,
        )

        # Stateless run_fn for authenticity probes (independent docker exec).
        def auth_run_fn(command: str, timeout: int) -> dict:
            return _run_command_stateless(
                None, project_dir_str, args.image_tag, command, timeout,
            )

        identifier = args.image_tag

    else:
        ok, start_output = _container_start(args.image_tag)
        if not ok:
            _container_stop(args.image_tag)
            print(json.dumps({
                "error": "docker run failed to start verify container",
                "detail": start_output[:2000],
            }))
            sys.exit(1)

        container_name = _container_name(args.image_tag)
        shell = PersistentShell(
            ["docker", "exec", "-i",
             "-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8",
             container_name, "sh"],
        )

        # Stateless run_fn for authenticity probes.
        def auth_run_fn(command: str, timeout: int) -> dict:
            return _run_command_stateless(
                container_name, None, args.image_tag, command, timeout,
            )

        identifier = args.image_tag

    try:
        launchability_passed = _check_launchability(shell)

        # Phase 1: Diagnostic
        diag_results, diag_timeouts, diag_failed = _run_all_commands(
            shell, diagnostic_commands, args.timeout, args.max_commands,
        )

        # Reset shell state
        shell.respawn()

        # Phase 2: Target
        target_results, target_timeouts, target_failed = _run_all_commands(
            shell, target_commands, args.timeout, args.max_commands,
        )

        def _compute_metrics(results):
            counts = {
                "passed": sum(1 for r in results if r["status"] == "passed"),
                "failed": sum(1 for r in results if r["status"] == "failed"),
                "timed_out": sum(1 for r in results if r["status"] == "timed_out"),
            }
            runnable_total = sum(
                1 for r in results
                if r["status"] in {"passed", "failed", "timed_out"}
            )
            success_rate = (
                round(counts["passed"] / runnable_total, 4)
                if runnable_total
                else None
            )
            return counts, success_rate

        diag_counts, diag_success_rate = _compute_metrics(diag_results)
        target_counts, target_success_rate = _compute_metrics(target_results)

        maturity_level = _derive_maturity_level(
            target_success_rate, diag_success_rate, launchability_passed
        )

        authenticity = _build_authenticity_checks(auth_run_fn, diag_results + target_results)

        output = {
            "metadata": {
                "image_tag": identifier,
                "compose_mode": compose_mode,
            },
            "metrics": {
                "maturity_level": maturity_level,
                "launchability_passed": launchability_passed,
                "diagnostic_commands": {
                    "total": len(diag_results),
                    **diag_counts,
                    "success_rate": diag_success_rate,
                },
                "target_commands": {
                    "total": len(target_results),
                    **target_counts,
                    "success_rate": target_success_rate,
                },
                "authenticity": {
                    "dependency_rate": authenticity["dependency_authenticity_rate"],
                    "summary": authenticity["authenticity_summary"],
                }
            },
            "details": {
                "diagnostic_commands": {
                    "all_results": diag_results,
                    "failed": diag_failed,
                    "timed_out": diag_timeouts,
                },
                "target_commands": {
                    "all_results": target_results,
                    "failed": target_failed,
                    "timed_out": target_timeouts,
                },
                "authenticity": {
                    "all_checks": authenticity["authenticity_checks"],
                    "failures": authenticity["authenticity_failures"],
                }
            }
        }

        output_json = json.dumps(output, ensure_ascii=False, indent=2)

        if args.output_file:
            Path(args.output_file).write_text(output_json, encoding="utf-8")
            _update_build_metadata(project_dir_str, maturity_level)
            print(f"Results written to {args.output_file}")
            print(f"Diagnostic Summary: {diag_counts}")
            print(f"Target Summary: {target_counts}")
        else:
            _update_build_metadata(project_dir_str, maturity_level)
            print(output_json)

    finally:
        if shell is not None:
            shell.close()
        if compose_mode:
            _compose_down(project_dir_str, args.image_tag)
        else:
            _container_stop(args.image_tag)


if __name__ == "__main__":
    main()
