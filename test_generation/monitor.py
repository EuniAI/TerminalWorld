#!/usr/bin/env python3
"""
Task Refinement Monitor — live dashboard for batch_refine.py runs.

Usage:
    python -m task_refinement.monitor [OPTIONS]

Options:
    --root DIR          Project root (default: auto-detected via git)
    --interval N        Refresh interval in seconds (default: 15)
    --once              Print once and exit (no live refresh)
    --task-list FILE    Task list file (default: task_scaling/scaling_tasks_v1.txt)
    --no-clear          Do not clear screen between refreshes (useful for piping)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# ANSI color codes
GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
RED = "\033[1;31m"
CYAN = "\033[36m"
GREY = "\033[90m"
BOLD = "\033[1m"
NC = "\033[0m"

# Status display order and colors
STATUS_ORDER = ["refined", "partial", "failed", "MaxTurns", "MaxCost", "infeasible", "pending"]
STATUS_COLOR = {
    "refined":     GREEN,
    "partial":     YELLOW,
    "failed":      RED,
    "MaxTurns":    YELLOW,
    "MaxCost":     YELLOW,
    "infeasible":  GREY,
    "pending":     GREY,
}

# Statuses that count as "done" (have metadata)
DONE_STATUSES = {"refined", "partial", "failed", "infeasible", "MaxTurns", "MaxCost"}


def find_project_root() -> Path:
    """Find project root via git, falling back to cwd."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(result.stdout.strip())
    except Exception:
        return Path.cwd()


def collect_stats(root: Path, task_list_file: Path) -> dict:
    """Collect stats from refinement_metadata.json files and logs."""
    tasks_dir = root / "data" / "refined_scaled_tasks_v1"
    counts: Counter = Counter()
    costs: dict[str, list[float]] = defaultdict(list)
    elapsed: dict[str, list[float]] = defaultdict(list)
    errors: list[tuple[str, str, float]] = []  # (task_id, status, cost)

    if task_list_file.exists():
        task_ids: list[str] = [
            line.strip()
            for line in task_list_file.read_text().splitlines()
            if line.strip()
        ]
    elif tasks_dir.exists():
        task_ids = [d.name for d in tasks_dir.iterdir() if d.is_dir()]
    else:
        task_ids = []

    total = len(task_ids)

    for task_id in task_ids:
        meta_path = tasks_dir / task_id / "refinement_metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        status = meta.get("status", "unknown")
        cost = meta.get("total_cost_usd_cumulative") or meta.get("total_cost_usd")
        secs = meta.get("elapsed_seconds")
        counts[status] += 1
        if cost:
            costs[status].append(float(cost))
        if secs:
            elapsed[status].append(float(secs))
        if status in ("failed", "partial", "MaxTurns", "MaxCost"):
            errors.append((task_id, status, float(cost or 0)))

    done = sum(counts[s] for s in DONE_STATUSES)
    counts["pending"] = max(0, total - done)

    errors.sort(key=lambda x: x[2], reverse=True)

    return {
        "total": total,
        "counts": counts,
        "costs": costs,
        "elapsed": elapsed,
        "errors": errors,
    }


def fmt_cost(value: float) -> str:
    return f"${value:,.2f}"


