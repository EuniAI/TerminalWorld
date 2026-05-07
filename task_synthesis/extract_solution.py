#!/usr/bin/env python3
"""
Extract Golden Solution from raw terminal transcripts.

Reads source/recording.txt for each task ID, asks an LLM to distill the session
into a clean, executable solve.sh that writes its final result to /app/ files
(state-based output). Writes to solution/solve.sh.

Usage:
    python extract_solution.py --ids 11758
    python extract_solution.py --id-file ../task_scaling/scaling_task_v1.txt --resume --workers 10
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

litellm.drop_params = True
litellm.suppress_debug_info = True
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

# =============================================================================
# Logging
# =============================================================================

logger = logging.getLogger("extract_solution")

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
# Prompt
# =============================================================================

SYSTEM_PROMPT_EXTRACT = """\
You are an expert Linux system administrator and Bash scripting specialist.
Extract the state-changing commands from this terminal transcript segment into a clean bash script.

## Extraction Rules

The transcript is noisy — it contains shell prompts, command outputs, errors,
exploratory reads, repeated attempts, and multiline constructs.

1. Infer the final successful workflow from the transcript.
2. REMOVE noise: pure outputs, repeated prompts, exploratory reads (ls, cat,
   pwd, ps) unless they materially contribute to the workflow.
3. REMOVE obvious failed attempts and typos when a corrected version follows.
4. KEEP state-changing commands: package installs, downloads, file writes,
   permission changes, config edits, process/service operations, script runs.
5. KEEP multiline constructs (heredocs, file-writing blocks) when they matter.
6. RETAIN required variables and directory changes used by later commands.
7. Do NOT invent commands not clearly supported by the transcript.
8. Prefer the cleaner final successful path when multiple variants were tried.

## Output Format

Output ONLY a Markdown bash code block. No conversational filler.
"""

SYSTEM_PROMPT_REFINE = """\
You are an expert Linux system administrator and Bash scripting specialist.
You are given a raw extracted bash script from a terminal recording. Your job is to
refine it into a clean, production-quality solve.sh.

## Refinement Rules

1. Remove duplicate commands (e.g. repeated apt-get update from multiple chunks).
2. Remove any remaining exploratory commands (ls, cat for inspection, pwd, ps).
3. Ensure logical ordering — setup/install before use.
4. **State-based outputs**: the script MUST write its final result to a file under /app/.
   Tests verify filesystem state, not stdout.
   - Bad:  `echo "Password is 1234"`
   - Good: `echo "1234" > /app/password.txt`
5. **Match computation style:**
   - Simple state changes → direct shell commands.
   - Complex computation → write script with `cat << 'EOF' > /app/script.py`, then run it.
6. Do NOT add progress/completion banners (`echo "Done!"` etc.).
7. Output paths must be simple and predictable: `/app/result.txt`, `/app/output.json`, etc.
8. Do NOT invent commands not present in the input script.

## Examples

### Crack archive → write secret to file
```bash
#!/bin/bash
set -e
apt-get update -qq
apt-get install -y libcompress-raw-lzma-perl 7zip
/app/john/run/7z2john.pl /app/secrets.7z > /app/secrets.hash
/app/john/run/john /app/secrets.hash > /app/cracked.txt
7z x -p1998 /app/secrets.7z -o/app
cat /app/secrets/secret_file.txt > /app/solution.txt
```

### Decrypt + query DB → write JSON
```bash
#!/bin/bash
set -e
cat << 'EOF' > /app/decrypt_wal.py
with open('/app/main.db-wal', 'rb') as f:
    data = f.read()
with open('/app/main.db-wal', 'wb') as f:
    f.write(bytes(b ^ 0x42 for b in data))
EOF
python3 /app/decrypt_wal.py
cat << 'EOF' > /app/extract_data.py
import sqlite3, json
conn = sqlite3.connect('/app/main.db')
rows = conn.execute('SELECT id, name, value FROM items ORDER BY id').fetchall()
with open('/app/recovered.json', 'w') as f:
    json.dump([{"id": r[0], "name": r[1], "value": r[2]} for r in rows], f, indent=2)
EOF
python3 /app/extract_data.py
```

## Output Format

