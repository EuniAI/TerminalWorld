#!/usr/bin/env python3
"""
Batch environment builder: run build_environment.py for multiple recording IDs
in parallel using a thread pool (each build is a subprocess with its own event
loop).

Run modes (mutually exclusive):
    (default)        Resume: STRICT mode. Only runs recordings that have NEVER
                     been run (status is completely missing/None). Skips anything
                     that has been attempted, even if it failed.
    --repair         Run not-started ones PLUS retry those that encountered errors
                     (Exception, NoDockerfile, etc). Skips correctly finished ones.
    --retry-failed   Run ONLY previously-failed ones. Skips not-started.
    --refresh        Re-run ALL recordings regardless of existing results.

Usage:
    # Resume (default): safest, only process virgin recordings
    python -m environment_building.batch_build --input output/filtered_recordings.json

    # Repair: resume virgin recordings + retry crashes
    python -m environment_building.batch_build --input output/filtered_recordings.json --repair

    # Re-run everything
    python -m environment_building.batch_build --input output/filtered_recordings.json --refresh

    # Force a clean environment/ reset before rebuilding
    python -m environment_building.batch_build --input output/filtered_recordings.json \
        --refresh --workspace-policy fresh

    # From a plain text file (one ID per line, blank lines / # comments ignored):
    python -m environment_building.batch_build --id-file ids.txt

    # Or pass IDs directly:
    python -m environment_building.batch_build --ids 1060 1061 1062

    # Limit concurrency and pass through build options:
    python -m environment_building.batch_build --input output/filtered_recordings.json \\
        --workers 4 \\
        --recordings-dir ./data/recordings \\
        --max-turns 30 \\
        --model claude-sonnet-4 \\
        --turn-delay 3.0 \\
        --log-dir ./environment_building/logs
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import shutil
from pathlib import Path

logger = logging.getLogger("batch_build")

# Project root = parent of this file's directory (i.e. TerminalWorld/)
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logger(log_path: str | None = None) -> None:
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


def _determine_mode(args: argparse.Namespace) -> str:
    if args.refresh:
        return "refresh"
    if args.repair:
        return "repair"
    if getattr(args, "retry_exhausted", False):
        return "repair_exhausted"
    if args.retry_failed:
        return "retry_failed"
    return "resume"


def _slugify_label(value: str) -> str:
    safe = []
    for ch in value:
        if ch.isalnum():
            safe.append(ch.lower())
        else:
            safe.append("-")
    slug = "".join(safe).strip("-")
    return slug or "default"


def _build_run_label(model: str | None) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    model_label = _slugify_label(model or "proxy-default")
    return f"{model_label}_{timestamp}"


# ---------------------------------------------------------------------------
# Single build
# ---------------------------------------------------------------------------


def get_existing_status(recording_id: str, recordings_dir: str) -> str | None:
    """Return the existing build status for a recording, or None if not started."""
    metadata_path = (
        Path(recordings_dir) / recording_id / "pipeline_artifacts" / "environment" / "build_metadata.json"
    )
    if not metadata_path.exists():
        return None
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        # Return status which now includes maturity levels
        s = data.get("status")
        return str(s) if s is not None else None
    except Exception:
        return None  # treat unreadable metadata as not started



_BUILD_SUBPROCESS_TIMEOUT_SECONDS = 7200

# Files produced by the pipeline agent that belong in pipeline_artifacts, not in
# the docker build context.  Only these are moved out of environment/ on kill.
# Everything else (entrypoint scripts, data files, etc.) is part of the build
# context and must stay in environment/ so `docker build` can find it later.
_PIPELINE_ARTIFACT_NAMES = {
    "build_metadata.json",
    "env_signals.json",
    "verification_results.json",
    "trajectory.jsonl",
    "artifact_history.jsonl",
    ".build_attempt_count",
    "diagnostic_commands.json",
    "diagnostic_cmds.json",
    "target_commands.json",
    "target_cmds.json",
    "verify_commands.json",
    "verify_cmds.json",
    "verify_diagnostic.json",
    "verify_target.json",
    "verification_commands.json",
    "project_detection.json",
}
_PIPELINE_ARTIFACT_PREFIXES = ("project_detection_",)
# Match by suffix only — prefix rules are intentionally omitted to avoid
# accidentally matching build context files like build_script.sh or build_config.yaml.
# Log files named build_{task_id}.log are already covered by the .log suffix.
_PIPELINE_ARTIFACT_SUFFIXES = (".log",)


def _is_pipeline_artifact(name: str) -> bool:
    if name in _PIPELINE_ARTIFACT_NAMES:
        return True
    if any(name.startswith(p) for p in _PIPELINE_ARTIFACT_PREFIXES):
        return True
    if any(name.endswith(s) for s in _PIPELINE_ARTIFACT_SUFFIXES):
        return True
    return False


def _move_artifacts_after_kill(task_dir: Path) -> None:
    """Best-effort: after SIGKILL, move known pipeline artifacts to
    pipeline_artifacts/environment/ and stamp status=Timeout so --repair
    can re-queue the task.

    Only files explicitly identified as pipeline artifacts are moved out;
    all other files (build context, data files, scripts referenced by COPY, etc.)
    remain in environment/ so subsequent docker builds still work.
    """
    env_dir = task_dir / "environment"
    pipeline_dir = task_dir / "pipeline_artifacts" / "environment"
    if not env_dir.exists():
        return
    pipeline_dir.mkdir(parents=True, exist_ok=True)

    # Build merged metadata: use maturity_level as status if agent already wrote one,
    # otherwise stamp Timeout so --repair can re-queue the task.
    env_meta_path = env_dir / "build_metadata.json"
    existing_env: dict = {}
    if env_meta_path.exists():
        try:
            existing_env = json.loads(env_meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    pipeline_meta_path = pipeline_dir / "build_metadata.json"
    # Only write metadata if pipeline_dir doesn't already have a successful terminal result.
    # Either way, we still proceed to clean artifact files out of env_dir below.
    should_write_meta = True
    if pipeline_meta_path.exists():
        try:
            prev = json.loads(pipeline_meta_path.read_text(encoding="utf-8"))
            if prev.get("status") not in (None, "Timeout", "Exception", "Interrupted"):
                should_write_meta = False
        except Exception:
            pass

    if should_write_meta:
        maturity = existing_env.get("maturity_level")
        stamped_status = maturity if maturity else "Timeout"
        kill_meta = {**existing_env, "status": stamped_status}
        with open(pipeline_meta_path, "w", encoding="utf-8") as f:
            json.dump(kill_meta, f, ensure_ascii=False, indent=2)

    # Move known pipeline artifact files to pipeline_artifacts/environment/.
    # These files don't belong in the docker build context.
    # build_metadata.json was already written (or skipped) above; just remove the
    # original from env_dir. For all other artifacts, move them.
    # Build context files (anything not matching _is_pipeline_artifact) are left alone.
    for child in sorted(env_dir.iterdir()):
        if not _is_pipeline_artifact(child.name):
            continue
        if child.name == "build_metadata.json":
            # Merged version already handled above; remove original.
            try:
                child.unlink()
            except Exception:
                pass
            continue
        dest = pipeline_dir / child.name
        try:
            # If dest is an existing directory, remove it first so shutil.move
            # replaces it rather than nesting inside it.
            if dest.exists() and dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(str(dest))
            # Always overwrite — a later run's artifacts supersede an earlier one's.
            shutil.move(str(child), str(dest))
        except Exception:
            pass


def build_one(recording_id: str, build_args: list[str], recordings_dir: str) -> dict:
    """Invoke build_environment for one recording ID and return a result dict."""
    cmd = [
        sys.executable, "-m", "environment_building.build_environment",
        "--recording-id", recording_id,
        *build_args,
    ]
    start = time.time()
    logger.info("[%s] Starting build: %s", recording_id, " ".join(cmd))

    popen_proc = None
    try:
        popen_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=_PROJECT_ROOT,
            start_new_session=True,
        )
        try:
            stdout, stderr = popen_proc.communicate(timeout=_BUILD_SUBPROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise
        elapsed = round(time.time() - start, 1)
        success = popen_proc.returncode == 0

        # Read the newly generated status to enrich the summary
        final_status = get_existing_status(recording_id, recordings_dir) or "Unknown"

        # A run is considered "handled" if it exited 0, OR if it safely reached a
        # terminal status like "blocked" (which intentionally exits 1 to indicate skip).
        # We don't want batch_build to treat graceful skips as crashes.
        is_handled = success or (final_status in _TERMINAL_STATUSES)

        if success:
            logger.info("[%s] DONE (%.1fs, exit 0, status: %s)", recording_id, elapsed, final_status)
        else:
            level = logging.INFO if is_handled else logging.WARNING
            logger.log(
                level,
                "[%s] %s (%.1fs, exit %d, status: %s)\n--- stdout tail ---\n%s\n--- stderr tail ---\n%s",
                recording_id, "HANDLED" if is_handled else "FAILED", elapsed, popen_proc.returncode, final_status,
                _tail(stdout), _tail(stderr),
            )

        return {
            "recording_id": recording_id,
            "success": is_handled,
            "status": final_status,
            "returncode": popen_proc.returncode,
            "elapsed_seconds": elapsed,
            "stdout_tail": _tail(stdout),
            "stderr_tail": _tail(stderr),
        }

    except subprocess.TimeoutExpired:
        elapsed = round(time.time() - start, 1)
        logger.error(
            "[%s] TIMEOUT after %.1fs (limit=%ds) — killing subprocess tree",
            recording_id, elapsed, _BUILD_SUBPROCESS_TIMEOUT_SECONDS,
        )
        # Kill the entire process group (build_environment.py + claude CLI + any
        # docker subprocesses it spawned).  start_new_session=True above ensures
        # the child has its own session/pgid, so killpg only hits that tree.
        if popen_proc is not None:
            try:
                os.killpg(popen_proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    popen_proc.kill()
                except Exception:
                    pass
            try:
                popen_proc.communicate()  # drain pipes so OS reclaims FDs
            except Exception:
                pass
        # Best-effort: stamp Timeout status and move artifacts so the monitor
        # can see the partial result and --repair will re-queue this task.
        try:
            _move_artifacts_after_kill(Path(recordings_dir) / recording_id)
        except Exception as move_err:
            logger.warning("[%s] Could not move artifacts after kill: %s", recording_id, move_err)
        return {
            "recording_id": recording_id,
            "success": False,
            "status": "Timeout",
            "returncode": -1,
            "elapsed_seconds": elapsed,
            "error": f"Subprocess timed out after {_BUILD_SUBPROCESS_TIMEOUT_SECONDS}s",
        }

    except Exception as exc:
        elapsed = round(time.time() - start, 1)
        logger.error("[%s] EXCEPTION after %.1fs: %s", recording_id, elapsed, exc)
        return {
            "recording_id": recording_id,
            "success": False,
            "status": "Exception",
            "returncode": -1,
            "elapsed_seconds": elapsed,
            "error": str(exc),
        }


def _tail(text: str, lines: int = 30) -> str:
    """Return the last `lines` lines of a string."""
    parts = text.strip().splitlines()
    return "\n".join(parts[-lines:]) if parts else ""


# ---------------------------------------------------------------------------
# Status filtering
# ---------------------------------------------------------------------------

# Terminal statuses are those that represent a "completed" execution
# (even if the result was a failure to build a high maturity level)
_TERMINAL_STATUSES = {
    # Old legacy statuses
    "success",
    "blocked",
    # New maturity levels
    "Buildability",
    "Launchability",
    "Testability",
    "Runnability",
    "Replayability",
    "Reproducibility",
}

# These are "problematic" or "incomplete" states that --repair targets.
# MaxTurns / MaxCost are intentionally excluded: these tasks ran to budget
# exhaustion and are low-ROI to retry without changing parameters.
# Use --retry-exhausted (or manually reset status) to re-queue them.
_RETRYABLE_STATUSES = {
    "NoDockerfile",
    "Task_Incomplete",
    "Unverified",
    "Exception",
    "Timeout",
    "failed",  # Legacy
}

# Statuses that indicate the task hit a hard resource limit and should not be
# automatically retried by --repair.
_EXHAUSTED_STATUSES = {
    "MaxTurns",
    "MaxCost",
}

def filter_ids(
    ids: list[str],
    recordings_dir: str,
    mode: str,
) -> tuple[list[str], dict[str, int]]:
    """Filter recording IDs according to run mode.

    Args:
        ids: Full list of recording IDs.
        recordings_dir: Base recordings directory.
        mode: One of "resume" | "repair" | "retry_failed" | "refresh".

    Returns:
        (filtered_ids, skip_counts) where skip_counts has keys tracking skipped statuses.
    """
    if mode == "refresh":
        return ids, {}

    statuses = {rid: get_existing_status(rid, recordings_dir) for rid in ids}
    skip_counts: dict[str, int] = {}
    filtered: list[str] = []

    for rid in ids:
        s = statuses[rid]

        # Track skipping reasons
        if s not in skip_counts and s is not None:
            skip_counts[s] = 0

        if "not_started" not in skip_counts:
            skip_counts["not_started"] = 0

        # Mode logic
        if mode == "resume":
            # ONLY run if it has NEVER been run (status is None).
            # If it has ANY status (including Exception or NoDockerfile), skip it.
            if s is None:
                filtered.append(rid)
            else:
                skip_counts[s] += 1

        elif mode == "repair":
            # Run if not started OR if the status indicates an incomplete/failed run.
            # MaxTurns/MaxCost are skipped unless --retry-exhausted is passed.
            if s is None:
                filtered.append(rid)
            elif s in _RETRYABLE_STATUSES:
                filtered.append(rid)
            else:
                skip_counts[s] += 1

        elif mode == "repair_exhausted":
            # Like repair, but ALSO retries MaxTurns/MaxCost tasks.
            if s is None:
                filtered.append(rid)
            elif s in _RETRYABLE_STATUSES or s in _EXHAUSTED_STATUSES:
                filtered.append(rid)
            else:
                skip_counts[s] += 1

        elif mode == "retry_failed":
            # ONLY run those that previously failed/had problems. Skip not-started.
            if s is None:
                skip_counts["not_started"] += 1
            elif s in _RETRYABLE_STATUSES:
                filtered.append(rid)
            else:
                skip_counts[s] += 1

    return filtered, skip_counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch-build Docker environments for multiple recording IDs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    id_group = parser.add_mutually_exclusive_group(required=True)
    id_group.add_argument(
        "--input",
        help="JSON file whose keys are recording IDs "
             "(e.g. output/filtered_recordings.json).",
    )
    id_group.add_argument(
        "--id-file",
        help="Text file with one recording ID per line "
             "(blank lines and lines starting with # are ignored).",
    )
    id_group.add_argument(
        "--ids",
        nargs="+",
        metavar="ID",
        help="Recording IDs to build (space-separated).",
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--repair",
        action="store_true",
        help="Run not-started recordings AND retry ones with incomplete/failed statuses (e.g. Exception). "
             "Skips MaxTurns/MaxCost tasks (use --retry-exhausted to include those).",
    )
    mode_group.add_argument(
        "--retry-exhausted",
        action="store_true",
        help="Like --repair but also retries MaxTurns/MaxCost tasks. Use when willing to spend "
             "more turns/budget on previously exhausted tasks.",
    )
    mode_group.add_argument(
        "--retry-failed",
        action="store_true",
        help="Run ONLY previously-failed recordings (skips not-started ones).",
    )
    mode_group.add_argument(
        "--refresh",
        action="store_true",
        help="Re-run all recordings regardless of existing results.",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Number of parallel build workers (default: 2).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Only process the first N recording IDs from --input (ignored for --ids/--id-file).",
    )

    # Pass-through args for build_environment.py
    parser.add_argument("--recordings-dir", default="./data/scaled_tasks_v1")
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--model", default=None)
    parser.add_argument("--turn-delay", type=float, default=3.0)
    parser.add_argument(
        "--log-dir",
        default="./environment_building/logs",
        help="Directory for per-build log files and the batch summary "
             "(default: ./environment_building/logs)",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=1.5,
        help="Hard cost limit in USD per recording. Agent is killed if exceeded (default: 1.5)",
    )
    parser.add_argument(
        "--workspace-policy",
        choices=("auto", "fresh", "reuse"),
        default="auto",
        help=(
            "How to handle each recording's environment/ directory before a build: "
            "'fresh' clears it, 'reuse' keeps prior artifacts, 'auto' maps from mode "
            "(default: auto)."
        ),
    )

    return parser.parse_args()


def load_ids(args: argparse.Namespace) -> list[str]:
    if args.ids:
        return args.ids
    if args.input:
        path = Path(args.input)
        if not path.exists():
            print(f"ERROR: Input file not found: {path}", file=sys.stderr)
            sys.exit(1)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            print(f"ERROR: Expected a JSON object (dict) in {path}", file=sys.stderr)
            sys.exit(1)
        ids = list(data.keys())
        if args.limit is not None:
            ids = ids[:args.limit]
        return ids
    # --id-file
    path = Path(args.id_file)
    if not path.exists():
        print(f"ERROR: ID file not found: {path}", file=sys.stderr)
        sys.exit(1)
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ids.append(line)
    return ids


def build_passthrough_args(args: argparse.Namespace) -> list[str]:
    """Build the list of extra CLI flags to forward to build_environment.py."""
    extra: list[str] = ["--recordings-dir", args.recordings_dir,
                        "--max-turns", str(args.max_turns),
                        "--max-cost", str(args.max_cost),
                        "--turn-delay", str(args.turn_delay),
                        "--log-dir", args.log_dir,
                        "--run-mode", _determine_mode(args),
                        "--workspace-policy", args.workspace_policy]
    if args.model:
        extra += ["--model", args.model]
    return extra


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    mode = _determine_mode(args)
    run_label = _build_run_label(args.model)

    batch_log = str(log_dir / f"batch_build_{run_label}.log")
    setup_logger(batch_log)

    all_ids = load_ids(args)
    if not all_ids:
        logger.error("No recording IDs found.")
        sys.exit(1)

    ids, skip_counts = filter_ids(all_ids, args.recordings_dir, mode)

    passthrough = build_passthrough_args(args)

    logger.info("=" * 70)
    logger.info("Batch Build  [mode: %s]", mode)
    logger.info("=" * 70)
    logger.info("  Total input IDs: %d", len(all_ids))
    if skip_counts:
        # Dynamically log all skipped statuses (new maturity levels)
        for s_key, s_count in sorted(skip_counts.items()):
            if s_count > 0:
                logger.info("  Skipped (%-15s): %d", s_key, s_count)
    logger.info("  To run:  %d", len(ids))
    logger.info("  Workers: %d", args.workers)
    logger.info("  Model:   %s", args.model or "(proxy default)")
    logger.info("  Workspace policy: %s", args.workspace_policy)
    logger.info("  Run ID:  %s", run_label)
    logger.info("  Log:     %s", batch_log)
    logger.info("=" * 70)

    if not ids:
        logger.info("Nothing to run — all recordings already handled.")
        sys.exit(0)

    batch_start = time.time()
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(build_one, rid, passthrough, args.recordings_dir): rid for rid in ids}
        for future in as_completed(futures):
            results.append(future.result())

    # Sort by original order for readability
    id_order = {rid: i for i, rid in enumerate(ids)}
    results.sort(key=lambda r: id_order.get(r["recording_id"], 9999))

    # Summary
    total_elapsed = round(time.time() - batch_start, 1)
    succeeded = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    # Track distribution of final statuses
    status_distribution: dict[str, int] = {}
    total_batch_cost = 0.0

    for r in results:
        status = r.get("status", "Unknown")
        status_distribution[status] = status_distribution.get(status, 0) + 1

        # Add up costs if available in the final status/metadata read
        # batch_build doesn't natively read it, so let's parse it quickly
        try:
            metadata_path = Path(args.recordings_dir) / r["recording_id"] / "pipeline_artifacts" / "environment" / "build_metadata.json"
            if metadata_path.exists():
                with open(metadata_path, encoding="utf-8") as f:
                    meta = json.load(f)
                total_batch_cost += meta.get("total_cost_usd", 0.0)
        except Exception:
            pass

    logger.info("")
    logger.info("=" * 70)
    logger.info("BATCH COMPLETE — %d exited clean, %d crashed  (wall time: %.1fs)",
                len(succeeded), len(failed), total_elapsed)
    logger.info("TOTAL COST FOR THIS RUN: $%.4f", total_batch_cost)
    logger.info("=" * 70)

    for r in results:
        mark = "OK" if r["success"] else "FAIL"
        logger.info("  [%s] %-8s status: %-15s (%.1fs)", mark, r["recording_id"], r.get("status", "Unknown"), r["elapsed_seconds"])

    if failed:
        logger.warning("Crashed IDs: %s", [r["recording_id"] for r in failed])

    logger.info("-" * 70)
    logger.info("Maturity / Status Distribution (Current Run):")
    for stat, count in sorted(status_distribution.items(), key=lambda x: x[1], reverse=True):
        logger.info("  %-18s: %d", stat, count)

    # Save summary JSON
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "total_input": len(all_ids),
        "skipped": skip_counts,
        "ran": len(ids),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "wall_seconds": total_elapsed,
        "workers": args.workers,
        "workspace_policy": args.workspace_policy,
        "results": results,
    }
    summary_path = log_dir / f"batch_summary_{run_label}.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    logger.info("Summary saved: %s", summary_path)

    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
