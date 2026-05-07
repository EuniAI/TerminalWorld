#!/usr/bin/env python3
"""
Environment building orchestrator: thin CLI wrapper for triggering the docker-env-builder Skill via Claude Code Agent SDK.

This wrapper:
  1. Validates paths
  2. Sends a one-liner prompt to Claude Code via the SDK
  3. Logs cost and metadata

Usage:
    python -m environment_building.build_environment \
        --recording-id 1060 --recordings-dir ./data/recordings

Environment variables (all required for Claude Code CLI to work):
    ANTHROPIC_API_KEY=sk-...                    Proxy API key (Claude Code CLI reads this, NOT ANTHROPIC_AUTH_TOKEN)
    ANTHROPIC_BASE_URL=https://...              LiteLLM proxy endpoint
    CLAUDE_CODE_SKIP_SANDBOX=1                  Skip bubblewrap sandbox (required on most Linux servers)
    CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1    Suppress beta headers unsupported by LiteLLM proxy
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import anyio

logger = logging.getLogger("env_builder")

# 10 min less than batch_build's subprocess timeout (7200s) so cleanup has
# time to run before the parent kills the process tree.
_AGENT_TASK_TIMEOUT_SECONDS = 7200 - 600  # 110 min


class MaxTurnsError(RuntimeError):
    """Agent hit the max_turns limit without completing the task."""
    def __init__(self, msg: str, cost_usd: float = 0.0):
        super().__init__(msg)
        self.cost_usd = cost_usd


class MaxCostError(RuntimeError):
    """Agent exceeded the cost budget."""
    def __init__(self, msg: str, cost_usd: float = 0.0):
        super().__init__(msg)
        self.cost_usd = cost_usd


# ---------------------------------------------------------------------------
# Logger setup
# ---------------------------------------------------------------------------


def setup_logger(log_path: str | None = None) -> logging.Logger:
    """Configure the logger with console + optional file output."""
    logger.setLevel(logging.INFO)
    logger.handlers = []

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    if log_path:
        fh = logging.FileHandler(log_path)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# Agent SDK interaction
# ---------------------------------------------------------------------------


def _serialize_content_block(block) -> dict:
    """Convert a ContentBlock to a JSON-serializable dict."""
    from claude_agent_sdk import (
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
    )

    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    elif isinstance(block, ThinkingBlock):
        return {
            "type": "thinking",
            "thinking": block.thinking,
            "signature": block.signature,
        }
    elif isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    elif isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    else:
        return {"type": type(block).__name__, "repr": repr(block)}


async def run_agent(
    prompt: str,
    cwd: str,
    max_turns: int = 30,
    max_cost_usd: float = 1.5,
    model: str | None = None,
    turn_delay_seconds: float = 0.0,
) -> dict:
    """Run the Claude Code agent with the given prompt. This function
    sends the trigger prompt, collects results, and records the full
    trajectory for later analysis.

    Args:
        prompt: The trigger prompt for the agent.
        cwd: Working directory for the agent.
        max_turns: Maximum agent turns.
        model: Model name override (e.g. "claude-sonnet-4"). If None, the
            SDK/proxy default is used. Fallback is handled by the LiteLLM proxy.
        turn_delay_seconds: Seconds to sleep after each assistant turn to reduce
            request pressure. 0 disables the delay.

    Returns:
        Metadata dict with cost, usage, timing, session_id, and trajectory.
    """
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            SystemMessage,
            TextBlock,
            query,
        )
    except ImportError:
        logger.error(
            "claude-agent-sdk is not installed. Install it with:\n"
            "  pip install claude-agent-sdk\n"
            "Also ensure Claude Code CLI is available."
        )
        raise

    options = ClaudeAgentOptions(
        allowed_tools=[
            "Skill", "Read", "Write", "Edit", "Bash",
            "Glob", "Grep", "WebSearch",
        ],
        setting_sources=["user", "project"],
        permission_mode="bypassPermissions",
        cwd=cwd,
        max_turns=max_turns,
        model=model or None,
    )

    logger.info("Starting Claude Code agent (max_turns=%d, max_cost=$%.2f)", max_turns, max_cost_usd)
    logger.info("  model=%s  turn_delay=%.1fs",
                model or "(proxy default)", turn_delay_seconds)
    logger.info("Agent working directory: %s", cwd)
    logger.info("Prompt: %s", prompt)

    total_cost_usd = 0.0
    manual_cost_usd = 0.0
    assistant_turns = 0
    sdk_num_turns: int | None = None
    session_id: str | None = None
    start_time = time.time()
    trajectory: list[dict] = []
    _last_error_subtype: str | None = None

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                assistant_turns += 1
                turn_record = {
                    "type": "assistant",
                    "turn": assistant_turns,
                    "model": message.model,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "content": [
                        _serialize_content_block(b) for b in message.content
                    ],
                }
                if message.parent_tool_use_id:
                    turn_record["parent_tool_use_id"] = message.parent_tool_use_id
                if message.error:
                    turn_record["error"] = repr(message.error)
                    logger.warning(
                        "[Turn %d] AssistantMessage error: %s",
                        assistant_turns, message.error,
                    )
                trajectory.append(turn_record)

                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_preview = block.text[:200]
                        if len(block.text) > 200:
                            text_preview += "..."
                        logger.info("[Turn %d] %s", assistant_turns, text_preview)

                # Check budget limit (cost is usually only updated in ResultMessage,
                # but some proxies/SDK versions stream it incrementally)
                if total_cost_usd > max_cost_usd:
                    logger.warning("Hard cost limit exceeded ($%.4f > $%.2f), terminating agent.", total_cost_usd, max_cost_usd)
                    raise MaxCostError("Cost limit exceeded ($%.4f)" % total_cost_usd, cost_usd=total_cost_usd)

                # Throttle: sleep between turns to reduce Bedrock throughput pressure
                if turn_delay_seconds > 0:
                    await anyio.sleep(turn_delay_seconds)

            elif isinstance(message, ResultMessage):
                session_id = message.session_id

                # 1. Check if SDK calculated the cumulative cost
                sdk_cost = getattr(message, "total_cost_usd", 0.0) or 0.0

                # 2. Manually calculate this turn's cost and add to our manual running total
                turn_cost = 0.0
                if hasattr(message, "usage") and message.usage:
                    usage_obj = message.usage
                    if isinstance(usage_obj, dict):
                        in_tok = usage_obj.get("input_tokens", 0)
                        out_tok = usage_obj.get("output_tokens", 0)
                    else:
                        in_tok = getattr(usage_obj, "input_tokens", 0)
                        out_tok = getattr(usage_obj, "output_tokens", 0)

                    # Claude Sonnet (3.5/3.7) pricing: $3.00/1M input, $15.00/1M output
                    turn_cost = (in_tok * 3.0 + out_tok * 15.0) / 1_000_000.0

                manual_cost_usd += turn_cost

                # 3. Use SDK cost if available, otherwise use our manual cumulative cost
                total_cost_usd = sdk_cost if sdk_cost > 0.0 else manual_cost_usd

                # Check budget again when ResultMessage explicitly delivers the final cost
                if total_cost_usd > max_cost_usd:
                    logger.warning("Hard cost limit exceeded on ResultMessage ($%.4f > $%.2f).", total_cost_usd, max_cost_usd)
                    raise MaxCostError("Cost limit exceeded ($%.4f)" % total_cost_usd, cost_usd=total_cost_usd)
                trajectory.append({
                    "type": "result",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "subtype": message.subtype,
                    "is_error": message.is_error,
                    "num_turns": message.num_turns,
                    "session_id": message.session_id,
                    "duration_ms": message.duration_ms,
                    "duration_api_ms": message.duration_api_ms,
                    "total_cost_usd": total_cost_usd,
                    "usage": message.usage,
                    "result": message.result,
                    "structured_output": message.structured_output,
                })
                sdk_num_turns = message.num_turns
                logger.info("Agent finished. session_id=%s  cost=$%.4f  sdk_turns=%s",
                            session_id, total_cost_usd, sdk_num_turns)
                if message.is_error:
                    _last_error_subtype = message.subtype
                    logger.warning("Agent finished with is_error=True (subtype=%s)", message.subtype)

            elif isinstance(message, SystemMessage):
                # session_id may appear in SystemMessage.data before ResultMessage
                if "session_id" in message.data and session_id is None:
                    session_id = message.data["session_id"]
                trajectory.append({
                    "type": "system",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "subtype": message.subtype,
                    "data": message.data,
                })

            else:
                trajectory.append({
                    "type": type(message).__name__,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "repr": repr(message),
                })

    except MaxCostError:
        raise
    except Exception as _sdk_exc:
        if _last_error_subtype == "error_max_turns":
            raise MaxTurnsError(str(_sdk_exc), cost_usd=total_cost_usd) from _sdk_exc
        raise

    elapsed = time.time() - start_time

    return {
        "agent_turns": sdk_num_turns if sdk_num_turns is not None else assistant_turns,
        "agent_turns_raw": assistant_turns,  # AssistantMessage 事件数，约为 sdk_turns 的 1.7x
        "max_turns": max_turns,
        "session_id": session_id,
        "total_cost_usd": round(total_cost_usd, 6),
        "elapsed_seconds": round(elapsed, 2),
        "trajectory": trajectory,
    }



def prepare_agent_workspace(env_dir: Path, project_root: Path) -> Path:
    """Create an isolated per-task workspace for Claude Code.

    Parallel Claude Code SDK runs should not share the same cwd because the CLI
    stores session-scoped state relative to the working directory. We keep the
    real build artifacts in ``env_dir`` and give each run a dedicated hidden
    workspace that only needs access to the repository's ``.claude`` skill tree.
    """
    source_claude_dir = project_root / ".claude"
    if not source_claude_dir.is_dir():
        raise RuntimeError(f"Required Claude skill directory is missing: {source_claude_dir}")

    workspace_dir = Path(
        tempfile.mkdtemp(prefix=".agent_workspace_", dir=str(env_dir))
    )
    workspace_claude_dir = workspace_dir / ".claude"
    workspace_claude_dir.symlink_to(source_claude_dir, target_is_directory=True)
    return workspace_dir


_WORKSPACE_POLICY_DEFAULTS = {
    "resume": "fresh",
    "repair": "reuse",
    "retry_failed": "reuse",
    "refresh": "fresh",
}
_TRANSIENT_WORKSPACE_PREFIX = ".agent_workspace_"

# Files that stay in environment/ after a build; everything else moves to
# pipeline_artifacts/environment/.
_KEEP_IN_ENV = {
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
}


def resolve_workspace_policy(run_mode: str, requested_policy: str) -> str:
    """Resolve the final workspace policy for this build."""
    if requested_policy != "auto":
        return requested_policy
    return _WORKSPACE_POLICY_DEFAULTS.get(run_mode, "fresh")


def read_previous_cost(task_dir: Path) -> float:
    """Return the cumulative total_cost_usd from the last run, or 0.0."""
    prev_metadata = task_dir / "pipeline_artifacts" / "environment" / "build_metadata.json"
    if not prev_metadata.exists():
        return 0.0
    try:
        data = json.loads(prev_metadata.read_text(encoding="utf-8"))
        return float(data.get("total_cost_usd", 0.0))
    except Exception:
        return 0.0


def move_artifacts_to_pipeline(
    task_dir: Path,
    log_path: str | None = None,
) -> None:
    """Move intermediate build artifacts from environment/ to pipeline_artifacts/environment/.

    Only Dockerfile and docker-compose.yaml remain in environment/.
    The build log is copied (not moved) so the external log directory is unchanged.
    """
    env_dir = task_dir / "environment"
    pipeline_dir = (task_dir / "pipeline_artifacts" / "environment").resolve()
    pipeline_dir.mkdir(parents=True, exist_ok=True)

    for child in sorted(env_dir.iterdir()):
        if child.name in _KEEP_IN_ENV:
            continue
        if child.name.startswith(_TRANSIENT_WORKSPACE_PREFIX):
            continue  # already removed by cleanup
        dest = pipeline_dir / child.name
        if dest.exists():
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        shutil.move(str(child), str(dest))

    if log_path:
        log_src = Path(log_path)
        if log_src.exists():
            shutil.copy2(log_src, pipeline_dir / log_src.name)
            logger.info("Log copied to pipeline artifacts: %s", pipeline_dir / log_src.name)


def reset_environment_dir(env_dir: Path) -> list[str]:
    """Remove all contents under env_dir while preserving the directory itself."""
    removed_entries: list[str] = []
    if not env_dir.exists():
        return removed_entries

    for child in sorted(env_dir.iterdir(), key=lambda path: path.name):
        marker = "/" if child.is_dir() and not child.is_symlink() else ""
        removed_entries.append(f"{child.name}{marker}")
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    return removed_entries


def collect_environment_artifacts(env_dir: Path) -> list[str]:
    """List retained top-level artifacts that are meaningful to the agent."""
    if not env_dir.exists():
        return []

    artifacts: list[str] = []
    for child in sorted(env_dir.iterdir(), key=lambda path: path.name):
        if child.name.startswith(_TRANSIENT_WORKSPACE_PREFIX):
            continue
        marker = "/" if child.is_dir() and not child.is_symlink() else ""
        artifacts.append(f"{child.name}{marker}")
    return artifacts


def summarize_artifacts(artifacts: list[str], limit: int = 8) -> str:
    """Summarize a top-level artifact list for logs, prompts, and metadata."""
    if not artifacts:
        return "none"

    shown = artifacts[:limit]
    summary = ", ".join(shown)
    remaining = len(artifacts) - len(shown)
    if remaining > 0:
        summary += f", ... (+{remaining} more)"
    return summary


def detect_build_repair_context(task_dir: Path, recording_id: str) -> str | None:
    """Detect previous build artifacts and return a prompt section for repair mode.

    Mirrors refine_task.detect_repair_context(): reads prior metadata and log
    pointers so the agent can resume from the failure point rather than starting
    from scratch.
    """
    pipeline_dir = task_dir / "pipeline_artifacts" / "environment"
    if not pipeline_dir.exists():
        return None

    lines: list[str] = []

    # Previous build metadata
    meta_path = pipeline_dir / "build_metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            status = meta.get("maturity_level") or meta.get("status", "unknown")
            dockerfile = meta.get("dockerfile_created", "?")
            compose = meta.get("compose_created", "?")
            error = meta.get("error_detail", "")
            lines.append(
                f"- Previous status: `{status}` "
                f"(dockerfile={dockerfile}, compose={compose})"
            )
            if error:
                lines.append(f"- Previous error: {str(error)[:300]}")
        except Exception:
            lines.append(f"- Previous metadata found but unreadable: {meta_path}")

    # Previous build log
    log_path = pipeline_dir / f"build_{recording_id}.log"
    if log_path.exists():
        lines.append(f"- Previous build log: `{log_path}`")

    # Pipeline artifacts overview
    pipeline_artifacts = collect_environment_artifacts(pipeline_dir)
    if pipeline_artifacts:
        lines.append(
            f"- Pipeline artifacts: {summarize_artifacts(pipeline_artifacts)}"
            f"  (full path: {pipeline_dir})"
        )

    # Retained files in environment/ (Dockerfile, docker-compose.yml)
    env_dir = task_dir / "environment"
    retained = (
        [c.name for c in env_dir.iterdir() if c.name in _KEEP_IN_ENV]
        if env_dir.exists() else []
    )
    if retained:
        lines.append(f"- Retained in environment/: {', '.join(retained)}")

    if not lines:
        return None

    return "\n".join([
        "## Repair Context — Previous Build Attempt",
        "This task was previously attempted but did not produce a working environment.",
        "Before rebuilding from scratch, read the previous run artifacts:",
        *lines,
        "",
        "**Instructions:**",
        "1. Read the previous build log to understand what was tried and where it failed.",
        "2. If a Dockerfile or docker-compose.yml already exists in `environment/`, inspect it — "
        "fix it rather than rewriting from scratch if the structure is salvageable.",
        "3. Check `pipeline_artifacts/environment/` for intermediate artifacts "
        "(e.g. env_signals.json, base image choices) that can be reused.",
        "4. Focus only on fixing the failure point; do NOT redo steps that already succeeded.",
    ])


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------


_DISK_LOW_THRESHOLD_GB = 5


def preflight_docker_check() -> list[str]:
    """Verify the Docker environment is healthy before starting the agent.

    Returns a list of human-readable problems found.  An empty list means
    all checks passed.
    """
    problems: list[str] = []

    # Docker daemon reachable?
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "permission denied" in stderr.lower():
                problems.append(
                    "Docker permission denied — run with sudo or add your "
                    "user to the 'docker' group."
                )
            elif "cannot connect" in stderr.lower() or "not running" in stderr.lower():
                problems.append(
                    "Docker daemon is not running. Start it with: "
                    "sudo systemctl start docker"
                )
            else:
                problems.append(f"docker info failed: {stderr or result.stdout.strip()}")
    except FileNotFoundError:
        problems.append(
            "Docker CLI not found on PATH. Install Docker: "
            "https://docs.docker.com/engine/install/"
        )
    except subprocess.TimeoutExpired:
        problems.append(
            "docker info timed out (15s) — the Docker daemon may be "
            "unresponsive or overloaded."
        )

    # Docker Compose reachable?
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            # Fallback to older docker-compose standalone binary
            result2 = subprocess.run(
                ["docker-compose", "version"],
                capture_output=True, text=True, timeout=10,
            )
            if result2.returncode != 0:
                problems.append(
                    "Docker Compose not found. Install the docker-compose-plugin: "
                    "sudo apt-get install docker-compose-plugin"
                )
    except FileNotFoundError:
        # This shouldn't happen if docker exists, but just in case
        problems.append("Docker Compose not found.")
    except subprocess.TimeoutExpired:
        problems.append("docker compose version timed out (10s).")

    # Host disk space on Docker root (usually /)
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.DockerRootDir}}"],
            capture_output=True, text=True, timeout=10,
        )
        docker_root = result.stdout.strip() if result.returncode == 0 else "/"

        # Traverse up to find a directory we have permission to read
        check_path = Path(docker_root)
        while not os.access(check_path, os.R_OK) and check_path.parent != check_path:
            check_path = check_path.parent

        usage = shutil.disk_usage(check_path)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < _DISK_LOW_THRESHOLD_GB:
            problems.append(
                f"Low disk space on {check_path}: {free_gb:.1f} GB free "
                f"(< {_DISK_LOW_THRESHOLD_GB} GB). Free space with: "
                "docker system prune -af"
            )
    except Exception as e:
        logger.debug("Disk space check skipped: %s", e)

    return problems


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Build a Docker environment from a terminal recording (via Skill).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    # Auto-detect source code URL from analysis.json:
    python -m environment_building.build_environment \\
        --recording-id 1060

    # Specify model (proxy default is used if omitted):
    python -m environment_building.build_environment \\
        --recording-id 1060 \\
        --model claude-sonnet-4

Environment variables:
    ANTHROPIC_BASE_URL=https://...  LiteLLM proxy endpoint
    ANTHROPIC_AUTH_TOKEN=sk-...     Proxy API key
""",
    )
    parser.add_argument(
        "--recording-id",
        required=True,
        help="Recording ID (e.g. 1060)",
    )
    parser.add_argument(
        "--recordings-dir",
        default="./data/scaled_tasks_v1",
        help="Base directory containing task subdirectories "
        "(default: ./data/scaled_tasks_v1)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=30,
        help="Maximum number of agent turns (default: 30)",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=1.5,
        help="Hard cost limit in USD. Agent is killed if exceeded (default: 1.5)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name to use (e.g. claude-sonnet-4). ",
    )
    parser.add_argument(
        "--turn-delay",
        type=float,
        default=3.0,
        help=(
            "Seconds to sleep after each agent turn to reduce request pressure "
            "(default: 3.0; set 0 to disable)"
        ),
    )
    parser.add_argument(
        "--log-dir",
        default="./environment_building/logs",
        help="Directory for log files (default: ./environment_building/logs)",
    )
    parser.add_argument(
        "--workspace-policy",
        choices=("auto", "fresh", "reuse"),
        default="auto",
        help=(
            "How to handle the task's environment/ directory before building: "
            "'fresh' clears it, 'reuse' keeps existing artifacts, 'auto' maps from run mode "
            "(default: auto)."
        ),
    )
    parser.add_argument(
        "--run-mode",
        choices=("resume", "repair", "retry_failed", "refresh"),
        default="resume",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Validate paths
    project_root = Path(__file__).resolve().parent.parent
    recordings_dir = Path(args.recordings_dir).resolve()
    task_dir = (recordings_dir / args.recording_id).resolve()

    if not task_dir.is_dir():
        print(f"ERROR: Task directory not found: {task_dir}", file=sys.stderr)
        sys.exit(1)

    env_dir = (task_dir / "environment").resolve()
    env_dir.mkdir(parents=True, exist_ok=True)

    # Setup logging
    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(log_dir / f"build_{args.recording_id}.log")
    setup_logger(log_path)

    # Read cumulative cost from the previous run (if any) before the build starts.
    previous_total_cost = read_previous_cost(task_dir)

    requested_workspace_policy = args.workspace_policy
    workspace_policy = resolve_workspace_policy(
        args.run_mode, requested_workspace_policy
    )
    retained_artifacts = collect_environment_artifacts(env_dir)
    reset_artifacts: list[str] = []
    if workspace_policy == "fresh":
        reset_artifacts = reset_environment_dir(env_dir)
        retained_artifacts = []

    agent_workspace = prepare_agent_workspace(env_dir, project_root)
    retained_artifacts_summary = summarize_artifacts(retained_artifacts)
    reset_artifacts_summary = summarize_artifacts(reset_artifacts)

    logger.info("=" * 70)
    logger.info("Environment Building (Skill-based)")
    logger.info("=" * 70)
    logger.info("  Recording ID:    %s", args.recording_id)
    logger.info("  Task dir:        %s", task_dir)
    logger.info("  Run mode:        %s", args.run_mode)
    logger.info("  Workspace policy requested: %s", requested_workspace_policy)
    logger.info("  Workspace policy resolved:  %s", workspace_policy)
    logger.info("  Env dir:         %s", env_dir)
    if workspace_policy == "fresh":
        logger.info("  Env dir reset: YES (%s)", reset_artifacts_summary)
    else:
        logger.info("  Env dir reset: NO")
        logger.info("  Retained artifacts: %s", retained_artifacts_summary)
    logger.info("  Agent workspace: %s", agent_workspace)
    logger.info("  Max turns:       %d", args.max_turns)
    logger.info("=" * 70)

    # -----------------------------------------------------------------------
    # Pre-flight: verify required environment variables for Claude Code CLI
    # -----------------------------------------------------------------------
    _env_warnings: list[str] = []
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _env_warnings.append(
            "ANTHROPIC_API_KEY is not set. Claude Code CLI will fail to authenticate.\n"
            "  export ANTHROPIC_API_KEY=<your-proxy-key>"
        )
    if not os.environ.get("ANTHROPIC_BASE_URL"):
        _env_warnings.append(
            "ANTHROPIC_BASE_URL is not set. Claude Code CLI will hit api.anthropic.com instead of your proxy.\n"
            "  export ANTHROPIC_BASE_URL=<your-litellm-proxy-url>"
        )
    if not os.environ.get("CLAUDE_CODE_SKIP_SANDBOX"):
        _env_warnings.append(
            "CLAUDE_CODE_SKIP_SANDBOX is not set. CLI may hang on Linux servers without bubblewrap.\n"
            "  export CLAUDE_CODE_SKIP_SANDBOX=1"
        )
    if not os.environ.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"):
        _env_warnings.append(
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS is not set. CLI may send beta headers that LiteLLM proxy rejects.\n"
            "  export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"
        )
    if _env_warnings:
        logger.warning("Environment variable check — potential issues detected:")
        for w in _env_warnings:
            logger.warning("  ⚠  %s", w)

    # -----------------------------------------------------------------------
    # Pre-flight: verify Docker environment before wasting agent turns
    # -----------------------------------------------------------------------

    logger.info("Running pre-flight Docker checks...")
    docker_problems = preflight_docker_check()
    if docker_problems:
        logger.error("Pre-flight check FAILED — fix these issues first:")
        for i, problem in enumerate(docker_problems, 1):
            logger.error("  [%d] %s", i, problem)
        sys.exit(1)
    logger.info("Pre-flight checks passed.")

    # -----------------------------------------------------------------------
    # Pre-run: extract recording signals and save them
    # -----------------------------------------------------------------------
    from environment_building.analyze_recording import (
        load_recording,
        signals_to_dict,
    )

    logger.info("Analyzing recording signals...")
    try:
        signals = load_recording(task_dir)
        signals_dict = signals_to_dict(signals)
    except Exception as e:
        logger.error("Failed to analyze recording: %s", e, exc_info=True)
        sys.exit(1)

    external_urls = signals.external_urls
    logger.info("  External URLs: %s", external_urls or "(none)")

    base_metadata = {
        "id": args.recording_id,
        "external_urls": external_urls,
        "run_mode": args.run_mode,
        "workspace_policy": workspace_policy,
        "workspace_policy_requested": requested_workspace_policy,
        "workspace_reset": workspace_policy == "fresh",
        "reset_artifacts": reset_artifacts,
        "reset_artifacts_summary": reset_artifacts_summary,
        "reused_artifacts": retained_artifacts,
        "reused_artifacts_summary": retained_artifacts_summary,
    }

    signals_path = env_dir / "env_signals.json"
    with open(signals_path, "w", encoding="utf-8") as f:
        json.dump(signals_dict, f, ensure_ascii=False, indent=2)
    logger.info("Environment signals saved: %s", signals_path)

    # -----------------------------------------------------------------------
    # Check for blocker-level special cases (abort before wasting agent turns)
    # -----------------------------------------------------------------------
    blockers = [sc for sc in signals.special_cases if sc["severity"] == "blocker"]
    if blockers:
        logger.error("Recording has BLOCKER special cases — cannot proceed:")
        for sc in blockers:
            logger.error("  [%s] %s", sc["tag"], sc["detail"])
        # Still save metadata so the blocker is recorded
        metadata_path = env_dir / "build_metadata.json"
        metadata = {
            **base_metadata,
            "status": "blocked",
            "blocked_by": [sc["tag"] for sc in blockers],
            "special_cases": signals.special_cases,
            "signals_file": "env_signals.json",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        logger.info("Metadata saved: %s", metadata_path)
        shutil.rmtree(agent_workspace, ignore_errors=True)
        move_artifacts_to_pipeline(task_dir, log_path)
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Build the trigger prompt
    # -----------------------------------------------------------------------
    image_tag = f"terminalworld-env-{args.recording_id}"
    solve_sh_path = task_dir / "solution" / "solve.sh"
    recording_txt_path = task_dir / "source" / "recording.txt"

    has_external_links = bool(external_urls)

    if has_external_links:
        hierarchy_constraint = (
            "1. Information Hierarchy (AGENT DECIDES): External URLs are present. "
            "Inspect `external_urls` to determine which links are likely repositories via `is_repo`, "
            "and whether each link is reachable via `access_status` and `privacy`. Treat this URL analysis as structured evidence, "
            "but decide for yourself whether repository scanning or `solve.sh` should be the primary source of truth. "
            "These URLs are related to the current task; you must decide which ones are important for environment reconstruction and, when useful, open/read those URLs to inspect their contents."
        )
    else:
        hierarchy_constraint = (
            "1. Information Hierarchy (SOLUTION FIRST): No external URLs are provided. "
            "Therefore, `solve.sh` is your PRIMARY source of truth for inferring dependencies and versions."
        )

    pipeline_artifacts_dir = (task_dir / "pipeline_artifacts" / "environment").resolve()
    pipeline_artifacts = collect_environment_artifacts(pipeline_artifacts_dir)
    pipeline_artifacts_summary = summarize_artifacts(pipeline_artifacts)

    if workspace_policy == "reuse":
        workspace_prompt_lines = [
            "## Workspace State",
            f"- Run Mode:         {args.run_mode}",
            f"- Workspace Policy: {workspace_policy} (requested: {requested_workspace_policy})",
            "- Artifacts from the previous run are available for reference:",
            f"  - environment/:                   {retained_artifacts_summary}",
            f"  - pipeline_artifacts/environment/: {pipeline_artifacts_summary}",
            f"    (full path: {pipeline_artifacts_dir})",
            "- You may inspect and reuse those artifacts if they are helpful, but they are not authoritative.",
            "- If retained artifacts conflict with `env_signals.json` or `solve.sh` evidence, trust the current signals.",
            "",
        ]
    else:
        workspace_prompt_lines = [
            "## Workspace State",
            f"- Run Mode:         {args.run_mode}",
            f"- Workspace Policy: {workspace_policy} (requested: {requested_workspace_policy})",
            "- The output directory was reset before this run.",
            "- Do not depend on any files from previous attempts; rebuild from the current signals only.",
            "",
        ]

    # Inject repair context when retrying a previously-failed build
    repair_context_section: list[str] = []
    if args.run_mode in ("repair", "retry_failed"):
        repair_ctx = detect_build_repair_context(task_dir, args.recording_id)
        if repair_ctx:
            repair_context_section = [repair_ctx, ""]

    prompt = "\n".join([
        "## Objective",
        "You are an expert environment engineer. Reconstruct a working, reproducible Docker environment "
        "from the provided environment signals. Strictly follow the `docker-env-builder` skill workflow. "
        "Do not skip steps or invent dependencies without evidence from the signals.",
        "",
        "## Task Parameters",
        f"- Task ID:          {args.recording_id}",
        f"- Task Directory:   {task_dir}",
        f"- Output Directory: {env_dir}",
        f"- Image Tag:        {image_tag}",
        "",
        *workspace_prompt_lines,
        *repair_context_section,
        "## Step 0 — Read Input Data",
        "Before taking any action, read both of these files in full:",
        f"  1. Environment signals: {signals_path}",
        f"  2. Reference solution:  {solve_sh_path}",
        "",
        "`env_signals.json` key fields:",
        "- `environment`:   raw OS / shell / terminal string (use for base image selection)",
        "- `external_urls`: structured external URL records (each includes `url`, `platform`, `is_repo`, `access_status`, and `privacy`)",
        "- `special_cases`: pre-detected edge cases (see constraints below)",
        "",
        "`solve.sh` is the reference solution script — read it to infer all required tools, packages, and runtime dependencies.",
        *(
            ["",
             f"Supplementary information (read only if solve.sh lacks sufficient context):",
             f"  {recording_txt_path}"]
            if recording_txt_path.exists() else []
        ),
        "",
        "## Constraints",
        hierarchy_constraint,
        "2. Special Cases: Read the `special_cases` array before starting. All entries at this stage "
        "are 'warning' severity. For each, adapt your build and verification strategy based on its "
        "`tag` and `detail`; do NOT abort or treat them as build failures.",
    ])

    async def _agent_task() -> dict:
        with anyio.fail_after(_AGENT_TASK_TIMEOUT_SECONDS):
            return await run_agent(
                prompt,
                str(agent_workspace),
                args.max_turns,
                max_cost_usd=args.max_cost,
                model=args.model,
                turn_delay_seconds=args.turn_delay,
            )

    def run_cleanup():
        logger.info("Enforcing mandatory cleanup...")
        cleanup_script = Path(__file__).resolve().parent / "cleanup.py"
        try:
            subprocess.run(
                [sys.executable, str(cleanup_script), "--output-dir", str(env_dir), "--image-tag", image_tag],
                check=False,
                capture_output=True,
                text=True,
                timeout=120
            )
            logger.info("Cleanup completed successfully.")
        except Exception as err:
            logger.warning("Cleanup failed or timed out: %s", err)
        finally:
            shutil.rmtree(agent_workspace, ignore_errors=True)

    try:
        agent_result = anyio.run(_agent_task)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (Ctrl+C)")
        run_cleanup()
        # Write minimal metadata so the next run can read previous cost correctly
        metadata_path = env_dir / "build_metadata.json"
        interrupted_metadata = {
            **base_metadata,
            "status": "Interrupted",
            "run_cost_usd": 0.0,
            "total_cost_usd": previous_total_cost,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
        }
        if metadata_path.exists():
            try:
                with open(metadata_path, encoding="utf-8") as f:
                    existing_metadata = json.load(f)
                interrupted_metadata = {**existing_metadata, **interrupted_metadata}
            except Exception:
                pass
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(interrupted_metadata, f, ensure_ascii=False, indent=2)
        move_artifacts_to_pipeline(task_dir, log_path)
        sys.exit(1)
    except (MaxTurnsError, MaxCostError) as e:
        if isinstance(e, MaxTurnsError):
            fail_status = "MaxTurns"
            logger.warning("Agent hit max_turns limit: %s", e)
        else:
            fail_status = "MaxCost"
            logger.warning("Agent exceeded cost budget: %s", e)

        metadata_path = env_dir / "build_metadata.json"
        run_cost = getattr(e, "cost_usd", 0.0)
        error_metadata = {
            **base_metadata,
            "status": fail_status,
            "error_detail": str(e),
            "special_cases": signals.special_cases,
            "run_cost_usd": run_cost,
            "total_cost_usd": round(previous_total_cost + run_cost, 6),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
        }
        if metadata_path.exists():
            try:
                with open(metadata_path, encoding="utf-8") as f:
                    existing_metadata = json.load(f)
                error_metadata = {**existing_metadata, **error_metadata}
            except Exception:
                pass
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(error_metadata, f, ensure_ascii=False, indent=2)
        logger.info("Saved %s metadata (will not auto-retry): %s", fail_status, metadata_path)

        run_cleanup()
        move_artifacts_to_pipeline(task_dir, log_path)
        sys.exit(1)
    except Exception as e:
        logger.error("Agent failed with error: %s", e, exc_info=True)

        # MUST write metadata even on agent crash, otherwise batch_build will
        # infinitely retry this recording because its status remains "None".
        metadata_path = env_dir / "build_metadata.json"
        run_cost = getattr(e, "cost_usd", 0.0)
        error_metadata = {
            **base_metadata,
            "status": "Exception",
            "error_detail": str(e),
            "special_cases": signals.special_cases,
            "run_cost_usd": run_cost,
            "total_cost_usd": round(previous_total_cost + run_cost, 6),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
        }

        # Merge with existing if any, to preserve maturity_level if crash happened late
        if metadata_path.exists():
            try:
                with open(metadata_path, encoding="utf-8") as f:
                    existing_metadata = json.load(f)
                error_metadata = {**existing_metadata, **error_metadata}
            except Exception:
                pass

        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(error_metadata, f, ensure_ascii=False, indent=2)
        logger.info("Saved exception metadata to prevent infinite retries: %s", metadata_path)

        run_cleanup()
        move_artifacts_to_pipeline(task_dir, log_path)
        sys.exit(1)

    # Check results
    dockerfile_exists = (env_dir / "Dockerfile").exists()
    compose_exists = any((env_dir / name).exists() for name in _KEEP_IN_ENV if name != "Dockerfile")

    run_cleanup()

    # Extract trajectory before building metadata (it's large, stored separately)
    trajectory = agent_result.pop("trajectory", [])

    # Save trajectory (full agent conversation for later analysis)
    trajectory_path = env_dir / "trajectory.jsonl"
    with open(trajectory_path, "w", encoding="utf-8") as f:
        for entry in trajectory:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info("Trajectory saved: %s (%d entries)", trajectory_path, len(trajectory))

    # Query docker image size if build succeeded
    image_size_bytes = None
    if dockerfile_exists:
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image_tag,
                 "--format", "{{.Size}}"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip().isdigit():
                image_size_bytes = int(result.stdout.strip())
                size_mb = image_size_bytes / (1024 * 1024)
                logger.info("Docker image size: %.1f MB", size_mb)
        except Exception as e:
            logger.warning("Could not query image size: %s", e)

    # Save metadata: merge with any existing agent-written metadata rather than
    # overwriting it, so we preserve agent-side fields like base_image,
    # build_attempts, and verification_commands_tested.
    metadata_path = env_dir / "build_metadata.json"
    existing_metadata: dict = {}
    if metadata_path.exists():
        try:
            with open(metadata_path, encoding="utf-8") as f:
                existing_metadata = json.load(f)
            logger.info("Merging with agent-written metadata (%d keys)",
                        len(existing_metadata))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not read existing metadata: %s", e)

    # Determine status: use the new maturity_level from the skill scripts
    maturity_level = existing_metadata.get("maturity_level")

    if maturity_level:
        status = maturity_level
    elif dockerfile_exists:
        # Check if verification_results.json exists to differentiate between
        # an environment that just wasn't verified vs one that failed verification
        verification_exists = (env_dir / "verification_results.json").exists()
        if verification_exists:
            status = "Unverified"
        else:
            status = "Task_Incomplete"
    else:
        status = "NoDockerfile"

    # Split cost: run_cost_usd = this run only; total_cost_usd = cumulative across all runs.
    run_cost_usd = agent_result.pop("total_cost_usd", 0.0)
    total_cost_usd = round(previous_total_cost + run_cost_usd, 6)

    orchestrator_metadata = {
        **base_metadata,
        "image_tag": image_tag,
        "status": status,
        "special_cases": signals.special_cases,
        "dockerfile_created": dockerfile_exists,
        "compose_created": compose_exists,
        "image_size_bytes": image_size_bytes,
        "signals_file": "env_signals.json",
        "trajectory_file": "trajectory.jsonl",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "run_cost_usd": run_cost_usd,
        "total_cost_usd": total_cost_usd,
        **agent_result,
    }

    # Agent fields first, orchestrator fields override on conflict
    metadata = {**existing_metadata, **orchestrator_metadata}

    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    logger.info("Build metadata saved: %s", metadata_path)

    # Move intermediate artifacts to pipeline_artifacts/environment/;
    # only Dockerfile / compose files remain in environment/.
    move_artifacts_to_pipeline(task_dir, log_path)

    # Final summary
    logger.info("")
    logger.info("=" * 70)
    logger.info("AGENT SESSION FINISHED")
    logger.info("=" * 70)
    logger.info("  Status (Maturity): %s", metadata["status"])
    logger.info("  Dockerfile:        %s", "YES" if dockerfile_exists else "NO")
    logger.info("  Compose:           %s", "YES" if compose_exists else "NO")
    logger.info("  Turns:        %d / %d", agent_result["agent_turns"], args.max_turns)
    logger.info("  Run cost:     $%.4f", run_cost_usd)
    logger.info("  Total cost:   $%.4f  (previous: $%.4f)", total_cost_usd, previous_total_cost)
    logger.info("  Time:         %.1fs", agent_result["elapsed_seconds"])
    logger.info("  Session ID:   %s", agent_result.get("session_id") or "N/A")
    logger.info("  Model:        %s", args.model or "(proxy default)")
    logger.info("  Artifacts:    %s", task_dir / "pipeline_artifacts" / "environment")
    logger.info("  Env dir:      %s", env_dir)
    logger.info("=" * 70)

    if not dockerfile_exists:
        logger.warning("No Dockerfile was generated. Check the log for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()
