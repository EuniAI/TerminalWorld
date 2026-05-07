#!/usr/bin/env python3
"""
Synthesize instruction.md for scaled terminal tasks.

Reads source/info.json and solution/solve.sh for each task ID, asks an LLM
to produce a goal-oriented instruction.md. Writes to {id}/instruction.md and
puts metadata in pipeline_artifacts/instruction/.

Inputs:
  - {tasks_dir}/{id}/source/info.json: User intent (title, description)
  - {tasks_dir}/{id}/solution/solve.sh: Extracted solution script

Output:
  - {tasks_dir}/{id}/instruction.md

Usage:
    python generate_instruction.py --ids 100135
    python generate_instruction.py --id-file ../task_scaling/scaling_task_v1.txt --resume --workers 10
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import litellm

# Configure litellm
litellm.drop_params = True
litellm.suppress_debug_info = True
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

# =============================================================================
# Logging Setup
# =============================================================================

logger = logging.getLogger("generate_instruction")

def setup_logger(log_file: Path) -> logging.Logger:
    """Configure and return a logger."""
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
You are an expert system engineer and benchmark task designer.
Your goal is to synthesize a natural language `instruction.md` for an autonomous AI agent.

INPUT PROVIDED:
1. User Intent: Title and Description from the original recording.
2. Solution Script: A clean bash script that solves the task — use it to identify the
   technology domain, the end goal, and the exact output file paths produced.

CORE PRINCIPLE — DESCRIBE THE GOAL, NOT THE SOLUTION:
The solution script tells you WHAT state the system ends up in, not what the agent
should be told to do step by step. Your job is to write a goal-oriented task prompt:
describe the required end state so the agent can figure out its own approach.
Never transcribe, paraphrase, or allude to the specific commands in the script.

SYNTHESIS RULES:
1. Infer the Core Intent: From the title, description, and script together, identify
   the ultimate goal. Use the script only to understand the domain, the key technologies
   involved, and what output files are produced — not as a recipe to follow.
2. Goal-Oriented Tone:
   - Open with the main objective immediately — no preamble.
   - NEVER reproduce commands, flags, pipe chains, or intermediate steps from the script.
   - Do NOT teach the agent how to solve the task. Describe WHAT needs to be done and
     the required end state.
3. Be Concise and Grounded:
   - Write in a natural, direct voice (1–3 paragraphs).
   - Mention the working directory or key entry points if relevant (e.g., `/app`).
   - Name the core technology or service if it defines the task domain (e.g., "Use Nginx",
     "Train with PyTorch"). Never name intermediate helper tools.
   - NEVER mention "recording", "script", "solution", "trace", or "user actions".
4. Embed Output Paths and Format Constraints:
   - The instruction MUST mention ALL output file paths that the solution writes to
     (e.g., `/app/result.txt`, `/app/output.json`). Use absolute paths always.
   - If the output has a specific structure that tests will check — column order,
     timestamp format, key names, file type — state it explicitly. Agents will
     make different valid choices if not constrained; those choices break tests.
     Example: "Write results as tab-separated lines with format: rank count ngram"
     rather than just "write the frequency table".
   - Do NOT embed solution-internal artifacts: completion banners (`echo "Done!"`),
     arbitrary labels the solution invented (e.g., `SMOKE_OK`), or naming choices
     not required by the task (e.g., specific veth pair names, log section headers).
     Only include a name or format if it is genuinely required for the task to be
     verifiable — i.e., the test MUST see that exact string.
5. End-State Only: Describe only the final observable outcome.
   - NEVER use sequential phrases: "Start by", "First", "Then", "Next", "Finally".
   - NEVER list sub-steps. Write cohesive paragraphs (2–3 max).
   - Validation criteria must describe the final state, not intermediate checkpoints.
   - Do NOT list available tools or libraries — the agent discovers them independently.

SELF-CHECK before writing:
- Does the instruction mention any specific commands or flags? → Remove them.
- Does it read like a step-by-step tutorial? → Rewrite as end-state description.
- Does it name ALL output file paths from the solution? → Must be present.
- Does it specify output format/structure where tests will check it? → Add if missing.
- Does it mention solution-internal labels or banners not required by the task? → Remove them.

OUTPUT FORMAT:
Output ONLY valid Markdown. Write 1–3 concise paragraphs. No structural headings.

EXAMPLES:

### Example 1 — crack-7z-hash (minimal output, single file)

Solution script (key lines):
```bash
apt-get install libcompress-raw-lzma-perl 7zip
/app/john/run/7z2john.pl /app/secrets.7z > /app/secrets.hash
/app/john/run/john /app/secrets.hash > /app/cracked.txt
7z x -p1998 /app/secrets.7z -o/app
cat /app/secrets/secret_file.txt > /app/solution.txt
```

Good instruction:
> You need to create a file called "/app/solution.txt" with the word found in "secret_file.txt" in the "secrets.7z" archive.

Why it's good: names the output path, states the goal without mentioning john/7z2john/specific commands.

---

### Example 2 — db-wal-recovery (format constraints required)

Solution script (key lines):
```bash
python3 /app/decrypt_wal.py   # XOR-decrypts /app/main.db-wal in place
python3 /app/extract_data.py  # writes /app/recovered.json
# extract_data.py produces: [{"id":1,"name":"item1","value":X}, ...] sorted by id
```

Good instruction:
> I have a database in WAL (Write-Ahead Logging) mode in /app/. However, the WAL file
> appears to be corrupted or encrypted. When you try to access the database, SQLite may
> only show the base data (5 records) instead of all 11 records that should be there.
>
> Your task is to fix the WAL file so SQLite can read it, extract ALL data from the
> database (including WAL changes), and create a JSON file at /app/recovered.json.
> The output should have the format:
> [{"id": 1, "name": "item1", "value": X}, ...] sorted by id. You should recover all
> 11 records total.

Why it's good: specifies the JSON format and record count because tests check them exactly.

---

### Example 3 — feal-differential-cryptanalysis (interface specification)

Solution script (key lines):
```bash
cat << 'EOF' > /app/attack.py
def attack(encrypt_fn):
    # differential cryptanalysis to recover key[5]
    ...
    return key5_value  # uint32
EOF
```

Good instruction:
> The file /app/feal.py implements a FEAL-like encryption function. Implement a chosen
> plaintext attack that recovers the value of key[5]. Your attack should be implemented
> in /app/attack.py, and should implement a function called attack(encrypt_fn) that
> returns the uint32 value of key[5]. Your attack should run in less than 30 seconds.

Why it's good: specifies the interface (file path, function name, return type) because the test calls attack() directly; omits the cryptanalysis method.
"""

