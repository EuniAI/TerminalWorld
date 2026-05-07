#!/usr/bin/env python3
"""
Refine task.toml resource/timeout fields using an LLM.

Reads instruction.md, solution/solve.sh, environment/Dockerfile (or docker-compose),
and tests/test_state.py to decide conservative adjustments to task.toml fields.

Guiding principle: err on the side of generous values — a timeout that is slightly
too large wastes a few seconds; one that is too small causes false negatives in
benchmark results.

Inputs (from {tasks_dir}/{id}/):
  - instruction.md
  - solution/solve.sh
  - environment/Dockerfile (or docker-compose.yaml)
  - tests/test_state.py
  - task.toml

Outputs:
  - {tasks_dir}/{id}/task.toml          (patched in-place)
  - {tasks_dir}/{id}/pipeline_artifacts/toml_refinement/toml_metadata.json

Usage:
    python -m task_refinement.refine_task_toml --ids 11741
    python -m task_refinement.refine_task_toml --id-file task_scaling/refined_scaling_tasks_v1.txt --resume --workers 8
    python -m task_refinement.refine_task_toml --tasks-dir data/refined_tasks --resume --workers 4
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
# Logging
# =============================================================================

logger = logging.getLogger("refine_task_toml")


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

_STANDARD_PATH = Path(__file__).parent / "skill" / "references" / "task_toml_standard.md"

SYSTEM_PROMPT = """\
You are a benchmark infrastructure engineer reviewing task.toml configuration files
for the TerminalWorld terminal-task benchmark.

Your job: propose minimal, conservative adjustments to resource and timeout fields
so that neither the AI agent nor the verifier fails due to insufficient resources.

Guiding principle: err on the side of slightly generous values.
A timeout that is a bit large wastes seconds; a timeout that is too small produces
false negatives in benchmark results. Only flag absurdly inflated values that clearly
exceed what the task requires — never downgrade a value that is already generous.

You will receive:
1. The task.toml field standard (rules for each adjustable field)
2. The current task.toml
3. instruction.md, solution/solve.sh, environment/Dockerfile, tests/test_state.py

Respond with ONLY a JSON object in this exact schema — no explanation, no markdown:
{
  "patch": {
    "<section>.<field>": <new_value>
  },
  "reasoning": {
    "<section>.<field>": "<one sentence citing specific evidence from the files>"
  }
}

Rules:
- Only include fields in "patch" that genuinely need changing.
- Use numeric types (not strings) for timeout_sec, memory_mb, cpus, storage_mb.
- Use boolean for allow_internet.
- Never change: metadata.*, version, environment.docker_image, environment.gpus,
  environment.gpu_types, environment.mcp_servers, environment.skills_dir.
- Never downgrade a value that is already at or above the correct level.
- If no changes are needed, return: {"patch": {}, "reasoning": {}}
"""


def _build_user_prompt(standard: str, task_toml: str, instruction: str,
                       solve_sh: str, dockerfile: str, test_state_py: str,
                       test_sh: str) -> str:
    parts = [
        "## task.toml Standard\n",
        standard,
        "\n\n## Current task.toml\n```toml\n",
        task_toml,
        "```\n\n## instruction.md\n```markdown\n",
        instruction,
        "```\n\n## solution/solve.sh\n```bash\n",
        solve_sh,
        "```\n\n## environment/Dockerfile (or docker-compose)\n```\n",
        dockerfile,
        "```\n\n## tests/test_state.py\n```python\n",
        test_state_py,
        "```\n",
    ]
    if test_sh.strip():
        parts += ["\n## tests/test.sh\n```bash\n", test_sh, "```\n"]
    return "".join(parts)


# =============================================================================
# TOML patch helpers
# =============================================================================

# Fields that must be written as TOML floats (e.g. 900.0)
_FLOAT_FIELDS = {"timeout_sec", "build_timeout_sec"}


def _toml_value(key: str, val: object) -> str:
    """Convert a Python value to its TOML literal, enforcing float/int per field."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        if key in _FLOAT_FIELDS:
            s = repr(float(val))
            return s if "." in s or "e" in s else s + ".0"
        return str(int(val))
    return json.dumps(str(val))


