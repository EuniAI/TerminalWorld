#!/usr/bin/env python3
"""
Batch task refinement: run refine_task.py on multiple tasks with concurrency control.

Usage:
    python -m task_refinement.batch_refine \
        [--refined-tasks-dir ./data/refined_tasks] \
        [--n-concurrent 2] [--max-turns 40] [--max-cost 3.0] \
        [--model claude-sonnet-4] [--skip-refined]
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
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("batch_refiner")


def check_proxy_health() -> bool:
    """Return True if the LiteLLM proxy readiness endpoint reports healthy.

    Returns True immediately if ANTHROPIC_BASE_URL is not set (no proxy in use).
    """
    import urllib.request
    from urllib.parse import urlparse

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    if not base_url:
        return True
    parsed = urlparse(base_url)
    health_url = f"{parsed.scheme}://{parsed.netloc}/health/readiness"
    try:
        with urllib.request.urlopen(health_url, timeout=10) as resp:
            body = resp.read().decode()
            return "Unhealthy" not in body and resp.status < 400
    except Exception:
        return False


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


# Statuses that indicate the task hit a hard resource limit.
# Not auto-retried by --repair; use --retry-exhausted to re-queue them.
_EXHAUSTED_STATUSES = {"MaxTurns", "MaxCost"}


def should_skip(task_dir: Path, skip_refined: bool, retry_exhausted: bool = False) -> tuple[bool, str]:
    """Return (should_skip, reason). Infeasible tasks are always skipped."""
    metadata_path = task_dir / "refinement_metadata.json"
    if not metadata_path.exists():
        return False, ""
    try:
        metadata = json.loads(metadata_path.read_text())
        status = metadata.get("status")
        if status == "infeasible":
            return True, "infeasible"
        if skip_refined and status == "refined":
            return True, "already refined"
        if status in _EXHAUSTED_STATUSES and not retry_exhausted:
            return True, status
    except (json.JSONDecodeError, OSError):
        pass
    return False, ""


def discover_tasks(refined_tasks_dir: Path, skip_refined: bool, retry_exhausted: bool = False) -> list[str]:
    """Find all task IDs in the refined tasks directory."""
    task_ids: list[str] = []
    if not refined_tasks_dir.is_dir():
        return task_ids

    _DOCKER_FILES = {"Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
    for child in sorted(refined_tasks_dir.iterdir()):
        if not child.is_dir():
            continue
        # Require all four artifacts: instruction, solution, docker environment, tests
        if not (child / "instruction.md").exists():
            continue
        if not (child / "solution" / "solve.sh").exists():
            continue
        env_dir = child / "environment"
        if not env_dir.is_dir() or not any((env_dir / f).exists() for f in _DOCKER_FILES):
            continue
        test_path = child / "tests" / "test_state.py"
        if not test_path.exists():
            continue
        try:
            src = test_path.read_text(encoding="utf-8", errors="replace")
            compile(src, str(test_path), "exec")
            if not any(line.lstrip().startswith("def test_") for line in src.splitlines()):
                continue
        except SyntaxError:
            continue
        task_id = child.name
        skip, reason = should_skip(child, skip_refined, retry_exhausted=retry_exhausted)
        if skip:
            logger.info("Skipping %s (%s)", task_id, reason)
            continue
        task_ids.append(task_id)

    return task_ids


def run_single_task(
    task_id: str,
    refined_tasks_dir: str,
    max_turns: int,
    max_cost: float,
    model: str | None,
    turn_delay: float,
    log_dir: str,
    timeout: int,
    repair: bool = False,
    retry_exhausted: bool = False,
) -> dict:
    """Run refine_task.py as a subprocess for a single task."""
    cmd = [
        sys.executable, "-m", "task_refinement.refine_task",
        "--task-id", task_id,
        "--refined-tasks-dir", refined_tasks_dir,
        "--max-turns", str(max_turns),
        "--max-cost", str(max_cost),
        "--turn-delay", str(turn_delay),
        "--log-dir", log_dir,
    ]
    if model:
        cmd.extend(["--model", model])
    if repair:
        cmd.append("--repair")

    start = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the entire process group (refine_task.py + any subprocesses)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                proc.kill()
            try:
                proc.communicate()  # drain pipes so OS reclaims FDs
            except Exception:
                pass
            raise

        elapsed = time.time() - start
        return {
            "task_id": task_id,
            "returncode": proc.returncode,
            "elapsed_seconds": round(elapsed, 2),
            "error": None,
        }

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        # Stamp failed/Timeout status so monitor sees it and the task is re-queued
        task_dir = Path(refined_tasks_dir) / task_id
        metadata_path = task_dir / "refinement_metadata.json"
        existing: dict = {}
        if metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text())
            except Exception:
                pass
        timeout_meta = {
            **existing,
            "status": "failed",
            "error_detail": f"Killed by outer timeout after {timeout}s",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.write_text(json.dumps(timeout_meta, indent=2, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return {
            "task_id": task_id,
            "returncode": -1,
            "elapsed_seconds": round(elapsed, 2),
            "error": f"Timeout after {timeout}s",
        }

    except Exception as e:
        elapsed = time.time() - start
        return {
            "task_id": task_id,
            "returncode": -1,
            "elapsed_seconds": round(elapsed, 2),
            "error": str(e),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch-refine TerminalWorld tasks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--refined-tasks-dir", default="./data/refined_tasks",
        help="Base directory containing task subdirectories (default: ./data/refined_tasks)",
    )
    parser.add_argument("--n-concurrent", type=int, default=2, help="Max concurrent tasks (default: 2)")
    parser.add_argument("--max-turns", type=int, default=40, help="Max agent turns per task (default: 40)")
    parser.add_argument("--max-cost", type=float, default=3.0, help="Max cost per task in USD (default: 3.0)")
    parser.add_argument("--model", default=None, help="Model name (e.g. claude-sonnet-4)")
    parser.add_argument("--turn-delay", type=float, default=3.0, help="Delay between agent turns (default: 3.0)")
    parser.add_argument("--skip-refined", action="store_true", help="Skip tasks with status=refined")
    parser.add_argument("--repair", action="store_true",
                        help="Retry failed/incomplete tasks with previous run context (implies --skip-refined). "
                             "Skips MaxTurns/MaxCost tasks (use --retry-exhausted to include those).")
    parser.add_argument("--retry-exhausted", action="store_true",
                        help="Like --repair but also retries MaxTurns/MaxCost tasks. Use when willing to "
                             "spend more turns/budget on previously exhausted tasks.")
    parser.add_argument("--task-ids", nargs="*", default=None, help="Specific task IDs to refine (default: all)")
    parser.add_argument(
        "--log-dir", default="./task_refinement/logs",
        help="Directory for log files (default: ./task_refinement/logs)",
    )
    parser.add_argument(
        "--subprocess-timeout", type=int, default=7200,
        help="Timeout per subprocess in seconds (default: 7200)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(log_dir / f"batch_refine_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    setup_logger(log_path)

    refined_tasks_dir = Path(args.refined_tasks_dir).resolve()
    if not refined_tasks_dir.is_dir():
        logger.error("Refined tasks directory not found: %s", refined_tasks_dir)
        sys.exit(1)

    # Pre-flight: Docker check (fail fast before launching N subprocesses)
    from task_refinement.refine_task import preflight_docker_check

    docker_problems = preflight_docker_check()
    if docker_problems:
        logger.error("Pre-flight check FAILED:")
        for i, problem in enumerate(docker_problems, 1):
            logger.error("  [%d] %s", i, problem)
        sys.exit(1)

    if not check_proxy_health():
        logger.error("Proxy pre-flight check FAILED: %s is not ready.",
                     os.environ.get("ANTHROPIC_BASE_URL", "(ANTHROPIC_BASE_URL not set)"))
        logger.error("Check LiteLLM proxy status before starting a batch.")
        sys.exit(1)

    logger.info("Pre-flight checks passed (Docker + Proxy).")

    # --repair/--retry-exhausted implies --skip-refined
    retry_exhausted = getattr(args, "retry_exhausted", False)
    skip_refined = args.skip_refined or args.repair or retry_exhausted

    # Discover tasks
    if args.task_ids:
        task_ids = args.task_ids
    else:
        task_ids = discover_tasks(refined_tasks_dir, skip_refined, retry_exhausted=retry_exhausted)

    if not task_ids:
        logger.info("No tasks to refine.")
        return

    logger.info("=" * 70)
    logger.info("Batch Task Refinement")
    logger.info("=" * 70)
    logger.info("  Tasks:           %d", len(task_ids))
    logger.info("  Concurrent:      %d", args.n_concurrent)
    logger.info("  Max turns:       %d", args.max_turns)
    logger.info("  Max cost:        $%.2f", args.max_cost)
    logger.info("  Model:           %s", args.model or "(proxy default)")
    logger.info("  Repair:          %s", args.repair)
    logger.info("  Retry exhausted: %s", retry_exhausted)
    logger.info("  Task IDs:        %s", ", ".join(task_ids[:10]) + ("..." if len(task_ids) > 10 else ""))
    logger.info("=" * 70)

    # Run tasks with concurrency control
    results: list[dict] = []
    start_time = time.time()

    # Proxy abort: if N consecutive fast failures occur, check proxy health and abort.
    # "Fast failure" (< 30s) means the agent never got a real response — likely a proxy outage.
    _PROXY_ABORT_THRESHOLD = 5
    _consecutive_proxy_failures = 0

    with ProcessPoolExecutor(max_workers=args.n_concurrent) as executor:
        futures = {
            executor.submit(
                run_single_task,
                task_id=tid,
                refined_tasks_dir=str(refined_tasks_dir),
                max_turns=args.max_turns,
                max_cost=args.max_cost,
                model=args.model,
                turn_delay=args.turn_delay,
                log_dir=str(log_dir),
                timeout=args.subprocess_timeout,
                repair=args.repair or retry_exhausted,
                retry_exhausted=retry_exhausted,
            ): tid
            for tid in task_ids
        }

        for future in as_completed(futures):
            tid = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = {
                    "task_id": tid,
                    "returncode": -1,
                    "elapsed_seconds": 0,
                    "error": str(e),
                }
            results.append(result)

            status_str = "OK" if result["returncode"] == 0 else f"FAIL (rc={result['returncode']})"
            logger.info(
                "[%d/%d] %s: %s (%.1fs)",
                len(results), len(task_ids), tid, status_str, result["elapsed_seconds"],
            )

            # Track consecutive fast failures as a proxy-outage signal.
            if result["returncode"] != 0 and result["elapsed_seconds"] < 30:
                _consecutive_proxy_failures += 1
            else:
                _consecutive_proxy_failures = 0

            if _consecutive_proxy_failures >= _PROXY_ABORT_THRESHOLD:
                if not check_proxy_health():
                    logger.error(
                        "Proxy health check FAILED after %d consecutive fast failures (<30s). Aborting batch.",
                        _consecutive_proxy_failures,
                    )
                    logger.error("Restart LiteLLM proxy, then re-run with --repair to continue.")
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                _consecutive_proxy_failures = 0  # proxy is healthy; reset counter

    total_elapsed = time.time() - start_time

    # Read final status from each task's metadata (always check metadata, even after subprocess error,
    # because the agent may have written status before the process was killed by outer timeout)
    stats = {"refined": 0, "partial": 0, "failed": 0, "infeasible": 0, "skipped": 0, "error": 0}
    for r in results:
        metadata_path = refined_tasks_dir / r["task_id"] / "refinement_metadata.json"
        if metadata_path.exists():
            try:
                meta = json.loads(metadata_path.read_text())
                status = meta.get("status", "unknown")
                if status in stats:
                    stats[status] += 1
                else:
                    stats["error"] += 1
            except Exception:
                stats["error"] += 1
        else:
            stats["error"] += 1

    # Summary
    logger.info("")
    logger.info("=" * 70)
    logger.info("BATCH REFINEMENT COMPLETE")
    logger.info("=" * 70)
    logger.info("  Total tasks: %d", len(task_ids))
    logger.info("  Refined:     %d", stats["refined"])
    logger.info("  Partial:     %d", stats["partial"])
    logger.info("  Failed:      %d", stats["failed"])
    logger.info("  Infeasible:  %d", stats["infeasible"])
    logger.info("  Error:       %d", stats["error"])
    logger.info("  Total time:  %.1fs", total_elapsed)
    logger.info("=" * 70)

    # Write batch summary
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_count": len(task_ids),
        "stats": stats,
        "total_elapsed_seconds": round(total_elapsed, 2),
        "results": results,
    }
    summary_path = log_dir / f"batch_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    logger.info("Batch summary: %s", summary_path)


if __name__ == "__main__":
    main()
