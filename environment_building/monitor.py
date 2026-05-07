#!/usr/bin/env python3
"""
Environment Building Monitor — live dashboard for batch_build.py runs.

Usage:
    python -m environment_building.monitor [OPTIONS]

Options:
    --root DIR          Project root (default: auto-detected via git)
    --interval N        Refresh interval in seconds (default: 15)
    --once              Print once and exit (no live refresh)
    --task-list FILE    Text file of recording IDs to use as total (one per line).
                        If omitted, counts all dirs in data/recordings/.
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

# Maturity levels ordered best → worst for display
MATURITY_ORDER = [
    "Reproducibility",
    "Replayability",
    "Runnability",
    "Testability",
    "Launchability",
    "Buildability",
    # legacy / error states
    "success",
    "blocked",
    "Task_Incomplete",
    "Exception",
    "NoDockerfile",
    "failed",
    # monitor-derived
    "pending",
]

MATURITY_COLOR = {
    "Reproducibility": GREEN,
    "Replayability":   GREEN,
    "Runnability":     GREEN,
    "Testability":     YELLOW,
    "Launchability":   YELLOW,
    "Buildability":    YELLOW,
    "success":         GREEN,
    "blocked":         GREY,
    "Task_Incomplete": RED,
    "Exception":       RED,
    "NoDockerfile":    RED,
    "failed":          RED,
    "pending":         GREY,
}

# Statuses that count as "done" (have metadata, terminal state)
TERMINAL_STATUSES = {
    "Reproducibility", "Replayability", "Runnability",
    "Testability", "Launchability", "Buildability",
    "success", "blocked", "Task_Incomplete", "Exception", "NoDockerfile", "failed",
}

# Statuses considered "good" (high maturity) for summary line
GOOD_STATUSES = {"Reproducibility", "Replayability", "Runnability", "success"}


def find_project_root() -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(result.stdout.strip())
    except Exception:
        return Path.cwd()


def collect_stats(root: Path, task_list_file: Path | None) -> dict:
    recordings_dir = root / "data" / "scaled_tasks_v1"

    counts: Counter = Counter()
    costs: dict[str, list[float]] = defaultdict(list)
    elapsed: dict[str, list[float]] = defaultdict(list)
    errors: list[tuple[str, str, float]] = []  # (rec_id, status, cost)

    if task_list_file and task_list_file.exists():
        rec_ids: list[str] = [
            line.strip()
            for line in task_list_file.read_text().splitlines()
            if line.strip()
        ]
        total_source = f"from {task_list_file.name}"
    elif recordings_dir.exists():
        rec_ids = [d.name for d in recordings_dir.iterdir() if d.is_dir()]
        total_source = "all task dirs"
    else:
        rec_ids = []
        total_source = "all task dirs"

    total = len(rec_ids)
    for rec_id in rec_ids:
        # pipeline_artifacts/ is the final resting place; fall back to environment/
        # for tasks still running (agent writes status there before moving artifacts).
        meta_path = recordings_dir / rec_id / "pipeline_artifacts" / "environment" / "build_metadata.json"
        if not meta_path.exists():
            meta_path = recordings_dir / rec_id / "environment" / "build_metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        level = meta.get("maturity_level") or meta.get("status") or "unknown"
        cost = meta.get("total_cost_usd")
        secs = meta.get("elapsed_seconds")
        counts[level] += 1
        if cost:
            costs[level].append(float(cost))
        if secs:
            elapsed[level].append(float(secs))
        if level in ("Exception", "Task_Incomplete", "NoDockerfile", "failed", "blocked"):
            errors.append((rec_id, level, float(cost or 0)))

    done = sum(counts[s] for s in TERMINAL_STATUSES)
    counts["pending"] = max(0, total - done)

    errors.sort(key=lambda x: x[2], reverse=True)

    return {
        "total": total,
        "total_source": total_source,
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


def render_dashboard(stats: dict) -> str:
    total = stats["total"]
    counts = stats["counts"]
    costs = stats["costs"]
    errors = stats["errors"]

    done = sum(counts[s] for s in TERMINAL_STATUSES)
    good = sum(counts[s] for s in GOOD_STATUSES)
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
    lines.append(f"  {BOLD}TerminalWorld — Environment Building Monitor{NC}  [{now_str}]")
    lines.append(f"{BOLD}{sep}{NC}")
    lines.append(
        f"  Total: {total:,} ({stats['total_source']})  |  "
        f"{GREEN}Done: {done:,}{NC}  |  {GREEN}Good: {good:,}{NC}"
    )
    lines.append(f"  Progress: [{bar}] {pct:.1f}%")
    lines.append("")

    # Cost summary
    lines.append(f"  {BOLD}Cost Summary{NC}")
    if all_costs:
        lines.append(f"  Spent      : {BOLD}{YELLOW}{fmt_cost(total_cost)}{NC}  ({len(all_costs):,} recordings billed)")
        lines.append(f"  Avg/rec    : {fmt_cost(avg_cost)}")
        if pending > 0 and avg_cost:
            lines.append(f"  Est. remain: {YELLOW}{fmt_cost(est_remain)}{NC}  ({pending:,} pending × avg)")
            lines.append(f"  Est. total : {YELLOW}{fmt_cost(est_total)}{NC}")
    else:
        lines.append("  No billing data yet.")
    lines.append("")

    # Maturity level table
    lines.append(f"  {'Maturity Level':<18} {'Count':>7}  {'Avg Cost':>9}  {'Total Cost':>10}  {'Avg Time':>9}")
    lines.append(f"  {'─'*18} {'─'*7}  {'─'*9}  {'─'*10}  {'─'*9}")

    for level in MATURITY_ORDER:
        count = counts.get(level, 0)
        if count == 0:
            continue
        color = MATURITY_COLOR.get(level, "")
        level_costs = costs.get(level, [])
        level_elapsed = elapsed_by_level = stats["elapsed"].get(level, [])
        avg_c = sum(level_costs) / len(level_costs) if level_costs else None
        tot_c = sum(level_costs) if level_costs else None
        avg_e = sum(level_elapsed) / len(level_elapsed) if level_elapsed else None

        avg_c_str = fmt_cost(avg_c) if avg_c is not None else "—"
        tot_c_str = fmt_cost(tot_c) if tot_c is not None else "—"
        avg_e_str = fmt_duration(avg_e) if avg_e is not None else "—"

        lines.append(
            f"  {color}{level:<18}{NC} {count:>7}  {avg_c_str:>9}  {tot_c_str:>10}  {avg_e_str:>9}"
        )
    lines.append("")

    # Recent failures/errors
    if errors:
        lines.append(f"  {BOLD}Errors (last 10, by cost):{NC}")
        for rec_id, level, cost in errors[:10]:
            color = MATURITY_COLOR.get(level, "")
            lines.append(f"    {color}{rec_id:<12}  {level:<16}  {fmt_cost(cost)}{NC}")
    lines.append("")
    lines.append(f"{BOLD}{sep}{NC}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live dashboard for environment_building/batch_build.py"
    )
    parser.add_argument("--root", type=Path, default=None, help="Project root directory")
    parser.add_argument("--interval", type=float, default=15.0, help="Refresh interval (seconds)")
    parser.add_argument("--once", action="store_true", help="Print once and exit")
    parser.add_argument(
        "--task-list", type=Path, default=None,
        help="Text file of recording IDs (one per line) to use as total count",
    )
    parser.add_argument("--no-clear", action="store_true", help="Do not clear screen")
    args = parser.parse_args()

    root = args.root or find_project_root()

    def render() -> None:
        stats = collect_stats(root, args.task_list)
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
