#!/usr/bin/env python3
"""
Generate task.toml metadata for verified tasks using an LLM.

Reads from:
  - data/recordings/{id}/info.json      : title, description, author
  - data/tasks/{id}/instruction.md      : refined task instruction
  - data/tasks/{id}/solution/solve.sh   : reference solution (ground truth)
  - data/tasks/{id}/environment/Dockerfile : resource hints

Writes:
  - data/tasks/{id}/task.toml           : Harbor-compatible task metadata

Usage:
    python generate_task_metadata.py --ids 166586
    python generate_task_metadata.py --id-file ids.txt --resume --workers 4
    python generate_task_metadata.py --input output/filtered_recordings.json
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

import litellm

litellm.drop_params = True
litellm.suppress_debug_info = True
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

# =============================================================================
# Constants
# =============================================================================

TOML_VERSION = "1.0"

# Default resource limits (overridable by LLM suggestions)
DEFAULT_VERIFIER_TIMEOUT = 120.0
DEFAULT_AGENT_TIMEOUT = 900.0
DEFAULT_BUILD_TIMEOUT = 600.0
DEFAULT_CPUS = 1
DEFAULT_MEMORY_MB = 2048
DEFAULT_STORAGE_MB = 10240
DEFAULT_GPUS = 0

# Valid categories
VALID_CATEGORIES = [
    "software_engineering",
    "system_administration",
    "mathematics",
    "data_science",
    "cybersecurity",
    "networking",
    "devops",
    "data_processing",
    "scientific_computing",
    "debugging",
]

# =============================================================================
# Logging Setup
# =============================================================================

logger = logging.getLogger("generate_task_metadata")


def setup_logger(log_file: Path) -> logging.Logger:
    logger.setLevel(logging.INFO)
    logger.handlers = []
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# =============================================================================
# Prompts
# =============================================================================

SYSTEM_PROMPT = """\
You are a benchmark curator for TerminalWorld, a challenging terminal agent benchmark.
TerminalWorld targets tasks where frontier AI models succeed only 0-10% of the time.

You will be given:
1. Recording metadata (title, description from asciinema)
2. A task instruction (what the agent must accomplish)
3. A reference solution (shell script)
4. The environment Dockerfile

Your job is to generate metadata for a task.toml file. Output a single JSON object with these fields:

{
  "category": "<one of: software_engineering, system_administration, mathematics, data_science, cybersecurity, networking, devops, data_processing, scientific_computing, debugging>",
  "tags": ["<tag1>", "<tag2>", ...],
  "difficulty_explanation": "<1-3 sentences: why is this task hard for an AI agent? What knowledge, reasoning, or problem-solving is required?>",
  "solution_explanation": "<1-3 sentences: high-level approach and key steps to solve the task>",
  "verification_explanation": "<1-2 sentences: how the test suite verifies the solution is correct>",
  "expert_time_estimate_hours": <float: best-case hours for a focused domain expert>,
  "agent_timeout_sec": <float: recommended max seconds for an AI agent (300-7200, default 900)>,
  "memory_mb": <int: RAM needed (2048 default; increase for large datasets/in-memory work)>,
  "cpus": <int: CPU cores needed (1 default; increase for parallel/compile-heavy work)>,
  "storage_mb": <int: disk space needed (10240 default; increase for large files/builds)>
}

GUIDELINES:
- Tags should be lowercase, hyphenated (e.g., "number-theory", "build-system", "parallel-computing")
- Include 2-5 tags covering: domain, key tools/languages, technique
- difficulty_explanation should focus on what makes it hard FOR AN AI AGENT (not just humans)
- agent_timeout_sec: use 900 for most tasks; up to 3600 for compile/build-heavy; up to 7200 for very complex multi-step tasks
- memory_mb: 2048 for most; 4096+ if large datasets; 8192+ if many processes
- Be concise — no filler, no repetition
"""

USER_TEMPLATE = """\
--- RECORDING METADATA ---
Title: {title}
Description: {description}
Author: {author}
Duration: {duration}

--- TASK INSTRUCTION ---
{instruction}

--- REFERENCE SOLUTION ---
{solution}

--- ENVIRONMENT DOCKERFILE ---
{dockerfile}

