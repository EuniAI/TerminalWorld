#!/usr/bin/env python3
"""
Recording analyzer: loads files from a recording directory and extracts
structured EnvironmentSignals for environment reconstruction.

Input:  A recording directory (e.g. data/recordings/1060/)
        containing info.json, commands.json, recording.txt, category.json.

Output: An EnvironmentSignals dataclass with all extracted environment hints.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# =============================================================================
# EnvironmentSignals dataclass
# =============================================================================


@dataclass
class EnvironmentSignals:
    """Structured environment signals extracted from a terminal recording."""

    # --- From info.json ---
    recording_id: str = ""
    environment: str = ""  # raw "OS • terminal • shell" string, e.g. "GNU/Linux • xterm • zsh"
    title: str = ""
    description: str = ""

    # --- External ---
    external_urls: list[dict[str, Any]] = field(default_factory=list)  # structured external URLs from analysis.json

    # --- Category info (from category.json / classify_tasks.py) ---
    operation_types: dict = field(default_factory=dict)
    task_purposes: dict = field(default_factory=dict)

    # --- From analysis.json ---
    tui_status: str = ""  # "CLI_ONLY" or "CONTAINS_TUI" (from classify_tui.py)

    # --- Special cases detected pre-agent (zero LLM cost) ---
    special_cases: list[dict] = field(default_factory=list)

    # --- Raw data for the agent ---
    # Entries are either dicts (rule mode: {prompt, command, output, ...}) or
    # plain strings (LLM mode: bare command strings, no output attached).
    commands_with_output: list = field(default_factory=list)


# =============================================================================
# TUI / interactive program detection
# =============================================================================

# TUI / interactive programs that should NOT be run during verification;
# their commands_with_output entries will be marked with is_tui=True.
_TUI_PROGRAMS_ALWAYS = {
    # Editors — always interactive
    "vim", "vi", "nvim", "neovim", "nano", "pico", "emacs",
    "micro", "joe", "jed", "kakoune", "kak",
    # Pagers
    "less", "more", "most",
    # System monitors
    "htop", "top", "btop", "atop", "iotop", "iftop", "nmon", "glances",
    # Terminal multiplexers
    "tmux", "screen", "byobu",
    # File managers
    "mc", "ranger", "nnn", "lf", "vifm",
    # TUI tools
    "tig", "lazygit", "lazydocker", "k9s", "ncdu",
    # Misc interactive
    "ftp", "sftp", "telnet",
    "mutt", "alpine", "pine",
    "cfdisk", "fdisk", "parted",
    "dialog", "whiptail",
}

# Programs that are interactive only when invoked bare (no file/script argument).
# e.g., `python` is a REPL, but `python script.py` is not interactive.
# e.g., `mysql` is interactive, but `mysql -e "SELECT 1"` is not.
_TUI_PROGRAMS_BARE_ONLY = {
    "python", "python3", "python2", "node", "lua", "irb", "ghci",
    "erl", "iex", "R", "swift", "kotlin",
    "mysql", "psql", "sqlite3", "mongo", "mongosh", "redis-cli",
    "ed", "ex", "ssh",
}

# ---------------------------------------------------------------------------
# Special case detectors (zero LLM cost — pure regex / set matching)
# ---------------------------------------------------------------------------

_DOCKER_COMMANDS = {"docker", "docker-compose", "podman", "buildah", "nerdctl"}

_GUI_PROGRAMS = {"gedit", "kate", "xdg-open", "firefox", "chromium",
                 "google-chrome", "code", "subl", "atom", "gimp", "inkscape",
                 "vlc", "mpv", "eog", "evince", "nautilus", "thunar",
                 "xterm", "gnome-terminal", "konsole", "display", "xdotool",
                 "xclip", "xsel", "wmctrl", "xrandr"}

_GPU_SIGNALS = {"nvcc", "nvidia-smi", "rocm-smi", "cuda", "opencl", "clinfo"}

_PROPRIETARY_TOOLS = {"matlab", "mathematica", "stata", "spss", "sas",
                      "sqlplus", "tnsping", "impdp", "expdp",
                      "db2", "db2cmd"}

_SYSTEMD_COMMANDS = {"systemctl", "service", "journalctl", "timedatectl",
                     "hostnamectl", "loginctl", "networkctl"}

_KERNEL_COMMANDS = {"iptables", "ip6tables", "nft", "modprobe", "insmod",
                    "rmmod", "lsmod", "sysctl", "mount", "umount", "fdisk",
                    "mkfs", "losetup", "dmsetup", "brctl"}

_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                    # AWS access key
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*=\s*\S{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"),     # Bearer token
    re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                 # OpenAI key
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                 # GitHub PAT
    re.compile(r"glpat-[A-Za-z0-9\-]{20}"),             # GitLab PAT
]

_LARGE_DOWNLOAD_HINTS = re.compile(
    r"(wget|curl|aria2c|axel|aws\s+s3\s+cp|gsutil\s+cp).*"
    r"\.(tar\.gz|tar\.bz2|tar\.xz|zip|fa|fa\.gz|fasta|bam|vcf|hdf5|h5|parquet|csv\.gz)\b",
    re.IGNORECASE,
)

# Prompt pattern: user@host:path$ or user@host:path#
_PROMPT_USER_HOST_RE = re.compile(
    r"^(?P<user>[a-zA-Z0-9_][a-zA-Z0-9_.-]*)@(?P<host>[a-zA-Z0-9_][a-zA-Z0-9_.-]*):"
    r"(?P<path>[^\$#]*)\s*[\$#]\s*"
)


def _normalize_source_code_link(raw_entry: Any) -> dict[str, Any] | None:
    """Normalize a raw external URL entry from analysis.json."""
    if isinstance(raw_entry, str):
        url = raw_entry.strip()
        if not url:
            return None
        return {"url": url}
    if isinstance(raw_entry, dict):
        url = str(raw_entry.get("url", "")).strip()
        if not url:
            return None
        return dict(raw_entry) | {"url": url}
    return None


def _analyze_source_code_links(raw_urls: list[Any]) -> list[dict[str, Any]]:
    """Normalize source-code links into structured URL records."""
    link_records: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for raw_entry in raw_urls:
        record = _normalize_source_code_link(raw_entry)
        if not record:
            continue
        url = record["url"]
        if url not in seen_urls:
            link_records.append(record)
            seen_urls.add(url)

    return link_records


def detect_special_cases(signals: "EnvironmentSignals") -> list[dict]:
    """Scan extracted signals for known special-case patterns.

    Returns a list of dicts, each with:
      - tag: short label for prompt/metadata
      - severity: "blocker" (cannot proceed) | "warning" (proceed with caveats)
      - detail: human-readable one-liner
    """
    hits: list[dict] = []

    all_cmds = [c if isinstance(c, str) else c.get("command", "") for c in signals.commands_with_output]
    tools = {_extract_first_word(cmd) for cmd in all_cmds} - {""}

    # 1. Docker-in-Docker (DooD) / Nested Containers
    # The recording uses container tools (docker, docker-compose, podman).
    # While the agent can install the CLI clients, the Docker daemon cannot run
    # inside a standard unprivileged container because it lacks the kernel
    # capabilities (namespaces, cgroups, mounts) required to create new containers.
    # Therefore, verification commands like `docker ps` will fail with "Cannot
    # connect to the Docker daemon" — this is expected.
    # To actually use this environment later, the user must run it with
    # `-v /var/run/docker.sock:/var/run/docker.sock` (DooD) or `--privileged` (DinD).
    if tools & _DOCKER_COMMANDS:
        hits.append({
            "tag": "docker_in_docker", "severity": "warning",
            "detail": f"Docker/container tools detected: {sorted(tools & _DOCKER_COMMANDS)}. "
                      f"Install the CLI client only — the daemon cannot run inside an unprivileged container. "
                      f"Verify with `which docker`, not `docker ps`. "
                      f"At runtime, user must pass `-v /var/run/docker.sock:/var/run/docker.sock` (DooD) or `--privileged` (DinD).",
        })

    # 2. GUI Applications
    # The recording invokes graphical programs (e.g., firefox, gedit).
    # Docker containers are headless by default and lack an X11/Wayland display server.
    # The agent should install these tools but only verify them non-interactively
    # (e.g., checking their version or path), as running them directly will fail
    # with "cannot open display" errors.
    gui_found = tools & _GUI_PROGRAMS
    if gui_found:
        hits.append({
            "tag": "gui_application", "severity": "warning",
            "detail": f"GUI programs detected: {sorted(gui_found)}. "
                      f"These require X11/Wayland and cannot run headless.",
        })

    # 3. GPU-Dependent Workloads
    # The recording references GPU tools or libraries (like CUDA, nvidia-smi).
    # Building a fully functional GPU environment requires specialized base images
    # (e.g., nvidia/cuda) and passing the `--gpus all` flag at runtime.
    # The agent will likely generate a standard CPU-only image, meaning GPU-specific
    # code may fail during verification or execution.
    gpu_found = tools & _GPU_SIGNALS
    _gpu_keywords = ("cuda", "nvidia", "rocm", "torch-cuda", "tensorflow-gpu")
    gpu_in_cmds = {kw for cmd in all_cmds for kw in _gpu_keywords if kw in cmd.lower()}
    if gpu_found or gpu_in_cmds:
        hits.append({
            "tag": "gpu_workload", "severity": "warning",
            "detail": f"GPU-related tools/keywords detected: {sorted(gpu_found | gpu_in_cmds)}. "
                      f"Install GPU libraries normally, but expect GPU-specific code to fail during verification. "
                      f"The generated image will be CPU-only; at runtime use `--gpus all` with an nvidia/cuda base image.",
        })

    # 4. Windows Recordings (CMD / PowerShell)
    # The recording was captured in a Windows environment (Command Prompt or PowerShell).
    # Because standard Linux containers cannot natively execute Windows shell commands
    # or reproduce its ecosystem, this is a fundamental incompatibility.
    # It is flagged as a BLOCKER, causing the orchestrator to abort before the agent starts.
    _win_prefixes = ("Command Prompt", "Windows", "PowerShell")
    if any(signals.environment.startswith(p) for p in _win_prefixes):
        hits.append({
            "tag": "windows_recording", "severity": "blocker",
            "detail": "Recording is from Windows (CMD/PowerShell). "
                      "Linux containers cannot reproduce this environment.",
        })

    # 5. macOS-Specific Commands
    # The recording was captured on macOS and invokes commands exclusive to Apple's
    # ecosystem (e.g., launchctl, osascript, pbcopy, xcode-select).
    # Since we generate Linux containers, these specific commands have no direct
    # equivalent and will fail during verification. The agent will still attempt to
    # build the environment and translate general packages (like brew to apt).
    _mac_prefixes = ("macOS", "Mac OS")
    if any(signals.environment.startswith(p) for p in _mac_prefixes):
        macos_only = tools & {"launchctl", "osascript", "defaults", "xcode-select",
                              "xcodebuild", "open", "pbcopy", "pbpaste", "diskutil"}
        if macos_only:
            hits.append({
                "tag": "macos_specific", "severity": "warning",
                "detail": f"macOS-only commands detected: {sorted(macos_only)}. "
                          f"Skip these — they have no Linux equivalent. "
                          f"For brew packages, find the apt/pip equivalent instead.",
            })

    # 6. Licensed / Proprietary Software
    # The recording uses proprietary tools (like MATLAB, Mathematica, or Oracle SQL*Plus)
    # that require a paid license, account registration, or manual download.
    # Since they cannot be freely or automatically installed via standard Linux package
    # managers, the agent will likely fail to install them. It should treat them as
    # unresolvable and leave a manual step note in the Dockerfile rather than wasting
    # build attempts.
    propri_found = tools & _PROPRIETARY_TOOLS
    if propri_found:
        hits.append({
            "tag": "proprietary_software", "severity": "warning",
            "detail": f"Proprietary tools detected: {sorted(propri_found)}. "
                      f"These require a paid license or manual download and cannot be auto-installed. "
                      f"Leave a clearly commented placeholder in the Dockerfile instead of attempting installation.",
        })

    # 7. Embedded Secrets or Tokens
    # The recording contains commands with hardcoded authentication tokens, API keys,
    # or passwords. While the recording itself is public, these credentials are
    # almost certainly expired or revoked.
    # We flag this so the agent knows NOT to blindly copy these exact commands into
    # the Dockerfile. When authentication fails during verification, the agent will
    # recognize it as an expected credential issue rather than a missing dependency,
    # avoiding wasted build attempts.
    secrets_found = []
    for cmd_str in all_cmds:
        for pat in _SECRET_PATTERNS:
            if pat.search(cmd_str):
                secrets_found.append(cmd_str[:80])
                break
    if secrets_found:
        hits.append({
            "tag": "embedded_secrets", "severity": "warning",
            "detail": f"{len(secrets_found)} command(s) contain hardcoded tokens or credentials. "
                      f"Do NOT copy these commands into the Dockerfile. "
                      f"If authentication fails during verification, treat it as expected and skip — not a missing dependency.",
        })

    # 8. Multi-Session / Multi-Machine Recordings
    # The recording contains prompt patterns with different hostnames, suggesting
    # the user SSH'd into another machine during the session.
    # Without this warning, the agent might blindly try to combine conflicting
    # packages (e.g., mixing macOS brew with remote Ubuntu apt) into a single
    # Dockerfile. This flag tells the agent to focus on reproducing the primary
    # environment and gracefully ignore commands from the remote session.
    hostnames = set()
    for cmd_entry in signals.commands_with_output:
        prompt = "" if isinstance(cmd_entry, str) else cmd_entry.get("prompt", "")
        m = _PROMPT_USER_HOST_RE.match(prompt)
        if m:
            hostnames.add(m.group("host"))
    if len(hostnames) > 1:
        hits.append({
            "tag": "multi_machine", "severity": "warning",
            "detail": f"Multiple hostnames in prompts: {sorted(hostnames)}. "
                      f"The recording likely spans SSH sessions to different machines. "
                      f"Focus on the primary local environment and ignore commands from the remote session.",
        })

    # 9. Large Data Downloads
    # The recording includes commands that download potentially massive files
    # (e.g., datasets, model weights, or large archives). If the agent executes
    # these during `docker build`, the resulting image will be bloated and the
    # build might time out. This warning prompts the agent to consider moving
    # these downloads to a runtime ENTRYPOINT script instead.
    large_dl_cmds = [c for c in all_cmds if _LARGE_DOWNLOAD_HINTS.search(c)]
    if large_dl_cmds:
        hits.append({
            "tag": "large_data_download", "severity": "warning",
            "detail": f"{len(large_dl_cmds)} command(s) download large files (datasets, model weights, archives). "
                      f"Do NOT include these in `docker build` — the image will bloat or the build will time out. "
                      f"Move them to an ENTRYPOINT script or document as a post-startup step.",
        })

    # 10. Systemd / Init Services
    # The recording attempts to manage background services using `systemctl`, 
    # `service`, or similar init commands. Since Docker containers do not run 
    # an init system like systemd by default, these commands will fail with 
    # "System has not been booted with systemd" errors.
    # The agent should skip these commands during build and instead configure
    # the services to start in the foreground via ENTRYPOINT/CMD or docker-compose.
    systemd_found = tools & _SYSTEMD_COMMANDS
    # Also check for "service xxx start/restart/stop" pattern
    for cmd_str in all_cmds:
        parts = cmd_str.split()
        if len(parts) >= 2 and parts[0] == "service":
            systemd_found.add("service")
    if systemd_found:
        hits.append({
            "tag": "systemd_init", "severity": "warning",
            "detail": f"Systemd/init commands detected: {sorted(systemd_found)}. "
                      f"Skip these in the Dockerfile — containers have no init system. "
                      f"If a service is needed, configure it to start in the foreground via ENTRYPOINT/CMD or docker-compose.",
        })

    # 11. Kernel-Level Operations
    # The recording uses commands that interact directly with the Linux kernel
    # (e.g., iptables, mount, sysctl, modprobe). These operations are restricted
    # in standard Docker containers for security reasons and will fail with
    # "Operation not permitted".
    # The agent should note that the resulting image will require `--privileged`
    # or specific capabilities (e.g., `--cap-add=NET_ADMIN`) to run successfully.
    kernel_found = tools & _KERNEL_COMMANDS
    if kernel_found:
        hits.append({
            "tag": "kernel_operations", "severity": "warning",
            "detail": f"Kernel-level commands detected: {sorted(kernel_found)}. "
                      f"These are restricted in standard containers and will fail with 'Operation not permitted'. "
                      f"Note in the Dockerfile that the image requires `--privileged` or specific `--cap-add` flags at runtime.",
        })

    # 12. Interactive Build Systems
    # The recording contains commands like `make menuconfig` which launch interactive,
    # curses-based menus to configure build options (common in C/C++ or kernel development).
    # These menus will block indefinitely in a non-interactive `docker build` waiting
    # for user input, causing the build to time out.
    # The agent needs to either provide a pre-generated `.config` file or use
    # non-interactive alternatives (e.g., `make defconfig` or `yes "" | make config`).
    for cmd_str in all_cmds:
        first = _extract_first_word(cmd_str)
        if first in ("menuconfig", "nconfig", "xconfig", "gconfig"):
            hits.append({
                "tag": "interactive_build", "severity": "warning",
                "detail": "Interactive curses-based build configuration detected (e.g., menuconfig). "
                          "This will block indefinitely in a non-interactive `docker build`. "
                          "Replace with `make defconfig` or supply a pre-generated `.config` file.",
            })
            break
        if first == "make" and any(k in cmd_str for k in ("menuconfig", "nconfig")):
            hits.append({
                "tag": "interactive_build", "severity": "warning",
                "detail": "Interactive curses-based build configuration detected (e.g., make menuconfig). "
                          "This will block indefinitely in a non-interactive `docker build`. "
                          "Replace with `make defconfig` or supply a pre-generated `.config` file.",
            })
            break

    return hits


def _is_tui_command(cmd_str: str) -> bool:
    """Check if a command invokes a TUI/interactive program.

    Returns True if the command would block waiting for user input in a
    non-interactive Docker container. Used to mark commands so the agent
    skips them during verification.
    """
    first_word = _extract_first_word(cmd_str)
    if not first_word:
        return False

    # Always interactive (editors, monitors, file managers, etc.)
    if first_word in _TUI_PROGRAMS_ALWAYS:
        return True

    # Interactive only when invoked without arguments (bare REPL/shell)
    if first_word in _TUI_PROGRAMS_BARE_ONLY:
        parts = cmd_str.split()
        # Strip the command word itself (and sudo/env prefix)
        idx = 0
        for i, p in enumerate(parts):
            if p in ("sudo", "env") or "=" in p:
                continue
            idx = i
            break
        args_after_cmd = parts[idx + 1:]
        # If no real arguments (only flags like -i, -l), it's interactive
        non_flag_args = [a for a in args_after_cmd if not a.startswith("-")]
        if not non_flag_args:
            return True

    return False


# =============================================================================
# Extraction functions
# =============================================================================

def _extract_first_word(command_str: str) -> str:
    """Extract the first word (binary name) from a command string.

    Skips wrapper commands (sudo, env, nohup) and inline variable assignments
    like FOO=bar that precede the actual binary. Keeps --flag=value intact
    since those belong to the command itself.
    """
    _WRAPPERS = {"sudo", "env", "nohup"}
    for part in command_str.strip().split():
        if part in _WRAPPERS:
            continue
        if "=" in part and not part.startswith("-"):  # skip FOO=bar, keep --flag=val
            continue
        return part
    return ""


def _is_likely_real_command(command_str: str) -> bool:
    """Classify whether a command string is a real shell command or narration.

    Many recordings have users typing narration at the shell prompt
    (especially in demo/tutorial recordings). Returns False for text that
    looks like narration; callers use this to tag entries, not drop them.
    """
    cmd = command_str.strip()
    if not cmd:
        return False
    # Skip exit/clear
    if cmd in ("exit", "clear", "reset", "logout"):
        return False
    # Skip pure URLs typed as comments
    if cmd.startswith("http://") or cmd.startswith("https://"):
        return False

    # Narration heuristics: if the first word is a common English word
    # (capitalized, not a known command), it's likely narration
    raw_first = cmd.split()[0] if cmd.split() else ""
    _NARRATION_STARTERS = {
        "Hello", "Hi", "Hey", "Now", "The", "This", "That", "These",
        "Those", "Here", "There", "In", "On", "At", "For", "To", "So",
        "As", "If", "Or", "And", "But", "We", "You", "I", "It", "My",
        "Our", "Let", "All", "Also", "Please", "However", "Before",
        "After", "Once", "Done", "Okay", "OK", "Note", "Notice",
        "Remember", "Check", "See", "Look",
        # common sentence starters
        "as", "to", "or", "we", "have", "looks", "please",
        "however", "before", "after", "once", "also",
    }
    if raw_first in _NARRATION_STARTERS:
        return False

    # If the "command" looks like a long sentence (15+ words, no shell
    # metacharacters), it's likely narration
    words = cmd.split()
    if len(words) >= 15:
        has_shell_chars = any(
            c in cmd for c in ["|", ">", "<", ";", "&&", "||", "$", "`", "(", ")"]
        )
        has_flags = any(w.startswith("-") for w in words[1:])
        has_path = any("/" in w or "." in w for w in words)
        has_hyphenated_args = any("-" in w for w in words[1:])
        if not has_shell_chars and not has_flags and not has_path and not has_hyphenated_args:
            # Likely a natural language sentence
            return False

    return True


def _truncate_output(output: str) -> str:
    """Truncate long command output, keeping head and tail.

    Two-pass truncation strategy:
      Pass 1 (line-based): keep head + tail lines, drop noisy middle.
      Pass 2 (char-based):  hard cap on total characters to guard against
        single-line outputs (e.g. base64-encoded files, minified JS, binary
        data) that would bypass the line-count limit entirely.

    Most of the value in command output is at the start (what is happening)
    and the end (final result / success message). Middle lines are usually
    progress noise.
    """
    _OUTPUT_HEAD_LINES = 5    # lines to keep from the start of output
    _OUTPUT_TAIL_LINES = 3    # lines to keep from the end of output
    _OUTPUT_MAX_CHARS  = 2000 # hard cap: chars to keep from start + end

    if not output:
        return output

    # Pass 1: line-based truncation
    lines = output.splitlines()
    total_lines = _OUTPUT_HEAD_LINES + _OUTPUT_TAIL_LINES
    if len(lines) > total_lines:
        skipped = len(lines) - total_lines
        head = lines[:_OUTPUT_HEAD_LINES]
        tail = lines[-_OUTPUT_TAIL_LINES:]
        output = "\n".join(head) + f"\n[... {skipped} lines truncated ...]\n" + "\n".join(tail)

    # Pass 2: character-based hard cap (catches single-line giant outputs)
    if len(output) > _OUTPUT_MAX_CHARS:
        half = _OUTPUT_MAX_CHARS // 2
        skipped_chars = len(output) - _OUTPUT_MAX_CHARS
        output = output[:half] + f"\n[... {skipped_chars} chars truncated ...]\n" + output[-half:]

    return output


def _build_commands_with_output(
    commands: list[dict],
    recording_has_tui: bool = False,
) -> list[dict]:
    """Build the commands_with_output list from commands.json (rule mode) entries.

    Each entry keeps: prompt, command, truncated output, and optional tags:
      - is_narration=True  — typed narration/comment; read for context but
                             never executed during verification.
      - is_tui=True        — TUI/interactive program; skip during
                             non-interactive verification.
      - (no tag)           — regular executable command.
    """
    result: list[dict] = []
    for cmd_entry in commands:
        cmd_str = cmd_entry.get("command", "").strip()
        if not cmd_str:
            continue
        entry: dict = {
            "prompt": cmd_entry.get("prompt", ""),
            "command": cmd_str,
            "output": _truncate_output(cmd_entry.get("output", "")),
        }
        if not _is_likely_real_command(cmd_str):
            entry["is_narration"] = True
        elif recording_has_tui and _is_tui_command(cmd_str):
            entry["is_tui"] = True
        result.append(entry)
    return result


# =============================================================================
# Main loading function
# =============================================================================


def _load_commands_from_solve_sh(solve_sh_path: Path) -> list[str]:
    """Extract non-trivial command lines from a solve.sh script.

    Returns plain command strings (LLM mode format — no output attached).
    Strips shebang, blank lines, comments, and common shell preamble directives.
    """
    _SKIP_EXACT = {"set -e", "set -x", "set -ex", "set -xe", "set -euo pipefail", "set -eo pipefail"}
    commands: list[str] = []
    for line in solve_sh_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#!"):   # shebang
            continue
        if stripped.startswith("#"):    # comment
            continue
        if stripped in _SKIP_EXACT:     # shell preamble
            continue
        commands.append(stripped)
    return commands


def load_recording(
    recording_dir: Path,
) -> EnvironmentSignals:
    """Load a task directory and extract EnvironmentSignals.

    Expected layout (scaled_tasks_v1):
      {id}/source/info.json       ← recording metadata
      {id}/source/analysis.json   ← tui_classification, external_urls
      {id}/source/recording.txt   ← raw terminal recording
      {id}/solution/solve.sh      ← commands (used as primary signal)
      {id}/environment/           ← build output (written by this pipeline)

    Args:
        recording_dir: Path to the task directory (e.g. data/scaled_tasks_v1/100135).
    Returns:
        EnvironmentSignals with all extracted data.
    """
    recording_dir = Path(recording_dir)
    recording_id = recording_dir.name

    signals = EnvironmentSignals(
        recording_id=recording_id,
    )

    source_dir = recording_dir / "source"

    # --- info.json: metadata from asciinema ---
    info_path = source_dir / "info.json"
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            signals.title = info.get("title", "")
            signals.description = info.get("description", "")

            env_str = info.get("environment", "")
            if env_str:
                signals.environment = env_str
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load info.json: %s", e)

    # --- analysis.json: external URLs ---
    analysis_path = source_dir / "analysis.json"
    if analysis_path.exists():
        try:
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            raw_urls = analysis.get("external_urls", analysis.get("source_code_urls", []))
            signals.external_urls = _analyze_source_code_links(raw_urls)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load analysis.json: %s", e)

    # --- commands: extracted from solution/solve.sh ---
    solve_sh_path = recording_dir / "solution" / "solve.sh"
    if solve_sh_path.exists():
        cmds = _load_commands_from_solve_sh(solve_sh_path)
        signals.commands_with_output = cmds
        if not cmds:
            logger.debug("solve.sh exists but yielded zero commands for %s", recording_id)
    else:
        logger.warning("No solution/solve.sh found for %s", recording_id)

    # --- Detect special cases (zero LLM cost) ---
    signals.special_cases = detect_special_cases(signals)
    if signals.special_cases:
        tags = [sc["tag"] for sc in signals.special_cases]
        logger.warning("Detected %d special case(s): %s", len(tags), ", ".join(tags))

    logger.info(
        "Loaded task %s: env=%r commands=%d special_cases=%d external_urls=%s",
        recording_id,
        signals.environment or "(unknown)",
        len(signals.commands_with_output),
        len(signals.special_cases),
        signals.external_urls or "(none)",
    )

    return signals


# =============================================================================
# CLI entry point (for use as Level 3 Skill script)
# =============================================================================


def signals_to_dict(signals: EnvironmentSignals) -> dict:
    """Convert EnvironmentSignals to a JSON-serializable dict."""
    return {
        "recording_id": signals.recording_id,
        "environment": signals.environment,
        "title": signals.title,
        "description": signals.description,
        "external_urls": signals.external_urls,
        "special_cases": signals.special_cases,
    }


def main():
    """CLI entry point: analyze a recording and print JSON to stdout."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Analyze a terminal recording and output structured environment signals as JSON.",
    )
    parser.add_argument(
        "--recording-dir",
        required=True,
        help="Path to the recording directory (e.g. data/recordings/1060)",
    )
    args = parser.parse_args()

    recording_dir = Path(args.recording_dir)
    if not recording_dir.is_dir():
        print(json.dumps({"error": f"Recording directory not found: {recording_dir}"}))
        sys.exit(1)

    # Suppress logging to stderr so only JSON goes to stdout
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    signals = load_recording(recording_dir)
    result = signals_to_dict(signals)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
