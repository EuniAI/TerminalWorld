#!/usr/bin/env python3
"""Run a Harbor trial (oracle, nop, or partial) for a task and write a summary JSON.

Pure deterministic script — calls Harbor Python API, parses results, no heuristics.

Usage:
    python3 run_trial.py --task-dir <DIR> --mode oracle|nop|partial --output <FILE> [--trials-dir <DIR>]
    python3 run_trial.py --task-dir <DIR> --mode partial --partial-script <PATH> --output <FILE>
"""

import argparse
import asyncio
import json
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def build_trial_config(task_dir: Path, mode: str, trials_dir: Path):
    """Build a Harbor TrialConfig. Partial mode runs as oracle (executes solve.sh)."""
    from harbor.models.trial.config import (
        AgentConfig,
        EnvironmentConfig,
        TaskConfig,
        TrialConfig,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    short_id = uuid4().hex[:6]
    trial_name = f"{mode}-{timestamp}-{short_id}"

    # Partial mode uses the oracle agent (which runs solve.sh)
    agent_name = "oracle" if mode == "partial" else mode

    return TrialConfig(
        task=TaskConfig(path=task_dir),
        trial_name=trial_name,
        trials_dir=trials_dir,
        agent=AgentConfig(name=agent_name),
        environment=EnvironmentConfig(delete=True, force_build=True),
    )


async def run_trial(config) -> dict:
    """Run a single trial and return summary dict."""
    from harbor.trial.trial import Trial

    trial = await Trial.create(config)
    trial_dir = trial.trial_dir  # public @property: config.trials_dir / config.trial_name
    result = await trial.run()
    return {
        "trial_dir": str(trial_dir),
        "result": result,
    }


def parse_trial_output(trial_dir_str: str, mode: str) -> dict:
    """Parse trial output files into a summary dict."""
    trial_dir = Path(trial_dir_str)

    # Paths
    reward_txt_path = trial_dir / "verifier" / "reward.txt"
    reward_json_path = trial_dir / "verifier" / "reward.json"
    test_stdout_path = trial_dir / "verifier" / "test-stdout.txt"
    ctrf_path = trial_dir / "verifier" / "ctrf.json"
    result_path = trial_dir / "result.json"
    oracle_log_path = trial_dir / "agent" / "oracle.txt"

    # Parse reward (Harbor supports both reward.txt and reward.json)
    reward = None
    if reward_txt_path.exists():
        try:
            reward = int(float(reward_txt_path.read_text().strip()))
        except (ValueError, TypeError):
            reward = None
    elif reward_json_path.exists():
        try:
            rewards = json.loads(reward_json_path.read_text())
            reward = int(float(rewards.get("reward", 0)))
        except (ValueError, TypeError, json.JSONDecodeError):
            reward = None

    summary = {
        "mode": mode,
        "trial_dir": trial_dir_str,
        "reward": reward,
        "is_error": reward is None,
        "exception": None,
        "reward_path": str(reward_txt_path) if reward_txt_path.exists() else (str(reward_json_path) if reward_json_path.exists() else None),
        "test_stdout_path": str(test_stdout_path) if test_stdout_path.exists() else None,
        "ctrf_path": str(ctrf_path) if ctrf_path.exists() else None,
        "result_path": str(result_path) if result_path.exists() else None,
    }

    # Oracle and partial both produce oracle.txt (since partial runs as oracle agent)
    if mode in ("oracle", "partial"):
        summary["oracle_log_path"] = str(oracle_log_path) if oracle_log_path.exists() else None

    return summary


def main():
    parser = argparse.ArgumentParser(description="Run a Harbor trial (oracle, nop, or partial).")
    parser.add_argument("--task-dir", required=True, help="Path to the task directory")
    parser.add_argument("--mode", required=True, choices=["oracle", "nop", "partial"], help="Trial mode")
    parser.add_argument("--output", required=True, help="Path to write the trial summary JSON")
    parser.add_argument("--partial-script", default=None,
                        help="Path to partial solve script (required when --mode partial)")
    parser.add_argument("--trials-dir", default=None, help="Directory to store trial data (default: <task-dir>/.trials)")
    args = parser.parse_args()

    task_dir = Path(args.task_dir).resolve()
    if not task_dir.is_dir():
        print(f"ERROR: Task directory not found: {task_dir}", file=sys.stderr)
        sys.exit(1)

    if args.mode == "partial" and not args.partial_script:
        print("ERROR: --partial-script is required when --mode is partial", file=sys.stderr)
        sys.exit(1)

    if args.partial_script:
        partial_script = Path(args.partial_script).resolve()
        if not partial_script.is_file():
            print(f"ERROR: Partial script not found: {partial_script}", file=sys.stderr)
            sys.exit(1)

    trials_dir = Path(args.trials_dir) if args.trials_dir else task_dir / ".trials"

    # Import harbor — fail early with clear message
    try:
        from harbor.models.trial.config import TrialConfig  # noqa: F401
    except ImportError:
        print(
            "ERROR: harbor package is not installed. Install it with:\n"
            "  uv tool install harbor\n"
            "or:\n"
            "  pip install harbor",
            file=sys.stderr,
        )
        sys.exit(1)

    # For partial mode: swap solve.sh with the partial script, then restore after trial
    solve_sh = task_dir / "solution" / "solve.sh"
    solve_sh_backup = None

    if args.mode == "partial":
        solve_sh_backup = task_dir / "solution" / "solve.sh.bak"
        shutil.copy2(solve_sh, solve_sh_backup)
        shutil.copy2(partial_script, solve_sh)
        print(f"Partial mode: swapped solve.sh with {partial_script.name}")

    config = build_trial_config(task_dir, args.mode, trials_dir)

    partial_name = Path(args.partial_script).name if args.partial_script else None
    output = {
        "mode": args.mode,
        "task_dir": str(task_dir),
        "trial_name": config.trial_name,
    }
    if partial_name:
        output["partial_script"] = partial_name

    try:
        trial_result = asyncio.run(run_trial(config))
        trial_dir = trial_result["trial_dir"]
        summary = parse_trial_output(trial_dir, args.mode)
        output.update(summary)
    except Exception as e:
        output.update({
            "trial_dir": None,
            "reward": None,
            "is_error": True,
            "exception": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        })
    finally:
        # Always restore solve.sh for partial mode
        if solve_sh_backup and solve_sh_backup.exists():
            shutil.move(str(solve_sh_backup), str(solve_sh))
            print("Partial mode: restored original solve.sh")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str) + "\n")
    print(f"Trial summary written to {output_path}")

    if output.get("is_error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