Output the JSON metadata object.
"""


# =============================================================================
# LLM Call
# =============================================================================

def call_llm(
    rec_id: str,
    info: dict,
    instruction: str,
    solution: str,
    dockerfile: str,
    model: str,
    temperature: float = 0.3,
    max_tokens: int = 1024,
) -> tuple[Optional[dict], float, int, int]:
    """
    Call the LLM to generate task metadata.
    Returns (metadata_dict, cost_usd, prompt_tokens, completion_tokens).
    """
    user_text = USER_TEMPLATE.format(
        title=info.get("title", "untitled"),
        description=info.get("description", "No description"),
        author=info.get("author", "unknown"),
        duration=info.get("duration", "unknown"),
        instruction=instruction,
        solution=solution,
        dockerfile=dockerfile,
    )

    total_cost = 0.0
    total_p_tokens = 0
    total_c_tokens = 0
    budget = max_tokens

    for attempt in range(3):
        try:
            resp = litellm.completion(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                temperature=temperature,
                max_tokens=budget,
            )
            choice = resp.choices[0]
            content = choice.message.content or ""
            finish_reason = getattr(choice, "finish_reason", None)

            usage = resp.usage or {}
            p_tokens = getattr(usage, "prompt_tokens", 0) or 0
            c_tokens = getattr(usage, "completion_tokens", 0) or 0
            total_p_tokens += p_tokens
            total_c_tokens += c_tokens

            cost_usd = 0.0
            try:
                cost_usd = litellm.completion_cost(completion_response=resp)
            except Exception:
                cost_usd = (p_tokens * 3.0 + c_tokens * 15.0) / 1_000_000
            total_cost += cost_usd

            if finish_reason == "length":
                logger.warning("  [%s] Hit max_tokens=%d, retrying with larger budget.", rec_id, budget)
                budget *= 2
                continue

            metadata = parse_json_response(content)
            if metadata is None:
                logger.warning("  [%s] Failed to parse JSON response (attempt %d).", rec_id, attempt + 1)
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                continue

            return metadata, total_cost, total_p_tokens, total_c_tokens

        except litellm.ContextWindowExceededError as e:
            logger.error("  [%s] Context window exceeded: %s", rec_id, e)
            return None, total_cost, total_p_tokens, total_c_tokens
        except Exception as e:
            logger.warning("  [%s] LLM attempt %d/3 failed: %s", rec_id, attempt + 1, e)
            if attempt < 2:
                time.sleep(5 * (attempt + 1))

    return None, total_cost, total_p_tokens, total_c_tokens


def parse_json_response(content: str) -> Optional[dict]:
    """Extract JSON dict from LLM response (handles ```json ... ``` wrapper)."""
    code_block = re.search(r"```(?:json)?\s*\n(.*?)```", content, re.DOTALL | re.IGNORECASE)
    if code_block:
        text = code_block.group(1).strip()
    else:
        obj_match = re.search(r"\{.*\}", content, re.DOTALL)
        text = obj_match.group(0).strip() if obj_match else content.strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return None


# =============================================================================
# TOML Generation
# =============================================================================

def escape_toml_string(s: str) -> str:
    """Escape a string for TOML double-quoted value."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def build_task_toml(
    rec_id: str,
    metadata: dict,
) -> str:
    """Build a task.toml string from metadata dict."""
    category = metadata.get("category", "software_engineering")
    if category not in VALID_CATEGORIES:
        category = "software_engineering"

    tags = metadata.get("tags", [])
    if not isinstance(tags, list) or not tags:
        tags = ["terminal"]
    tags_toml = "[" + ", ".join(f'"{t}"' for t in tags) + "]"

    difficulty_explanation = escape_toml_string(str(metadata.get("difficulty_explanation", "")))
    solution_explanation = escape_toml_string(str(metadata.get("solution_explanation", "")))
    verification_explanation = escape_toml_string(str(metadata.get("verification_explanation", "")))

    expert_hours = float(metadata.get("expert_time_estimate_hours", 0.5))
    agent_timeout = max(300.0, min(7200.0, float(metadata.get("agent_timeout_sec", DEFAULT_AGENT_TIMEOUT))))
    memory_mb = max(1024, min(32768, int(metadata.get("memory_mb", DEFAULT_MEMORY_MB))))
    cpus = max(1, min(8, int(metadata.get("cpus", DEFAULT_CPUS))))
    storage_mb = max(4096, min(102400, int(metadata.get("storage_mb", DEFAULT_STORAGE_MB))))

    task_source = f"https://asciinema.org/a/{rec_id}"

    return f"""\
version = "{TOML_VERSION}"

[metadata]
task_source = "{task_source}"
task_drafter_name = "Zhaoyang Chu"
task_drafter_email = "zhaoyang.chu.25@ucl.ac.uk"
task_annotator_name = ""
task_annotator_email = ""
task_refiner_name = ""
task_refiner_email = ""
difficulty_explanation = "{difficulty_explanation}"
solution_explanation = "{solution_explanation}"
verification_explanation = "{verification_explanation}"
category = "{category}"
tags = {tags_toml}
expert_time_estimate_hours = {expert_hours}

[verifier]
timeout_sec = {DEFAULT_VERIFIER_TIMEOUT}

[agent]
timeout_sec = {agent_timeout}

[environment]
build_timeout_sec = {DEFAULT_BUILD_TIMEOUT}
cpus = {cpus}
memory_mb = {memory_mb}
storage_mb = {storage_mb}
gpus = {DEFAULT_GPUS}
"""


