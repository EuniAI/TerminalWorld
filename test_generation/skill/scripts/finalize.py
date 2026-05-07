#!/usr/bin/env python3
"""Write refinement_metadata.json and archive intermediate files.

Pure deterministic script — no judgment, no heuristics.

Usage:
    python3 finalize.py --task-dir <DIR> --status refined|partial|failed|infeasible \
        --oracle-reward <0|1> --nop-reward <0|1> --iterations <N>

For --status infeasible, --oracle-reward, --nop-reward, and --iterations are not
required and can be omitted.
"""

import argparse
import glob
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def archive_intermediate_files(task_dir: Path) -> list[str]:
    """Move intermediate files into <task_dir>/refinement/. Returns list of archived items."""
    refinement_dir = task_dir / "refinement"
    refinement_dir.mkdir(exist_ok=True)
    archived = []

    # Single files at task_dir root
    for name in [
        "audit.json",
        "diagnosis_static.json",
        "diagnosis_runtime.json",
        "oracle_trial.json",
        "nop_trial.json",
    ]:
        src = task_dir / name
        if src.exists():
            shutil.move(str(src), str(refinement_dir / name))
            archived.append(name)

    # Glob patterns at task_dir root
    for path in glob.glob(str(task_dir / "partial_trial_*.json")):
        name = Path(path).name
        shutil.move(path, str(refinement_dir / name))
        archived.append(name)

    # Partial solve scripts from solution/
    for path in glob.glob(str(task_dir / "solution" / "partial_solve_*.sh")):
        name = Path(path).name
        shutil.move(path, str(refinement_dir / name))
        archived.append(f"solution/{name}")

    # .trials directory
    trials_dir = task_dir / ".trials"
    if trials_dir.is_dir():
        dest = refinement_dir / "trials"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(trials_dir), str(dest))
        archived.append(".trials/ -> refinement/trials/")

    return archived


def main():
    parser = argparse.ArgumentParser(description="Write refinement metadata and archive intermediate files.")
    parser.add_argument("--task-dir", required=True, help="Path to the task directory")
    parser.add_argument("--status", required=True, choices=["refined", "partial", "failed", "infeasible"],
                        help="Final refinement status")
    parser.add_argument("--oracle-reward", required=False, type=int, choices=[0, 1], default=None,
                        help="Oracle trial reward (0 or 1); not required for --status infeasible")
    parser.add_argument("--nop-reward", required=False, type=int, choices=[0, 1], default=None,
                        help="Nop trial reward (0 or 1); not required for --status infeasible")
    parser.add_argument("--iterations", required=False, type=int, default=None,
                        help="Number of refinement iterations completed; not required for --status infeasible")
    args = parser.parse_args()

    task_dir = Path(args.task_dir)
    if not task_dir.is_dir():
        print(f"ERROR: Task directory not found: {task_dir}", file=sys.stderr)
        sys.exit(1)

    if args.status != "infeasible":
        missing = [f for f, v in [("--oracle-reward", args.oracle_reward), ("--nop-reward", args.nop_reward), ("--iterations", args.iterations)] if v is None]
        if missing:
            print(f"ERROR: {', '.join(missing)} required for --status {args.status}", file=sys.stderr)
            sys.exit(1)

    # Write metadata
    metadata: dict = {"status": args.status, "refined_at": datetime.now(timezone.utc).isoformat()}
    if args.status != "infeasible":
        metadata["oracle_reward"] = args.oracle_reward
        metadata["nop_reward"] = args.nop_reward
        metadata["iterations"] = args.iterations

    output_path = task_dir / "refinement_metadata.json"
    output_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
    print(f"Refinement metadata written to {output_path}")

    # Archive intermediate files
    archived = archive_intermediate_files(task_dir)
    if archived:
        print(f"Archived {len(archived)} items to {task_dir / 'refinement'}:")
        for item in archived:
            print(f"  {item}")
    else:
        print("No intermediate files to archive.")


if __name__ == "__main__":
    main()