def fmt_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def render_dashboard(stats: dict, title: str = "Task Refinement Monitor") -> str:
    total = stats["total"]
    counts = stats["counts"]
    costs = stats["costs"]
    errors = stats["errors"]

    done = sum(counts[s] for s in DONE_STATUSES)
    all_costs = [c for cs in costs.values() for c in cs]
    total_cost = sum(all_costs)
    avg_cost = total_cost / len(all_costs) if all_costs else 0
    pending = counts.get("pending", 0)
    est_remain = avg_cost * pending if avg_cost else 0
    est_total = total_cost + est_remain

    bar_width = 40
    filled = int(bar_width * done / total) if total else 0
    bar = "█" * filled + "░" * (bar_width - filled)
    pct = 100 * done / total if total else 0.0

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sep = "═" * 58

    lines = []
    lines.append(f"{BOLD}{sep}{NC}")
    lines.append(f"  {BOLD}TerminalWorld — {title}{NC}  [{now_str}]")
    lines.append(f"{BOLD}{sep}{NC}")
    lines.append(f"  Total: {total}  |  {GREEN}Done: {done}{NC}  |  Progress: [{bar}] {pct:.1f}%")
    lines.append("")

    # Cost summary
    lines.append(f"  {BOLD}Cost Summary{NC}")
    if all_costs:
        lines.append(f"  Spent      : {BOLD}{YELLOW}{fmt_cost(total_cost)}{NC}  ({len(all_costs)} tasks billed)")
        lines.append(f"  Avg/task   : {fmt_cost(avg_cost)}")
        if pending > 0 and avg_cost:
            lines.append(f"  Est. remain: {YELLOW}{fmt_cost(est_remain)}{NC}  ({pending:,} pending × avg)")
            lines.append(f"  Est. total : {YELLOW}{fmt_cost(est_total)}{NC}")
    else:
        lines.append("  No billing data yet.")
    lines.append("")

    # Status table
    lines.append(f"  {'Status':<14} {'Count':>6}  {'Avg Cost':>9}  {'Total Cost':>10}  {'Avg Time':>9}")
    lines.append(f"  {'─'*14} {'─'*6}  {'─'*9}  {'─'*10}  {'─'*9}")
    for status in STATUS_ORDER:
        count = counts.get(status, 0)
        if count == 0 and status != "pending":
            continue
        color = STATUS_COLOR.get(status, "")
        status_costs = costs.get(status, [])
        status_elapsed = stats["elapsed"].get(status, [])
        avg_c = sum(status_costs) / len(status_costs) if status_costs else None
        tot_c = sum(status_costs) if status_costs else None
        avg_e = sum(status_elapsed) / len(status_elapsed) if status_elapsed else None

        avg_c_str = fmt_cost(avg_c) if avg_c is not None else "—"
        tot_c_str = fmt_cost(tot_c) if tot_c is not None else "—"
        avg_e_str = fmt_duration(avg_e) if avg_e is not None else "—"

        lines.append(
            f"  {color}{status:<14}{NC} {count:>6}  {avg_c_str:>9}  {tot_c_str:>10}  {avg_e_str:>9}"
        )
    lines.append("")

    # Recent failures
    if errors:
        lines.append(f"  {BOLD}Non-refined (last 10, by cost):{NC}")
        for task_id, status, cost in errors[:10]:
            color = STATUS_COLOR.get(status, "")
            lines.append(f"    {color}{task_id:<12}  {status:<10}  {fmt_cost(cost)}{NC}")
    lines.append("")
    lines.append(f"{BOLD}{sep}{NC}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live dashboard for task_refinement/batch_refine.py"
    )
    parser.add_argument("--root", type=Path, default=None, help="Project root directory")
    parser.add_argument("--interval", type=float, default=15.0, help="Refresh interval (seconds)")
    parser.add_argument("--once", action="store_true", help="Print once and exit")
    parser.add_argument(
        "--task-list", type=Path, default=None,
        help="Task list file (default: task_scaling/scaling_tasks_v1.txt)",
    )
    parser.add_argument("--no-clear", action="store_true", help="Do not clear screen")
    args = parser.parse_args()

    root = args.root or find_project_root()
    task_list_file = args.task_list or (root / "task_scaling" / "refined_scaling_tasks_v1.txt")

    def render() -> None:
        stats = collect_stats(root, task_list_file)
        dashboard = render_dashboard(stats)
        if not args.no_clear:
            os.system("clear")
        print(dashboard)
        if not args.once:
            print(f"  Refreshing every {args.interval:.0f}s — Ctrl+C to exit")

    if args.once:
        render()
        return

    try:
        while True:
            render()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")


if __name__ == "__main__":
    main()