def _apply_patch(original: str, patch: dict[str, object]) -> str:
    """
    Apply {section.key: new_value} to raw TOML text.
    Modifies existing lines in-place; appends new keys at the end of their section
    if the key is not already present.
    """
    changes: dict[tuple[str, str], str] = {}
    for dotkey, val in patch.items():
        parts = dotkey.split(".", 1)
        if len(parts) == 2:
            changes[(parts[0], parts[1])] = _toml_value(parts[1], val)

    applied: set[tuple[str, str]] = set()
    lines = original.splitlines(keepends=True)
    current_section = "__root__"
    result = []

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[["):
            # Before moving to a new section, flush any unapplied keys for the
            # current section by appending them just before the section header.
            for (sec, key), toml_val in changes.items():
                if sec == current_section and (sec, key) not in applied:
                    result.append(f"{key} = {toml_val}\n")
                    applied.add((sec, key))
            current_section = stripped.strip("[]").strip()
            result.append(line)
            continue
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=")[0].strip()
            lookup = (current_section, key)
            if lookup in changes:
                indent = line[: len(line) - len(line.lstrip())]
                result.append(f"{indent}{key} = {changes[lookup]}\n")
                applied.add(lookup)
                continue
        result.append(line)

    # Flush any remaining unapplied keys for the last section
    for (sec, key), toml_val in changes.items():
        if (sec, key) not in applied:
            result.append(f"{key} = {toml_val}\n")
            applied.add((sec, key))

    return "".join(result)


# =============================================================================
# LLM call
# =============================================================================

def _call_llm(
    task_id: str,
    user_text: str,
    model: str,
    temperature: float,
    max_tokens: int,
) -> tuple[Optional[dict], float, int, int, Optional[str]]:
    """
    Call the LLM and parse the JSON patch response.
    Returns (patch_dict_or_None, cost_usd, prompt_tokens, completion_tokens, finish_reason).
    Retries up to 3 times.
    """
    token_budgets = [max_tokens, max_tokens * 2, max_tokens * 4]
    total_cost_usd = 0.0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    last_finish_reason: Optional[str] = None

    for attempt, token_budget in enumerate(token_budgets, 1):
        try:
            resp = litellm.completion(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                temperature=temperature,
                max_tokens=token_budget,
            )
            choice = resp.choices[0]
            content = choice.message.content
            finish_reason = getattr(choice, "finish_reason", None)
            last_finish_reason = finish_reason

            usage = resp.usage or {}
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            total_prompt_tokens += prompt_tokens
            total_completion_tokens += completion_tokens

            # Cost extraction — 3-tier
            cost_usd = 0.0
            if hasattr(resp, "_hidden_params"):
                headers = (
                    resp._hidden_params.get("additional_headers")
                    or resp._hidden_params.get("headers")
                    or {}
                )
                cost_str = (
                    headers.get("x-litellm-response-cost")
                    or headers.get("X-LiteLLM-Response-Cost")
                )
                if cost_str:
                    try:
                        cost_usd = float(cost_str)
                    except ValueError:
                        pass
            if cost_usd == 0.0:
                try:
                    cost_usd = litellm.completion_cost(completion_response=resp)
                except Exception:
                    pass
            if cost_usd == 0.0 and (prompt_tokens or completion_tokens):
                # Fallback: Sonnet 4.6 pricing ($3/$15 per 1M)
                cost_usd = (prompt_tokens * 3.0 + completion_tokens * 15.0) / 1_000_000
            total_cost_usd += cost_usd

            if finish_reason == "length":
                logger.warning(
                    "  [%s] Hit max_tokens=%d; retrying with larger budget.", task_id, token_budget
                )
                continue

            if not content:
                continue

            # Parse JSON — strip markdown fences if present
            text = content.strip()
            text = re.sub(r"^```[a-z]*\n?", "", text, flags=re.MULTILINE)
            text = re.sub(r"\n?```$", "", text.strip(), flags=re.MULTILINE)
            start = text.find("{")
            if start == -1:
                logger.warning("  [%s] No JSON found in response, retrying.", task_id)
                continue
            depth = 0
            end = start
            for i, ch in enumerate(text[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            parsed = json.loads(text[start: end + 1])
            return (
                parsed,
                total_cost_usd,
                total_prompt_tokens,
                total_completion_tokens,
                finish_reason,
            )

        except litellm.ContextWindowExceededError as e:
            logger.error("  [%s] Context window exceeded: %s", task_id, e)
            return None, total_cost_usd, total_prompt_tokens, total_completion_tokens, "context_window_exceeded"
        except Exception as e:
            logger.warning("  [%s] LLM attempt %d/%d failed: %s", task_id, attempt, len(token_budgets), e)
            if attempt < len(token_budgets):
                time.sleep(5 * attempt)

    return None, total_cost_usd, total_prompt_tokens, total_completion_tokens, last_finish_reason


# =============================================================================
# Per-task logic
# =============================================================================

def _read(p: Path) -> str:
    if p.exists():
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    return ""


def process_task(
    task_id: str,
    tasks_dir: Path,
    model: str,
    temperature: float,
    max_tokens: int,
) -> tuple[str, bool, float, int, int]:
    task_dir = tasks_dir / task_id
    task_toml_path = task_dir / "task.toml"

    if not task_toml_path.exists():
        logger.warning("  -> Skipping %s: task.toml not found", task_id)
        return task_id, False, 0.0, 0, 0

    task_toml = _read(task_toml_path)
    if not task_toml.strip():
        logger.warning("  -> Skipping %s: task.toml is empty", task_id)
        return task_id, False, 0.0, 0, 0

    instruction = _read(task_dir / "instruction.md")
    solve_sh = _read(task_dir / "solution" / "solve.sh")
    test_state_py = _read(task_dir / "tests" / "test_state.py")
    test_sh = _read(task_dir / "tests" / "test.sh")

    # Dockerfile or docker-compose
    for fname in ("Dockerfile", "docker-compose.yaml", "docker-compose.yml",
                  "compose.yaml", "compose.yml"):
        candidate = task_dir / "environment" / fname
        if candidate.exists():
            dockerfile = _read(candidate)
            break
    else:
        dockerfile = ""

    standard = _read(_STANDARD_PATH)

    user_text = _build_user_prompt(
        standard, task_toml, instruction, solve_sh, dockerfile, test_state_py, test_sh
    )

    parsed, cost, p_tokens, c_tokens, finish_reason = _call_llm(
        task_id, user_text, model, temperature, max_tokens
    )

    if parsed is None:
        logger.error(
            "  -> Failed to get LLM response for %s (finish_reason=%s)", task_id, finish_reason
        )
        return task_id, False, cost, p_tokens, c_tokens

    patch = parsed.get("patch", {})
    reasoning = parsed.get("reasoning", {})

    if patch:
        patched = _apply_patch(task_toml, patch)
        task_toml_path.write_text(patched, encoding="utf-8")
        logger.info(
            "  -> Patched %d field(s) in %s (cost: $%.4f)", len(patch), task_id, cost
        )
        for field, new_val in patch.items():
            note = reasoning.get(field, "")
            logger.info("     %s = %s  [%s]", field, new_val, note)
    else:
        logger.info("  -> No changes needed for %s (cost: $%.4f)", task_id, cost)

    artifacts_dir = task_dir / "pipeline_artifacts" / "toml_refinement"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "toml_metadata.json").write_text(
        json.dumps({
            "model": model,
            "patch": patch,
            "reasoning": reasoning,
            "cost_usd": cost,
            "prompt_tokens": p_tokens,
            "completion_tokens": c_tokens,
            "finish_reason": finish_reason,
            "timestamp": time.time(),
        }, indent=2),
        encoding="utf-8",
    )

    return task_id, True, cost, p_tokens, c_tokens


# =============================================================================
# ID loading
# =============================================================================

def load_ids(args: argparse.Namespace, tasks_dir: Path) -> list[str]:
    if args.ids:
        return args.ids
    if args.input:
        path = Path(args.input)
        if not path.exists():
            logger.error("Input file not found: %s", path)
            sys.exit(1)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.error("Expected a JSON object (dict) in %s", path)
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
    if not tasks_dir.exists():
        logger.error("Tasks directory not found: %s", tasks_dir)
        sys.exit(1)
    return sorted(p.name for p in tasks_dir.iterdir() if p.is_dir())


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="LLM-based task.toml field optimizer for TerminalWorld tasks"
    )
    parser.add_argument(
        "--tasks-dir",
        default=str(Path(__file__).parent.parent / "data" / "refined_tasks"),
    )
    parser.add_argument("--log-file", default="logs/refine_task_toml.log")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip tasks that already have pipeline_artifacts/toml_refinement/toml_metadata.json",
    )
    parser.add_argument("--limit", type=int, default=None)

    id_group = parser.add_mutually_exclusive_group(required=False)
    id_group.add_argument("--ids", nargs="+", metavar="ID")
    id_group.add_argument("--id-file", help="Text file with one task ID per line")
    id_group.add_argument("--input", help="JSON file whose keys are task IDs")

    args = parser.parse_args()

    global logger
    logger = setup_logger(Path(args.log_file))
    tasks_dir = Path(args.tasks_dir)

    all_ids = load_ids(args, tasks_dir)

    pending = []
    for task_id in all_ids:
        if args.resume:
            meta = tasks_dir / task_id / "pipeline_artifacts" / "toml_refinement" / "toml_metadata.json"
            if meta.exists():
                continue
        pending.append(task_id)

    if args.limit:
        pending = pending[: args.limit]

    logger.info(
        "Found %d tasks to process (skipped %d already done, %d workers)",
        len(pending), len(all_ids) - len(pending), args.workers,
    )

    total_cost = 0.0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    success_count = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_to_id = {
            executor.submit(
                process_task,
                task_id,
                tasks_dir,
                args.model,
                args.temperature,
                args.max_tokens,
            ): task_id
            for task_id in pending
        }
        for i, future in enumerate(as_completed(future_to_id), 1):
            task_id = future_to_id[future]
            try:
                _, ok, cost, p_tok, c_tok = future.result()
            except Exception as e:
                logger.exception("  -> Unexpected failure for %s: %s", task_id, e)
                error_count += 1
                continue

            total_cost += cost
            total_prompt_tokens += p_tok
            total_completion_tokens += c_tok
            if ok:
                success_count += 1
            else:
                error_count += 1
            if i % 50 == 0:
                logger.info("Progress: %d/%d done, $%.4f so far", i, len(pending), total_cost)

    logger.info("=" * 50)
    logger.info(
        "Done: %d success, %d errors, total cost $%.4f",
        success_count, error_count, total_cost,
    )
    logger.info(
        "LLM usage: prompt_tokens=%d  completion_tokens=%d",
        total_prompt_tokens, total_completion_tokens,
    )


if __name__ == "__main__":
    main()