# =============================================================================
# File I/O
# =============================================================================

def is_verified(task_dir: Path) -> bool:
    """Return True only if status.json exists with human_verified=True and flagged=False."""
    status_path = task_dir / "status.json"
    if not status_path.exists():
        return False
    try:
        s = json.loads(status_path.read_text(encoding="utf-8"))
        return bool(s.get("human_verified")) and not bool(s.get("flagged"))
    except Exception:
        return False


def load_ids(args: argparse.Namespace) -> list[str]:
    if args.ids:
        return args.ids
    if args.input:
        path = Path(args.input)
        if not path.exists():
            logger.error("Input file not found: %s", path)
            sys.exit(1)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.error("Expected a JSON object in %s", path)
            sys.exit(1)
        return list(data.keys())
    if args.id_file:
        path = Path(args.id_file)
        if not path.exists():
            logger.error("ID file not found: %s", path)
            sys.exit(1)
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    return []


# =============================================================================
# Main Orchestrator
# =============================================================================

MINIMAL_METADATA: dict = {
    "category": "software_engineering",
    "tags": ["terminal"],
    "difficulty_explanation": "",
    "solution_explanation": "",
    "verification_explanation": "",
    "expert_time_estimate_hours": 0.5,
    "agent_timeout_sec": DEFAULT_AGENT_TIMEOUT,
    "memory_mb": DEFAULT_MEMORY_MB,
    "cpus": DEFAULT_CPUS,
    "storage_mb": DEFAULT_STORAGE_MB,
}


