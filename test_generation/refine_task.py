#!/usr/bin/env python3
"""
Task refinement orchestrator: thin CLI wrapper that triggers the terminal-task-refiner Skill via Claude Code Agent SDK.

This wrapper:
  1. Validates paths
  2. Sends a trigger prompt to Claude Code via the SDK
  3. Logs cost and metadata

Usage:
    python -m task_refinement.refine_task \
        --task-id 188971 --refined-tasks-dir ./data/refined_tasks

Environment variables (all required for Claude Code CLI to work):
    ANTHROPIC_API_KEY=sk-...                    Proxy API key
    ANTHROPIC_BASE_URL=https://...              LiteLLM proxy endpoint
    CLAUDE_CODE_SKIP_SANDBOX=1                  Skip bubblewrap sandbox
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

logger = logging.getLogger("task_refiner")

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
    max_turns: int = 40,
    max_cost_usd: float = 3.0,
    model: str | None = None,
    turn_delay_seconds: float = 0.0,
) -> dict:
    """Run the Claude Code agent with the given prompt."""
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
    logger.info("  model=%s  turn_delay=%.1fs", model or "(proxy default)", turn_delay_seconds)
    logger.info("Agent working directory: %s", cwd)

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
                    logger.warning("[Turn %d] error: %s", assistant_turns, message.error)
                trajectory.append(turn_record)

                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_preview = block.text[:200]
                        if len(block.text) > 200:
                            text_preview += "..."
                        logger.info("[Turn %d] %s", assistant_turns, text_preview)

                if total_cost_usd > max_cost_usd:
                    logger.warning("Cost limit exceeded ($%.4f > $%.2f)", total_cost_usd, max_cost_usd)
                    raise MaxCostError("Cost limit exceeded ($%.4f)" % total_cost_usd, cost_usd=total_cost_usd)

                if turn_delay_seconds > 0:
                    await anyio.sleep(turn_delay_seconds)

            elif isinstance(message, ResultMessage):
                session_id = message.session_id

                sdk_cost = getattr(message, "total_cost_usd", 0.0) or 0.0

                turn_cost = 0.0
                if hasattr(message, "usage") and message.usage:
                    usage_obj = message.usage
                    if isinstance(usage_obj, dict):
                        in_tok = usage_obj.get("input_tokens", 0)
                        out_tok = usage_obj.get("output_tokens", 0)
                    else:
                        in_tok = getattr(usage_obj, "input_tokens", 0)
                        out_tok = getattr(usage_obj, "output_tokens", 0)
                    # Claude Opus 4.6 pricing: $15/M input, $75/M output
                    turn_cost = (in_tok * 15.0 + out_tok * 75.0) / 1_000_000.0

                manual_cost_usd += turn_cost
                total_cost_usd = sdk_cost if sdk_cost > 0.0 else manual_cost_usd

                if total_cost_usd > max_cost_usd:
                    logger.warning("Cost limit exceeded on ResultMessage ($%.4f > $%.2f)", total_cost_usd, max_cost_usd)
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


# ---------------------------------------------------------------------------
# Workspace & pre-flight
# ---------------------------------------------------------------------------


def prepare_agent_workspace(task_dir: Path, project_root: Path) -> Path:
    """Create an isolated workspace for Claude Code. Parallel runs need separate cwds."""
    source_claude_dir = project_root / ".claude"
    if not source_claude_dir.is_dir():
        raise RuntimeError(f"Required .claude directory missing: {source_claude_dir}")

    workspace_dir = Path(
        tempfile.mkdtemp(prefix=".refine_workspace_", dir=str(task_dir))
    )
    workspace_claude_dir = workspace_dir / ".claude"
    workspace_claude_dir.symlink_to(source_claude_dir, target_is_directory=True)
    return workspace_dir


def preflight_docker_check() -> list[str]:
    """Verify Docker is available and healthy."""
    problems: list[str] = []

    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "permission denied" in stderr.lower():
                problems.append("Docker permission denied — add user to 'docker' group.")
            elif "cannot connect" in stderr.lower() or "not running" in stderr.lower():
                problems.append("Docker daemon not running. Start with: sudo systemctl start docker")
            else:
                problems.append(f"docker info failed: {stderr or result.stdout.strip()}")
    except FileNotFoundError:
        problems.append("Docker CLI not found on PATH.")
    except subprocess.TimeoutExpired:
        problems.append("docker info timed out (30s).")

    # Docker Compose check (Harbor uses compose internally for all trials)
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
        problems.append("Docker Compose not found.")
    except subprocess.TimeoutExpired:
        problems.append("docker compose version timed out (10s).")

    # Disk space check
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.DockerRootDir}}"],
            capture_output=True, text=True, timeout=10,
        )
        docker_root = result.stdout.strip() if result.returncode == 0 else "/"
        check_path = Path(docker_root)
        while not os.access(check_path, os.R_OK) and check_path.parent != check_path:
            check_path = check_path.parent
        usage = shutil.disk_usage(check_path)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 5:
            problems.append(f"Low disk space: {free_gb:.1f} GB free. Run: docker system prune -af")
    except Exception:
        pass

    return problems


def _archive_log(task_dir: Path, log_path: str) -> None:
    """Copy the run log into <task_dir>/refinement/ for future --repair runs."""
    try:
        refinement_dir = task_dir / "refinement"
        refinement_dir.mkdir(exist_ok=True)
        shutil.copy2(log_path, str(refinement_dir / "refine.log"))
        logger.info("Log archived to %s", refinement_dir / "refine.log")
    except Exception as e:
        logger.warning("Failed to archive log: %s", e)


def detect_repair_context(task_dir: Path) -> str | None:
    """Detect previous run artifacts and return a prompt section for --repair mode."""
    lines: list[str] = []

    # Previous log
    prev_log = task_dir / "refinement" / "refine.log"
    if prev_log.exists():
        lines.append(f"- Previous run log: `{prev_log}`")

    # Previous metadata
    meta_path = task_dir / "refinement_metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            status = meta.get("status", "unknown")
            iterations = meta.get("iterations", "?")
            oracle = meta.get("oracle_reward", "?")
            nop = meta.get("nop_reward", "?")
            lines.append(f"- Previous status: {status} (iterations={iterations}, oracle={oracle}, nop={nop})")
        except Exception:
            pass

    # Intermediate files at task root (from interrupted run — finalize not reached)
    root_intermediates = []
    for name in ["diagnosis_static.json", "diagnosis_runtime.json",
                 "oracle_trial.json", "nop_trial.json"]:
        if (task_dir / name).exists():
            root_intermediates.append(name)
    for p in sorted(task_dir.glob("partial_trial_*.json")):
        root_intermediates.append(p.name)
    if root_intermediates:
        lines.append(f"- Intermediate files at task root: {', '.join(root_intermediates)}")

    # Archived files in refinement/ (from previous completed run)
    refinement_dir = task_dir / "refinement"
    if refinement_dir.is_dir():
        archived = []
        for name in ["diagnosis_static.json", "diagnosis_runtime.json",
                     "oracle_trial.json", "nop_trial.json"]:
            if (refinement_dir / name).exists():
                archived.append(name)
        for p in sorted(refinement_dir.glob("partial_trial_*.json")):
            archived.append(p.name)
        if archived:
            lines.append(f"- Archived files in refinement/: {', '.join(archived)}")

    if not lines:
        return None

    return "\n".join([
        "",
        "## Repair Mode — Previous Run Context",
        "This task was previously attempted but did not fully converge.",
        "Before starting from scratch, read the previous run artifacts:",
        *lines,
        "",
        "**Instructions:**",
        "1. Read the previous log and any intermediate files to understand what was tried.",
        "2. If diagnosis_static.json exists, skip STEP 1-2 — static diagnosis and initial fixes are done.",
        "3. If trial JSONs exist, read them to understand what passed/failed.",
        "   Do NOT re-run trials unless you made changes that would affect their results.",
        "4. Resume from the earliest incomplete step.",
        "5. Focus on fixing what failed, not re-doing what worked.",
    ])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refine a terminal benchmark task draft via the terminal-task-refiner Skill.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    python -m task_refinement.refine_task --task-id 188971
    python -m task_refinement.refine_task --task-id 188971 --repair
    python -m task_refinement.refine_task --task-id 188971 --model claude-sonnet-4
""",
    )
    parser.add_argument("--task-id", required=True, help="Task ID (e.g. 188971)")
    parser.add_argument(
        "--refined-tasks-dir", default="./data/refined_tasks",
        help="Base directory containing task subdirectories (default: ./data/refined_tasks)",
    )
    parser.add_argument("--max-turns", type=int, default=40, help="Max agent turns (default: 40)")
    parser.add_argument("--max-cost", type=float, default=3.0, help="Hard cost limit in USD (default: 3.0)")
    parser.add_argument("--model", default=None, help="Model name (e.g. claude-sonnet-4)")
    parser.add_argument(
        "--turn-delay", type=float, default=3.0,
        help="Seconds to sleep between agent turns (default: 3.0; 0 to disable)",
    )
    parser.add_argument(
        "--log-dir", default="./task_refinement/logs",
        help="Directory for log files (default: ./task_refinement/logs)",
    )
    parser.add_argument(
        "--repair", action="store_true",
        help="Resume from previous failed/incomplete run — inject previous context into prompt",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    project_root = Path(__file__).resolve().parent.parent
    refined_tasks_dir = Path(args.refined_tasks_dir).resolve()
    task_dir = (refined_tasks_dir / args.task_id).resolve()

    if not task_dir.is_dir():
        print(f"ERROR: Task directory not found: {task_dir}", file=sys.stderr)
        sys.exit(1)

    # Setup logging
    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(log_dir / f"refine_{args.task_id}.log")
    setup_logger(log_path)

    logger.info("=" * 70)
    logger.info("Task Refinement (Skill-based)")
    logger.info("=" * 70)
    logger.info("  Task ID:     %s", args.task_id)
    logger.info("  Task dir:    %s", task_dir)
    logger.info("  Max turns:   %d", args.max_turns)
    logger.info("  Max cost:    $%.2f", args.max_cost)
    logger.info("  Model:       %s", args.model or "(proxy default)")
    logger.info("=" * 70)

    # Pre-flight: env vars
    _env_warnings: list[str] = []
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _env_warnings.append("ANTHROPIC_API_KEY not set.")
    if not os.environ.get("ANTHROPIC_BASE_URL"):
        _env_warnings.append("ANTHROPIC_BASE_URL not set.")
    if not os.environ.get("CLAUDE_CODE_SKIP_SANDBOX"):
        _env_warnings.append("CLAUDE_CODE_SKIP_SANDBOX not set.")
    if _env_warnings:
        logger.warning("Environment variable warnings:")
        for w in _env_warnings:
            logger.warning("  %s", w)

    # Pre-flight: Docker
    logger.info("Running pre-flight Docker checks...")
    docker_problems = preflight_docker_check()
    if docker_problems:
        logger.error("Pre-flight check FAILED:")
        for i, problem in enumerate(docker_problems, 1):
            logger.error("  [%d] %s", i, problem)
        sys.exit(1)
    logger.info("Pre-flight checks passed.")

    # Prepare workspace
    agent_workspace = prepare_agent_workspace(task_dir, project_root)
    logger.info("Agent workspace: %s", agent_workspace)

    # Build trigger prompt
    prompt = "\n".join([
        "## Objective",
        "You are an expert task refiner. Refine the terminal benchmark task",
        f"at `{task_dir}` following the `terminal-task-refiner` skill workflow.",
        "",
        "## Task Parameters",
        f"- Task ID: {args.task_id}",
        f"- Task Directory: {task_dir}",
        "",
        "## Step 0 - Read Input",
        "Before taking any action, read the SKILL.md file:",
        f"  `.claude/skills/terminal-task-refiner/SKILL.md`",
        "Then follow the 6-step workflow described in SKILL.md.",
        "",
        "## Important Notes",
        "- Read ALL reference standards before making any diagnosis.",
        "- Do NOT skip the static diagnosis (STEP 1) - always start there.",
        "- Maximum 5 iterations of the trial loop (STEP 3->4->5).",
        "- Convergence requires ALL: oracle=1, nop=0, all partials=0, instruction quality OK.",
        "- If converged, finalize with --status refined.",
        "- If not converged after 5 iterations, finalize with --status partial.",
        "- If the task is fundamentally broken (unsolvable, unverifiable, requires unavailable external resources), finalize with --status infeasible early (omit --oracle-reward/--nop-reward/--iterations). Infeasible tasks are permanently skipped in future runs.",
    ])

    # Append repair context if --repair and previous artifacts exist
    if args.repair:
        repair_context = detect_repair_context(task_dir)
        if repair_context:
            prompt += repair_context
            logger.info("Repair mode: injecting previous run context into prompt.")
        else:
            logger.info("Repair mode: no previous artifacts found, running from scratch.")

    logger.info("Trigger prompt:\n%s", prompt)

    # Run agent
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
        # Harbor handles Docker cleanup internally (delete=True in TrialConfig).
        # We only need to remove the temporary agent workspace directory.
        shutil.rmtree(agent_workspace, ignore_errors=True)
        logger.info("Agent workspace cleaned up.")

    try:
        agent_result = anyio.run(_agent_task)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (Ctrl+C) or killed by outer timeout")
        run_cleanup()
        metadata_path = task_dir / "refinement_metadata.json"
        interrupted_meta = {
            "status": "failed",
            "error_detail": "Interrupted",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
        }
        if metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text())
                interrupted_meta = {**existing, **interrupted_meta}
            except Exception:
                pass
        metadata_path.write_text(json.dumps(interrupted_meta, indent=2, ensure_ascii=False) + "\n")
        sys.exit(1)
    except (MaxTurnsError, MaxCostError) as e:
        if isinstance(e, MaxTurnsError):
            fail_status = "MaxTurns"
            logger.warning("Agent hit max_turns limit: %s", e)
        else:
            fail_status = "MaxCost"
            logger.warning("Agent exceeded cost budget: %s", e)

        metadata_path = task_dir / "refinement_metadata.json"
        existing = {}
        if metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text())
            except Exception:
                pass
        run_cost = getattr(e, "cost_usd", 0.0)
        prior_cumulative = existing.get("total_cost_usd_cumulative", 0.0) or 0.0
        exhausted_meta = {
            **existing,
            "status": fail_status,
            "error_detail": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
            "total_cost_usd": round(run_cost, 6),
            "total_cost_usd_cumulative": round(prior_cumulative + run_cost, 6),
        }
        metadata_path.write_text(json.dumps(exhausted_meta, indent=2, ensure_ascii=False) + "\n")
        logger.info("Saved %s metadata (will not auto-retry): %s", fail_status, metadata_path)

        _archive_log(task_dir, log_path)
        try:
            from task_refinement.skill.scripts.finalize import archive_intermediate_files
            archived = archive_intermediate_files(task_dir)
            if archived:
                logger.info("Archived %d intermediate items: %s", len(archived), archived)
        except Exception as ae:
            logger.warning("Failed to archive intermediate files: %s", ae)

        run_cleanup()
        for stale_ws in task_dir.glob(".refine_workspace_*"):
            if stale_ws.is_dir():
                shutil.rmtree(stale_ws, ignore_errors=True)
        sys.exit(1)
    except Exception as e:
        logger.error("Agent failed: %s", e, exc_info=True)

        # Write error metadata
        metadata_path = task_dir / "refinement_metadata.json"
        error_metadata = {
            "status": "failed",
            "error_detail": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
        }
        if metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text())
                error_metadata = {**existing, **error_metadata}
            except Exception:
                pass
        metadata_path.write_text(json.dumps(error_metadata, indent=2, ensure_ascii=False) + "\n")
        logger.info("Error metadata saved: %s", metadata_path)

        # Archive log on error too
        _archive_log(task_dir, log_path)

        # Archive intermediate files (normally done by agent's finalize step, skipped on error/timeout)
        try:
            from task_refinement.skill.scripts.finalize import archive_intermediate_files
            archived = archive_intermediate_files(task_dir)
            if archived:
                logger.info("Archived %d intermediate items on error path: %s", len(archived), archived)
        except Exception as ae:
            logger.warning("Failed to archive intermediate files: %s", ae)

        # Clean up stale workspace dirs (current + any orphaned from previous runs)
        run_cleanup()
        for stale_ws in task_dir.glob(".refine_workspace_*"):
            if stale_ws.is_dir():
                shutil.rmtree(stale_ws, ignore_errors=True)
                logger.info("Cleaned stale workspace: %s", stale_ws.name)

        sys.exit(1)

    run_cleanup()
    # Clean up any stale workspace dirs from previous failed runs
    for stale_ws in task_dir.glob(".refine_workspace_*"):
        if stale_ws.is_dir():
            shutil.rmtree(stale_ws, ignore_errors=True)
            logger.info("Cleaned stale workspace: %s", stale_ws.name)

    # Extract and save trajectory into refinement/ (created by agent in STEP6)
    trajectory = agent_result.pop("trajectory", [])
    refinement_dir = task_dir / "refinement"
    refinement_dir.mkdir(exist_ok=True)
    trajectory_path = refinement_dir / "trajectory.jsonl"
    with open(trajectory_path, "w", encoding="utf-8") as f:
        for entry in trajectory:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    logger.info("Trajectory saved: %s (%d entries)", trajectory_path, len(trajectory))

    # Archive log into refinement/
    _archive_log(task_dir, log_path)

    # Save orchestrator metadata (merge with any agent-written metadata)
    metadata_path = task_dir / "refinement_metadata.json"
    existing_metadata: dict = {}
    if metadata_path.exists():
        try:
            existing_metadata = json.loads(metadata_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    orchestrator_metadata = {
        "task_id": args.task_id,
        "trajectory_file": "refinement/trajectory.jsonl",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        **agent_result,
    }
    prior_cumulative = existing_metadata.get("total_cost_usd_cumulative", 0.0) or 0.0
    orchestrator_metadata["total_cost_usd_cumulative"] = round(
        prior_cumulative + agent_result.get("total_cost_usd", 0.0), 6
    )
    metadata = {**existing_metadata, **orchestrator_metadata}
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
    logger.info("Metadata saved: %s", metadata_path)

    # Final summary
    logger.info("")
    logger.info("=" * 70)
    logger.info("REFINEMENT SESSION FINISHED")
    logger.info("=" * 70)
    logger.info("  Status:     %s", metadata.get("status", "unknown"))
    logger.info("  Turns:      %d / %d", agent_result["agent_turns"], args.max_turns)
    logger.info("  Cost:       $%.4f", agent_result["total_cost_usd"])
    logger.info("  Time:       %.1fs", agent_result["elapsed_seconds"])
    logger.info("  Session ID: %s", agent_result.get("session_id") or "N/A")
    logger.info("  Model:      %s", args.model or "(proxy default)")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
