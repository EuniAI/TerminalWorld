#!/usr/bin/env python3
"""Scan refined_tasks and clean up: archive intermediate files, remove stale workspaces.

Usage:
    python -m task_refinement.cleanup_tasks [--dry-run] [--refined-tasks-dir ./data/refined_tasks]
"""

import argparse
import shutil
import sys
from pathlib import Path

# Reuse the archival logic from finalize.py
from task_refinement.skill.scripts.finalize import archive_intermediate_files


def cleanup_task(task_dir: Path, dry_run: bool = False) -> dict:
    """Clean up a single task directory. Returns summary of actions taken."""
    tid = task_dir.name
    actions: dict = {"task_id": tid, "archived": [], "workspaces_removed": []}

    # 1. Archive intermediate files to refinement/
    if not dry_run:
        archived = archive_intermediate_files(task_dir)
    else:
        # Dry-run: just detect what would be archived
        archived = []
        for name in [
            "audit.json", "diagnosis_static.json", "diagnosis_runtime.json",
            "oracle_trial.json", "nop_trial.json",
        ]:
            if (task_dir / name).exists():
                archived.append(name)
        for p in sorted(task_dir.glob("partial_trial_*.json")):
            archived.append(p.name)
        for p in sorted((task_dir / "solution").glob("partial_solve_*.sh")) if (task_dir / "solution").is_dir() else []:
            archived.append(f"solution/{p.name}")
        if (task_dir / ".trials").is_dir():
            archived.append(".trials/ -> refinement/trials/")
    actions["archived"] = archived

    # 2. Remove stale .refine_workspace_* directories
    for ws in sorted(task_dir.glob(".refine_workspace_*")):
        if ws.is_dir():
            actions["workspaces_removed"].append(ws.name)
            if not dry_run:
                shutil.rmtree(ws, ignore_errors=True)

    return actions


def main():
    parser = argparse.ArgumentParser(description="Clean up refined task directories.")
    parser.add_argument(
        "--refined-tasks-dir", default="./data/refined_tasks",
        help="Base directory containing task subdirectories (default: ./data/refined_tasks)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    parser.add_argument("--task-ids", nargs="*", default=None, help="Specific task IDs (default: all)")
    args = parser.parse_args()

    base = Path(args.refined_tasks_dir).resolve()
    if not base.is_dir():
        print(f"ERROR: Directory not found: {base}", file=sys.stderr)
        sys.exit(1)

    # Discover tasks
    if args.task_ids:
        task_dirs = [base / tid for tid in args.task_ids]
    else:
        task_dirs = sorted([d for d in base.iterdir() if d.is_dir()], key=lambda d: d.name)

    if args.dry_run:
        print("=== DRY RUN (no changes will be made) ===\n")

    total_archived = 0
    total_workspaces = 0
    tasks_touched = 0

    for task_dir in task_dirs:
        if not task_dir.is_dir():
            print(f"SKIP {task_dir.name}: not found")
            continue

        result = cleanup_task(task_dir, dry_run=args.dry_run)
        archived = result["archived"]
        ws_removed = result["workspaces_removed"]

        if not archived and not ws_removed:
            continue

        tasks_touched += 1
        total_archived += len(archived)
        total_workspaces += len(ws_removed)

        print(f"[{result['task_id']}]")
        if archived:
            print(f"  Archived {len(archived)} items -> refinement/")
            for item in archived:
                print(f"    {item}")
        if ws_removed:
            print(f"  Removed {len(ws_removed)} workspace(s)")
            for ws in ws_removed:
                print(f"    {ws}")

    print(f"\n{'DRY RUN ' if args.dry_run else ''}SUMMARY: {tasks_touched} tasks cleaned, "
          f"{total_archived} items archived, {total_workspaces} workspaces removed")


if __name__ == "__main__":
    main()