def process_recording(
    rec_id: str,
    recordings_dir: Path,
    tasks_dir: Path,
    model: str,
    temperature: float,
    max_tokens: int,
    minimal: bool = False,
) -> tuple[str, bool, float, int, int]:
    info_path = recordings_dir / rec_id / "info.json"
    output_path = tasks_dir / rec_id / "task.toml"

    # In minimal mode we only need the task dir to exist
    if not (tasks_dir / rec_id).exists():
        logger.warning("  -> Skipping %s: task directory not found", rec_id)
        return rec_id, False, 0.0, 0, 0

    if minimal:
        toml_content = build_task_toml(rec_id, MINIMAL_METADATA)
        output_path.write_text(toml_content, encoding="utf-8")
        logger.info("  -> Wrote minimal task.toml for %s", rec_id)
        return rec_id, True, 0.0, 0, 0

    # Full LLM mode
    if not info_path.exists():
        logger.warning("  -> Skipping %s: info.json not found at %s", rec_id, info_path)
        return rec_id, False, 0.0, 0, 0

    info = json.loads(info_path.read_text(encoding="utf-8"))

    instruction_path = tasks_dir / rec_id / "instruction.md"
    solution_path = tasks_dir / rec_id / "solution" / "solve.sh"
    dockerfile_path = tasks_dir / rec_id / "environment" / "Dockerfile"

    if not instruction_path.exists():
        logger.warning("  -> Skipping %s: instruction.md not found", rec_id)
        return rec_id, False, 0.0, 0, 0
    if not solution_path.exists():
        logger.warning("  -> Skipping %s: solution/solve.sh not found", rec_id)
        return rec_id, False, 0.0, 0, 0

    instruction = instruction_path.read_text(encoding="utf-8").strip()
    solution = solution_path.read_text(encoding="utf-8").strip()
    dockerfile = dockerfile_path.read_text(encoding="utf-8").strip() if dockerfile_path.exists() else "(not available)"

    metadata, cost, p_tokens, c_tokens = call_llm(
        rec_id, info, instruction, solution, dockerfile,
        model=model, temperature=temperature, max_tokens=max_tokens,
    )

    if metadata is None:
        logger.error("  -> Failed to generate metadata for %s", rec_id)
        return rec_id, False, cost, p_tokens, c_tokens

    toml_content = build_task_toml(rec_id, metadata)
    output_path.write_text(toml_content, encoding="utf-8")

    logger.info(
        "  -> Wrote task.toml for %s (category=%s, tags=%s, agent_timeout=%.0fs, cost=$%.4f)",
        rec_id,
        metadata.get("category", "?"),
        metadata.get("tags", []),
        float(metadata.get("agent_timeout_sec", DEFAULT_AGENT_TIMEOUT)),
        cost,
    )
    return rec_id, True, cost, p_tokens, c_tokens


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate task.toml for verified TerminalWorld tasks")
    parser.add_argument(
        "--recordings-dir",
        default=str(Path(__file__).parent.parent / "data" / "recordings"),
        help="Directory containing raw recordings with info.json",
    )
    parser.add_argument(
        "--tasks-dir",
        default=str(Path(__file__).parent.parent / "data" / "tasks"),
        help="Directory containing verified tasks",
    )
    parser.add_argument("--log-file", default="logs/generate_task_metadata.log")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--resume", action="store_true", help="Skip if task.toml already exists")
    parser.add_argument("--minimal", action="store_true", help="Write fixed placeholder values, no LLM call")
    parser.add_argument("--limit", type=int, default=None)

    id_group = parser.add_mutually_exclusive_group(required=True)
    id_group.add_argument("--ids", nargs="+", metavar="ID")
    id_group.add_argument("--input", help="JSON file whose keys are recording IDs")
    id_group.add_argument("--id-file", help="Text file with one recording ID per line")

    args = parser.parse_args()

    global logger
    logger = setup_logger(Path(args.log_file))

    recordings_dir = Path(args.recordings_dir)
    tasks_dir = Path(args.tasks_dir)

    all_ids = load_ids(args)

    pending = []
    for rec_id in all_ids:
        if not is_verified(tasks_dir / rec_id):
            logger.info("  -> Skipping %s: not verified", rec_id)
            continue
        if args.resume and (tasks_dir / rec_id / "task.toml").exists():
            logger.info("  -> Skipping %s: task.toml already exists", rec_id)
            continue
        pending.append(rec_id)

    if args.limit:
        pending = pending[:args.limit]

    logger.info("Processing %d tasks with %d worker(s)", len(pending), max(1, args.workers))

    total_cost = 0.0
    total_p_tokens = 0
    total_c_tokens = 0
    success_count = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                process_recording,
                rec_id, recordings_dir, tasks_dir,
                args.model, args.temperature, args.max_tokens,
                args.minimal,
            ): rec_id
            for rec_id in pending
        }

        for future in as_completed(futures):
            rec_id = futures[future]
            try:
                _, ok, cost, p_tok, c_tok = future.result()
            except Exception as e:
                logger.exception("  -> Unexpected failure for %s: %s", rec_id, e)
                error_count += 1
                continue

            total_cost += cost
            total_p_tokens += p_tok
            total_c_tokens += c_tok
            if ok:
                success_count += 1
            else:
                error_count += 1

    logger.info("=" * 50)
    logger.info("Metadata Generation Complete: %d success, %d errors", success_count, error_count)
    logger.info(
        "LLM usage: prompt_tokens=%d  completion_tokens=%d  total_cost=$%.4f",
        total_p_tokens, total_c_tokens, total_cost,
    )


if __name__ == "__main__":
    main()