Output ONLY a Markdown bash code block. No conversational filler.
"""

# =============================================================================
# Input Building
# =============================================================================

def build_llm_chunks(
    recording_text: str,
    line_char_limit: int = 200,
    max_lines_per_chunk: int = 500,
) -> list[str]:
    """Split recording into fixed-size line chunks for separate LLM calls."""
    processed: list[str] = []
    for line in recording_text.splitlines():
        line = line.rstrip()
        if len(line) > line_char_limit:
            line = line[:line_char_limit] + " ...[truncated]"
        processed.append(line)

    chunks: list[str] = []
    total = len(processed)
    for start in range(0, total, max_lines_per_chunk):
        chunk_lines = processed[start:start + max_lines_per_chunk]
        end = start + len(chunk_lines)
        header = f"--- RAW TERMINAL TRANSCRIPT (lines {start + 1}-{end} of {total}) ---"
        body = "\n".join(f"{start + j + 1:04d}| {l}" for j, l in enumerate(chunk_lines))
        chunks.append(header + "\n" + body)

    return chunks


def script_has_body(script: str) -> bool:
    """Return True if the script has commands beyond shebang and set -e."""
    for line in script.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#!") and stripped != "set -e" and not stripped.startswith("#"):
            return True
    return False


def merge_scripts(scripts: list[str]) -> str:
    """Concatenate multiple solve.sh scripts, skipping empty-body chunks."""
    non_empty = [s for s in scripts if script_has_body(s)]
    if not non_empty:
        return scripts[0] if scripts else ""
    if len(non_empty) == 1:
        return non_empty[0]

    parts = [non_empty[0]]
    for script in non_empty[1:]:
        body_lines = []
        for line in script.splitlines():
            stripped = line.strip()
            if stripped.startswith("#!") or stripped == "set -e":
                continue
            body_lines.append(line)
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        if body_lines:
            parts.append("\n".join(body_lines))

    return "\n\n".join(parts)

# =============================================================================
# LLM Call
# =============================================================================

def call_llm(
    rec_id: str,
    system_prompt: str,
    user_text: str,
    model: str,
    temperature: float,
    max_tokens: int,
) -> tuple[Optional[str], float, int, int]:
    for attempt in range(3):
        try:
            resp = litellm.completion(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content

            usage = resp.usage or {}
            p_tok = getattr(usage, "prompt_tokens", 0) or 0
            c_tok = getattr(usage, "completion_tokens", 0) or 0

            cost_usd = 0.0
            headers = {}
            if hasattr(resp, "_hidden_params"):
                headers = (resp._hidden_params.get("additional_headers")
                           or resp._hidden_params.get("headers") or {})
            cost_str = headers.get("x-litellm-response-cost") or headers.get("X-LiteLLM-Response-Cost")
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
            if cost_usd == 0.0 and (p_tok or c_tok):
                cost_usd = (p_tok * 0.15 + c_tok * 0.60) / 1_000_000

            if not content:
                continue

            # Strip markdown fences
            script = content.strip()
            for prefix in ("```bash", "```shell", "```"):
                if script.startswith(prefix):
                    script = script[len(prefix):]
                    break
            if script.endswith("```"):
                script = script[:-3]
            script = script.strip()

            # Strip shebang/set-e lines to get the real body
            body_lines = [
                l for l in script.splitlines()
                if l.strip() and not l.strip().startswith("#!") and l.strip() != "set -e"
            ]
            if not body_lines:
                return None, cost_usd, p_tok, c_tok

            # Prepend shebang + set -e only when there is actual content
            script = "#!/bin/bash\nset -e\n\n" + "\n".join(body_lines)
            return script, cost_usd, p_tok, c_tok

        except Exception as e:
            logger.warning("  [%s] LLM attempt %d/3 failed: %s", rec_id, attempt + 1, e)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))

    return None, 0.0, 0, 0

# =============================================================================
# Per-Task Logic
# =============================================================================

def process_task(
    rec_id: str,
    tasks_dir: Path,
    model: str,
    temperature: float,
    max_tokens: int,
    line_char_limit: int,
    max_lines_per_chunk: int,
    refine_only: bool = False,
) -> tuple[str, bool, float, int, int]:
    task_dir = tasks_dir / rec_id
    total_cost, total_p, total_c = 0.0, 0, 0

    if refine_only:
        solve_path = task_dir / "solution" / "solve.sh"
        if not solve_path.exists():
            logger.warning("  -> Skipping %s: solution/solve.sh not found", rec_id)
            return rec_id, False, 0.0, 0, 0
        existing = solve_path.read_text(encoding="utf-8")
        refined, cost, p_tok, c_tok = call_llm(
            rec_id, SYSTEM_PROMPT_REFINE, existing, model, temperature, max_tokens)
        total_cost += cost
        total_p += p_tok
        total_c += c_tok
        if refined is None:
            solve_path.unlink()
            logger.warning("  -> Removed %s: no state-changing commands after refine", rec_id)
            return rec_id, False, total_cost, total_p, total_c
        combined = refined
        chunks_count = 0
    else:
        recording_path = task_dir / "source" / "recording.txt"
        if not recording_path.exists():
            logger.warning("  -> Skipping %s: source/recording.txt not found", rec_id)
            return rec_id, False, 0.0, 0, 0

        recording_text = recording_path.read_text(encoding="utf-8", errors="replace")
        if not recording_text.strip():
            logger.warning("  -> Skipping %s: recording.txt is empty", rec_id)
            return rec_id, False, 0.0, 0, 0

        chunks = build_llm_chunks(recording_text, line_char_limit, max_lines_per_chunk)
        chunks_count = len(chunks)
        logger.info("  [%s] %d chunk(s) from %d lines",
                    rec_id, chunks_count, len(recording_text.splitlines()))

        scripts: list[str] = []
        for idx, chunk in enumerate(chunks):
            script, cost, p_tok, c_tok = call_llm(
                rec_id, SYSTEM_PROMPT_EXTRACT, chunk, model, temperature, max_tokens)
            total_cost += cost
            total_p += p_tok
            total_c += c_tok
            if script:
                scripts.append(script)
            else:
                logger.warning("  [%s] chunk %d/%d extraction failed", rec_id, idx + 1, chunks_count)

        if not scripts:
            logger.error("  -> Failed to extract solution for %s", rec_id)
            return rec_id, False, total_cost, total_p, total_c

        merged = merge_scripts(scripts)
        refined, cost, p_tok, c_tok = call_llm(
            rec_id, SYSTEM_PROMPT_REFINE, merged, model, temperature, max_tokens)
        total_cost += cost
        total_p += p_tok
        total_c += c_tok
        combined = refined if refined else merged
        if not refined:
            logger.warning("  [%s] refine failed, using merged script", rec_id)

    if not script_has_body(combined):
        logger.warning("  -> Skipping %s: no state-changing commands found", rec_id)
        return rec_id, False, total_cost, total_p, total_c

    solution_dir = task_dir / "solution"
    solution_dir.mkdir(parents=True, exist_ok=True)
    out_path = solution_dir / "solve.sh"
    out_path.write_text(combined, encoding="utf-8")
    out_path.chmod(0o755)

    artifacts_dir = task_dir / "pipeline_artifacts" / "solution"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "model": model,
        "cost_usd": total_cost,
        "prompt_tokens": total_p,
        "completion_tokens": total_c,
        "chunks": chunks_count,
        "refine_only": refine_only,
        "timestamp": time.time(),
    }
    (artifacts_dir / "solution_metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    logger.info("  -> Saved solve.sh for %s (%d lines, $%.4f)",
                rec_id, len(combined.splitlines()), total_cost)
    return rec_id, True, total_cost, total_p, total_c

# =============================================================================
# ID Loading
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
        if isinstance(data, dict):
            return list(data.keys())
        logger.error("Expected a JSON object (dict) in %s", path)
        sys.exit(1)
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

# =============================================================================
# Main
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Extract solve.sh from terminal recordings")
    parser.add_argument("--tasks-dir", default=str(Path(__file__).parent.parent / "data" / "scaled_tasks"))
    parser.add_argument("--log-file", default="logs/extract_solution.log")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--line-char-limit", type=int, default=200)
    parser.add_argument("--max-lines-per-chunk", type=int, default=500)

    id_group = parser.add_mutually_exclusive_group(required=False)
    id_group.add_argument("--input", help="JSON file whose keys are task IDs.")
    id_group.add_argument("--id-file", help="Text file with one task ID per line.")
    id_group.add_argument("--ids", nargs="+", metavar="ID")

    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--resume", action="store_true", help="Skip if solution/solve.sh already exists")
    parser.add_argument("--refine-only", action="store_true",
                        help="Refine existing solution/solve.sh without re-extracting from recording")
    parser.add_argument("--workers", type=int, default=1)

    args = parser.parse_args()

    global logger
    logger = setup_logger(Path(args.log_file))
    tasks_dir = Path(args.tasks_dir)

    all_ids = load_ids(args, tasks_dir)

    pending = []
    for rec_id in all_ids:
        solve_path = tasks_dir / rec_id / "solution" / "solve.sh"
        if args.refine_only and not solve_path.exists():
            continue  # nothing to refine
        if not args.refine_only and args.resume and solve_path.exists():
            continue  # already extracted
        pending.append(rec_id)

    if args.limit:
        pending = pending[:args.limit]

    logger.info("Found %d tasks to process (%d workers)", len(pending), args.workers)

    total_cost = 0.0
    success_count = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_to_id = {
            executor.submit(
                process_task,
                rec_id, tasks_dir, args.model, args.temperature,
                args.max_tokens, args.line_char_limit, args.max_lines_per_chunk, args.refine_only,
            ): rec_id
            for rec_id in pending
        }
        for i, future in enumerate(as_completed(future_to_id), 1):
            rec_id = future_to_id[future]
            try:
                _, ok, cost, _, _ = future.result()
            except Exception as e:
                logger.exception("  -> Unexpected failure for %s: %s", rec_id, e)
                error_count += 1
                continue
            total_cost += cost
            if ok:
                success_count += 1
            else:
                error_count += 1
            if i % 100 == 0:
                logger.info("Progress: %d/%d done, $%.4f so far", i, len(pending), total_cost)

    logger.info("=" * 50)
    logger.info("Done: %d success, %d errors, total cost $%.4f", success_count, error_count, total_cost)

if __name__ == "__main__":
    main()