# =============================================================================
# Core Logic
# =============================================================================

def build_llm_input(info: dict, solve_sh: str) -> str:
    """Format info.json fields and solve.sh content as the LLM user message."""
    lines: list[str] = []

    title = info.get("title", "").strip()
    description = info.get("description", "").strip()

    lines.append("--- USER INTENT ---")
    if title:
        lines.append(f"Title: {title}")
    if description:
        lines.append(f"Description: {description}")
    lines.append("")
    lines.append("--- SOLUTION SCRIPT ---")
    lines.append(solve_sh.strip())

    return "\n".join(lines)


def generate_instruction_with_llm(
    rec_id: str,
    user_text: str,
    system_prompt: str,
    model: str,
    temperature: float = 0.5,
    max_tokens: int = 2048,
) -> tuple[Optional[str], float, int, int, Optional[str], int]:
    """
    Call the LLM to synthesize the instruction.md content.
    Retries up to 3 times on failure or length truncation.
    Returns:
      (instruction_markdown, total_cost_usd, total_prompt_tokens,
       total_completion_tokens, finish_reason, used_max_tokens)
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
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=temperature,
                max_tokens=token_budget,
            )
            choice = resp.choices[0]
            content = choice.message.content
            finish_reason = getattr(choice, "finish_reason", None)
            last_finish_reason = finish_reason

            # --- Cost and Usage Tracking ---
            usage = resp.usage or {}
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            total_prompt_tokens += prompt_tokens
            total_completion_tokens += completion_tokens

            cost_usd = 0.0

            # 1. Try to read exact cost from LiteLLM proxy headers
            if hasattr(resp, "_hidden_params") and "additional_headers" in resp._hidden_params:
                headers = resp._hidden_params["additional_headers"]
            elif hasattr(resp, "_hidden_params") and "headers" in resp._hidden_params:
                headers = resp._hidden_params["headers"]
            else:
                headers = {}

            cost_str = headers.get("x-litellm-response-cost") or headers.get("X-LiteLLM-Response-Cost")
            if cost_str is not None:
                try:
                    cost_usd = float(cost_str)
                except ValueError:
                    pass

            # 2. Fallback to litellm client's local cost calculation
            if cost_usd == 0.0:
                try:
                    cost_usd = litellm.completion_cost(completion_response=resp)
                except Exception:
                    pass

            # 3. Final fallback: manual estimation based on Haiku 4.5 pricing ($0.8/$4 per 1M)
            if cost_usd == 0.0 and (prompt_tokens or completion_tokens):
                cost_usd = (prompt_tokens * 0.8 + completion_tokens * 4.0) / 1_000_000
            total_cost_usd += cost_usd

            if finish_reason == "length":
                logger.warning(
                    "  [%s] Instruction hit max_tokens=%d (completion_tokens=%d); retrying with a larger budget.",
                    rec_id,
                    token_budget,
                    completion_tokens,
                )
                continue

            if content:
                return (
                    content.strip(),
                    total_cost_usd,
                    total_prompt_tokens,
                    total_completion_tokens,
                    finish_reason,
                    token_budget,
                )

        except litellm.ContextWindowExceededError as e:
            logger.error("  [%s] Context window exceeded! Error: %s", rec_id, e)
            return (
                None,
                total_cost_usd,
                total_prompt_tokens,
                total_completion_tokens,
                "context_window_exceeded",
                token_budget,
            )
        except Exception as e:
            logger.warning("  [%s] LLM attempt %d/%d failed: %s", rec_id, attempt, len(token_budgets), e)
            if attempt < len(token_budgets):
                time.sleep(5 * attempt)

    return (
        None,
        total_cost_usd,
        total_prompt_tokens,
        total_completion_tokens,
        last_finish_reason,
        token_budgets[-1],
    )

# =============================================================================
# File I/O
# =============================================================================

def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

# =============================================================================
# Main Orchestrator
# =============================================================================

def load_ids(args: argparse.Namespace, tasks_dir: Path) -> list[str]:
    """Load task IDs from the various input arguments, or auto-scan tasks_dir."""
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
        ids = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ids.append(line)
        return ids
    # Default: scan tasks_dir for all task subdirectories
    if not tasks_dir.exists():
        logger.error("Tasks directory not found: %s", tasks_dir)
        sys.exit(1)
    return sorted(p.name for p in tasks_dir.iterdir() if p.is_dir())


def process_task(
    rec_id: str,
    tasks_dir: Path,
    model: str,
    temperature: float,
    max_tokens: int,
) -> tuple[str, bool, float, int, int]:
    task_dir = tasks_dir / rec_id

    solve_path = task_dir / "solution" / "solve.sh"
    if not solve_path.exists():
        logger.warning("  -> Skipping %s: solution/solve.sh not found", rec_id)
        return rec_id, False, 0.0, 0, 0

    solve_sh = solve_path.read_text(encoding="utf-8")
    if not solve_sh.strip():
        logger.warning("  -> Skipping %s: solution/solve.sh is empty", rec_id)
        return rec_id, False, 0.0, 0, 0

    info_path = task_dir / "source" / "info.json"
    if not info_path.exists():
        logger.warning("  [%s] source/info.json not found, proceeding without user intent", rec_id)
    info = load_json(info_path)
    user_text = build_llm_input(info, solve_sh)

    instruction_md, cost, p_tokens, c_tokens, finish_reason, used_max_tokens = generate_instruction_with_llm(
        rec_id, user_text, SYSTEM_PROMPT, model, temperature, max_tokens
    )

    if not instruction_md:
        logger.error(
            "  -> Failed to synthesize instruction for %s (finish_reason=%s, max_tokens=%d)",
            rec_id, finish_reason, used_max_tokens,
        )
        return rec_id, False, cost, p_tokens, c_tokens

    out_path = task_dir / "instruction.md"
    out_path.write_text(instruction_md, encoding="utf-8")

    artifacts_dir = task_dir / "pipeline_artifacts" / "instruction"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "instruction_metadata.json").write_text(json.dumps({
        "model": model,
        "cost_usd": cost,
        "prompt_tokens": p_tokens,
        "completion_tokens": c_tokens,
        "finish_reason": finish_reason,
        "max_tokens": used_max_tokens,
        "timestamp": time.time(),
    }, indent=2), encoding="utf-8")

    logger.info(
        "  -> Saved instruction for %s (cost: $%.4f, finish_reason=%s)",
        rec_id, cost, finish_reason,
    )
    return rec_id, True, cost, p_tokens, c_tokens

def main() -> None:
    parser = argparse.ArgumentParser(description="Synthesize instruction.md for scaled terminal tasks")
    parser.add_argument("--tasks-dir", default=str(Path(__file__).parent.parent / "data" / "scaled_tasks_v1"))
    parser.add_argument("--log-file", default="logs/generate_instruction.log")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--temperature", type=float, default=0.5)
    parser.add_argument("--max-tokens", type=int, default=2048)

    id_group = parser.add_mutually_exclusive_group(required=False)
    id_group.add_argument("--input", help="JSON file whose keys are task IDs.")
    id_group.add_argument("--id-file", help="Text file with one task ID per line.")
    id_group.add_argument("--ids", nargs="+", metavar="ID")

    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--resume", action="store_true", help="Skip if instruction.md already exists")
    parser.add_argument("--workers", type=int, default=1)

    args = parser.parse_args()

    global logger
    logger = setup_logger(Path(args.log_file))
    tasks_dir = Path(args.tasks_dir)

    all_ids = load_ids(args, tasks_dir)

    pending = []
    for rec_id in all_ids:
        instruction_path = tasks_dir / rec_id / "instruction.md"
        if args.resume and instruction_path.exists():
            continue
        pending.append(rec_id)

    if args.limit:
        pending = pending[:args.limit]

    logger.info("Found %d tasks to process (%d workers)", len(pending), args.workers)

    total_cost_usd = 0.0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    success_count = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_to_id = {
            executor.submit(
                process_task,
                rec_id,
                tasks_dir,
                args.model,
                args.temperature,
                args.max_tokens,
            ): rec_id
            for rec_id in pending
        }
        for i, future in enumerate(as_completed(future_to_id), 1):
            rec_id = future_to_id[future]
            try:
                _, ok, cost, p_tokens, c_tokens = future.result()
            except Exception as e:
                logger.exception("  -> Unexpected failure for %s: %s", rec_id, e)
                error_count += 1
                continue

            total_cost_usd += cost
            total_prompt_tokens += p_tokens
            total_completion_tokens += c_tokens
            if ok:
                success_count += 1
            else:
                error_count += 1
            if i % 100 == 0:
                logger.info("Progress: %d/%d done, $%.4f so far", i, len(pending), total_cost_usd)

    logger.info("=" * 50)
    logger.info("Done: %d success, %d errors, total cost $%.4f", success_count, error_count, total_cost_usd)
    logger.info(
        "LLM usage: prompt_tokens=%d  completion_tokens=%d",
        total_prompt_tokens, total_completion_tokens,
    )

if __name__ == "__main__":
    main()
