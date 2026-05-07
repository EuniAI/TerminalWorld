#!/usr/bin/env python3
"""Audit task artifacts: collect mechanical facts about file existence, sizes, and test structure.

Pure deterministic script — no judgment, no heuristics.

Usage:
    python3 audit_artifacts.py --task-dir <DIR> --output <FILE>
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path


EXPECTED_FILES = [
    "instruction.md",
    "task.toml",
    "solution/solve.sh",
    "tests/test.sh",
    "tests/test_state.py",
    "environment/Dockerfile",
]

# Harbor only recognizes "docker-compose.yaml". All other variants must be renamed.
COMPOSE_CANONICAL = "docker-compose.yaml"
COMPOSE_VARIANTS = ["docker-compose.yml", "compose.yaml", "compose.yml"]


def normalize_compose_filename(task_dir: Path) -> str | None:
    """Rename non-standard compose filenames to docker-compose.yaml (Harbor requirement).

    Returns the original filename if a rename was performed, None otherwise.
    """
    env_dir = task_dir / "environment"
    canonical = env_dir / COMPOSE_CANONICAL
    if canonical.exists():
        return None

    for variant in COMPOSE_VARIANTS:
        src = env_dir / variant
        if src.exists():
            shutil.move(str(src), str(canonical))
            print(f"Renamed {variant} -> {COMPOSE_CANONICAL} (Harbor requirement)")
            return variant
    return None


def audit(task_dir: Path) -> dict:
    # Auto-fix compose filename before auditing
    renamed_from = normalize_compose_filename(task_dir)

    files_info = {}
    for rel in EXPECTED_FILES:
        p = task_dir / rel
        files_info[rel] = {
            "exists": p.exists(),
            "size_bytes": p.stat().st_size if p.exists() else 0,
        }

    # Extract test function names from test_state.py
    test_state = task_dir / "tests" / "test_state.py"
    test_functions: list[str] = []
    has_harbor_canary = False

    if test_state.exists():
        content = test_state.read_text(encoding="utf-8", errors="replace")
        test_functions = re.findall(r"^def (test_\w+)", content, re.MULTILINE)
        # Check first 5 lines for harbor-canary
        first_lines = "\n".join(content.splitlines()[:5])
        has_harbor_canary = "harbor-canary" in first_lines

    # Check docker-compose (after normalization)
    docker_compose_exists = (task_dir / "environment" / COMPOSE_CANONICAL).exists()

    result = {
        "task_dir": str(task_dir.resolve()),
        "files": files_info,
        "test_functions": test_functions,
        "test_function_count": len(test_functions),
        "has_harbor_canary": has_harbor_canary,
        "docker_compose_exists": docker_compose_exists,
    }
    if renamed_from:
        result["compose_renamed_from"] = renamed_from

    return result


def main():
    parser = argparse.ArgumentParser(description="Audit task artifact structure.")
    parser.add_argument("--task-dir", required=True, help="Path to the task directory")
    parser.add_argument("--output", required=True, help="Path to write the audit JSON")
    args = parser.parse_args()

    task_dir = Path(args.task_dir)
    if not task_dir.is_dir():
        print(f"ERROR: Task directory not found: {task_dir}", file=sys.stderr)
        sys.exit(1)

    result = audit(task_dir)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"Audit written to {output_path}")


if __name__ == "__main__":
    main()
