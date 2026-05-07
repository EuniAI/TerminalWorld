#!/usr/bin/env python3
"""
Score terminal recordings for training data value.

A two-stage evaluation framework:
  Stage 1 (Feasibility): Rule-based checks to determine if a recording can be
                         reproduced and is non-trivial. Zero LLM cost.
  Stage 2 (Value):       LLM-based scoring of the trajectory's informational
                         quality across three orthogonal dimensions.

The final decision is a policy that combines the two stages:
  - ACCEPT_GOLDEN:  Total score >= 10. Perfect trajectory + code repository.
  - ACCEPT_SILVER:  Total score 7-9. High quality standalone trajectory or good trajectory with repo.
  - ACCEPT_BRONZE:  Total score 4-6. Medium quality trajectory, usable for training.
  - REJECT_IRON:    Total score 1-3. Low tier, rejected but kept for potential future salvage (e.g. repo mining).
  - REJECT_ZERO:    Total score 0. Completely useless trajectory with no context.

Input:  data/recordings/{id}/  (info.json, commands.json, analysis.json)
Output: output/recording_value_scores.json

Usage:
    python score_value.py
    python score_value.py --model claude-sonnet-4-5 --limit 50
    python score_value.py --resume
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import json_repair

# Add environment_building to path for imports
env_build_dir = Path(__file__).parent.parent / "environment_building"
sys.path.append(str(env_build_dir))

import litellm

from analyze_duration import parse_duration
from analyze_recording import load_recording, signals_to_dict

litellm.drop_params = True
litellm.suppress_debug_info = True
logging.getLogger("LiteLLM").setLevel(logging.WARNING)


# =============================================================================
# Data Structures
# =============================================================================


@dataclass
class FeasibilityReport:
    """
    Result of Stage 1: rule-based feasibility assessment.
    Answers: "Can this recording be reproduced and is it worth evaluating?"
    """
    is_fatal: bool = False          # True if environment is fundamentally unreproducible
    is_trivial: bool = False        # True if the trajectory has no meaningful content
    fatal_reason: str = ""          # Human-readable reason for a fatal rejection
    trivial_reason: str = ""        # Human-readable reason for a trivial rejection
    high_cost_tags: list[str] = field(default_factory=list)  # e.g. ["gpu_workload", "docker_in_docker"]


@dataclass
class LLMScore:
    """
    Result of Stage 2: LLM-based trajectory value scoring.
    Answers: "How informative is this trajectory for training an agent?"
    """
    state_action_alignment: int = 0     # 0-3: Are actions logically derived from terminal state?
    task_complexity: int = 0            # 0-3: Does the session involve non-trivial decisions?
    signal_clarity: int = 0             # 0-3: Is there a clear success/failure observable outcome?
    total: int = 0                      # Sum of the three dimensions (0-9)
    reasoning: str = ""                 # LLM's detailed rationale citing evidence
    prompt_tokens: int = 0              # Input tokens consumed by this LLM call
    completion_tokens: int = 0          # Output tokens consumed by this LLM call
    cost_usd: float = 0.0               # Estimated cost of this LLM call in USD


@dataclass
class EvaluationResult:
    """
    Final consolidated result for a single recording.
    """
    recording_id: str = ""
    decision: str = ""                  # ACCEPT_GOLDEN, ACCEPT_SILVER, ACCEPT_BRONZE, REJECT
    reject_reason: str = ""             # Only populated on REJECT
    context_level: int = 0             # 0=none, 1=dead link, 2=web page, 3=code repository
    total_score: int = 0               # Combined score: llm_total (0-9) + context_level (0-3) = 0-12
    llm_score: Optional[LLMScore] = None
    feasibility: Optional[FeasibilityReport] = None
    high_cost_tags: list[str] = field(default_factory=list)


# =============================================================================
# LLM Prompts
# =============================================================================

# Recordings with more than this many commands get the detailed prompt;
# shorter sessions use the compact prompt to save tokens.
_LONG_PROMPT_THRESHOLD = 15

_SYSTEM_PROMPT_LONG = """\
You are a data quality evaluator for terminal recording datasets.
These recordings will be used to train an AI agent that operates in a terminal.

Your task: evaluate a terminal recording across THREE dimensions, then give a verdict.

======================================================================
DIMENSION 1: State-Action Alignment (0-3)
======================================================================
Can an observer reconstruct WHY each command was executed, purely from
the visible terminal history (commands + their stdout/stderr)?

- 0: Unreadable. Commands appear random or entirely depend on knowledge
     not present in the terminal (e.g., user silently reads a webpage,
     then types a command with no visible trigger).
- 1: Mostly opaque. A few commands make sense, but the majority lack
     visible motivation.
- 2: Mostly legible. Most commands have a clear trigger visible in
     prior output (error messages, file listings, build output), but
     some steps still lack visible grounding.
- 3: Fully legible. Every non-trivial command is a direct, traceable
     response to something visible in the terminal. An AI could learn
     the state → action mapping from this trajectory alone.

POSITIVE EXAMPLE (score 3):
  $ git clone https://github.com/user/project && cd project
  $ make
    > error: gcc not found
  $ sudo apt-get install -y gcc
  $ make
    > Build successful
  → Every command is a visible response to the prior output.

NEGATIVE EXAMPLE (score 0):
  $ vim ~/.config/special/app.conf
  $ curl http://10.0.1.5:8080/api/restart
  $ ssh deploy@prod-server
  → No visible context for why these commands are executed.

======================================================================
DIMENSION 2: Task Complexity (0-3)
======================================================================
How many commands in this session reflect a NON-TRIVIAL decision —
a choice that requires technical judgment, not just mechanical typing?

Trivial (not counted): cd, ls, pwd, cat, echo, clear, history, exit
Low-decision: running a command from a README verbatim (pip install -r requirements.txt)
High-decision: choosing a specific fix for an error, selecting between alternatives,
               adjusting flags/versions based on observed output

- 0: Zero non-trivial decisions. Entire session is navigation/inspection.
- 1: 1-2 low-decision commands (e.g., one install, one script run).
- 2: Multiple commands show genuine problem-solving or environment
     adaptation (e.g., pinning a version after a conflict, choosing
     between build systems).
- 3: Dense with non-trivial decisions throughout. The session
     demonstrates expert-level tool selection, debugging, or multi-step
     problem solving.

POSITIVE EXAMPLE (score 3):
  $ python train.py → CUDA out of memory
  $ python train.py --batch-size 16 --fp16 → loss is NaN
  $ python train.py --batch-size 16 --fp16 --grad-clip 1.0 → training starts
  → Each retry adapts based on the specific error observed.

NEGATIVE EXAMPLE (score 0):
  $ cd project
  $ ls
  $ cat README.md
  $ ls src/
  $ cat src/main.py
  → Pure browsing, zero decisions.

======================================================================
DIMENSION 3: Signal Clarity (0-3)
======================================================================
Does the session produce a clear, observable success or failure signal
that could be used to judge whether the task was completed?

- 0: No outcome signal at all. Session just stops or user exits.
- 1: Weak implicit signal (e.g., user moves on to something else,
     suggesting maybe the prior task succeeded, but overall task
     completion remains ambiguous).
- 2: Clear signal for the main task (e.g., tests pass, build succeeds,
     server starts and responds).
- 3: Unambiguous end-to-end signal: the session starts with a clear
     goal, and ends with definitive evidence of success or failure
     (exit code, test results, working output).

POSITIVE EXAMPLE (score 3):
  $ pytest
    > 12 passed, 0 failed
  → Unambiguous success signal.

NEGATIVE EXAMPLE (score 0):
  $ nano config.yml    (editor opens, user exits)
  $ exit
  → No way to know what happened or whether anything was achieved.

======================================================================
RESPONSE FORMAT (JSON only, no other text)
======================================================================
{
  "state_action_alignment": <0-3>,
  "task_complexity": <0-3>,
  "signal_clarity": <0-3>,
  "reasoning": "<3-5 sentences: cite specific commands or outputs as evidence for each dimension score>"
}"""

_SYSTEM_PROMPT_SHORT = """\
You evaluate terminal recordings for AI training value.
Score these 3 dimensions (0-3 each).

state_action_alignment: Can you explain WHY each command was run from visible terminal output alone?
  0=completely opaque, 1=mostly opaque, 2=mostly legible, 3=fully legible
task_complexity: How many commands require real technical judgment (not just cd/ls/cat/echo)?
  0=zero non-trivial, 1=1-2 low-decision, 2=genuine problem-solving, 3=expert-level throughout
signal_clarity: Is there a clear success/failure signal observable in the terminal output?
  0=no signal, 1=weak/ambiguous, 2=clear for main task, 3=unambiguous end-to-end

Respond with JSON only:
{
  "state_action_alignment": <0-3>,
  "task_complexity": <0-3>,
  "signal_clarity": <0-3>,
  "reasoning": "<3-5 sentences: cite specific commands/outputs as evidence>"
}"""

# =============================================================================
# Stage 1: Feasibility Assessment (Rule-Based, Zero LLM Cost)
# =============================================================================


# --- Fatal Blockers: environments we fundamentally cannot reproduce ---
_FATAL_TAGS = {
    "windows_recording",
    "gui_application",
    "proprietary_software",
}

# --- High Cost Warnings: possible to build, but with extra effort ---
_HIGH_COST_TAGS = {
    "gpu_workload",
    "docker_in_docker",
    "kernel_operations",
    "systemd_init",
    "interactive_build",
    "large_data_download",
    "macos_specific",
    "multi_machine",
}

# --- Trivial commands that alone are not worth learning from ---
_TRIVIAL_COMMANDS = {"cd", "ls", "cat", "pwd", "echo", "clear", "history", "exit", "man", "which"}

# --- Minimum duration in seconds for a recording to be considered non-trivial ---
_MIN_DURATION_SECONDS = 10

# --- Minimum number of commands for a recording to be considered non-trivial ---
_MIN_COMMAND_COUNT = 3


def assess_feasibility(analysis: dict) -> FeasibilityReport:
    """
    Stage 1: Evaluate the feasibility and triviality of a recording.

    Checks (in priority order):
      1. Fatal environment blockers (Windows, GUI, proprietary)
      2. Trivial trajectory (too few commands, all trivial, too short)
      3. High-cost warnings (GPU, Docker-in-Docker, etc.)

    Returns a FeasibilityReport. The caller should check is_fatal and
    is_trivial before proceeding to Stage 2 (LLM scoring).
    """
    report = FeasibilityReport()

    # --- Check 1: Fatal environment blockers ---
    # special_cases is a list of dicts with a "tag" key, produced by analyze_recording.py
    special_cases = analysis.get("special_cases", [])
    tags = {sc.get("tag") for sc in special_cases if sc.get("tag")}
    found_fatal = tags & _FATAL_TAGS
    if found_fatal:
        report.is_fatal = True
        report.fatal_reason = f"Unreproducible environment: {sorted(found_fatal)}"
        return report

    # --- Check 2: Trivial trajectory ---
    commands = analysis.get("commands_with_output", [])

    if len(commands) < _MIN_COMMAND_COUNT:
        report.is_trivial = True
        report.trivial_reason = f"Too few commands: {len(commands)} < {_MIN_COMMAND_COUNT}"
        return report

    non_trivial = [
        cmd for cmd in commands
        if cmd.get("command", "").split()[0:1] and
           cmd["command"].split()[0] not in _TRIVIAL_COMMANDS
    ]
    if not non_trivial:
        report.is_trivial = True
        report.trivial_reason = "All commands are trivial read-only operations (cd/ls/cat/…)"
        return report

    duration = analysis.get("duration", 0.0)
    if duration and duration < _MIN_DURATION_SECONDS:
        report.is_trivial = True
        report.trivial_reason = f"Recording too short: {duration:.1f}s < {_MIN_DURATION_SECONDS}s"
        return report

    # --- Check 3: High-cost warnings ---
    report.high_cost_tags = sorted(tags & _HIGH_COST_TAGS)

    return report


# =============================================================================
# Stage 2: Contextual Richness Assessment (Rule-Based)
# =============================================================================


def _is_repo_url(url: str) -> bool:
    """Return True if the URL points to a clonable code repository."""
    repo_patterns = [
        r"github\.com/[^/]+/[^/]+",
        r"gitlab\.com/[^/]+/[^/]+",
        r"bitbucket\.org/[^/]+/[^/]+",
        r"codeberg\.org/[^/]+/[^/]+",
        r"gitea\.[^/]+/[^/]+/[^/]+",
        r"sr\.ht/~[^/]+/[^/]+",
    ]
    return any(re.search(pat, url, re.IGNORECASE) for pat in repo_patterns)


def _url_is_accessible(url: str) -> bool:
    """Return True if the URL responds with a 2xx/3xx HTTP status code."""
    try:
        import urllib.request
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status < 400
    except Exception:
        return False


def determine_context_level(analysis: dict) -> int:
    """
    Assess the contextual richness of a recording based on its external URLs.

    Returns:
        0 — No external URLs found.
        1 — URLs found but none are accessible (dead links / private repos).
        2 — Accessible URLs found, but they are web pages, not repositories.
        3 — At least one accessible, clonable code repository found.
    """
    raw_urls = analysis.get("external_urls", analysis.get("source_code_urls", []))
    if not raw_urls:
        return 0

    urls: list[str] = []
    for entry in raw_urls:
        if isinstance(entry, str):
            url = entry
        elif isinstance(entry, dict):
            url = entry.get("url", "")
        else:
            url = ""
        if url:
            urls.append(url)

    if not urls:
        return 0

    has_accessible = False
    has_repo = False

    for url in urls:
        if not _url_is_accessible(url):
            continue
        has_accessible = True
        if _is_repo_url(url):
            has_repo = True
            break  # Found what we need; no point checking further

    if has_repo:
        return 3
    if has_accessible:
        return 2
    return 1


# =============================================================================
# Stage 3: LLM Value Scoring
# =============================================================================


def build_llm_input(analysis: dict, max_commands: int = 40) -> str:
    """
    Format a recording's trajectory as a structured text prompt for the LLM.
    Includes: title, description, environment, and the command-output pairs.
    """
    lines: list[str] = []

    title = analysis.get("title", "").strip()
    description = analysis.get("description", "").strip()
    env = analysis.get("environment", "").strip()

    if title:
        lines.append(f"Title: {title}")
    if description:
        desc_preview = (description[:500] + "...") if len(description) > 500 else description
        lines.append(f"Description: {desc_preview}")
    if env:
        lines.append(f"Environment: {env}")
    lines.append("")

    commands = analysis.get("commands_with_output", [])
    lines.append(f"Command trajectory ({len(commands)} commands):")

    for cmd in commands[:max_commands]:
        command_text = cmd.get("command", "").strip()
        # Prevent excessively long commands (e.g., base64 payloads, misidentified output) from blowing up the context window
        if len(command_text) > 1000:
            command_text = command_text[:500] + "\n... [Command truncated due to length] ...\n" + command_text[-500:]

        output = cmd.get("output", "").strip()
        if output:
            # Prevent excessively long single-command outputs from blowing up the context window
            if len(output) > 2000:
                output = output[:1000] + "\n... [Output truncated due to length] ...\n" + output[-1000:]

            indented = output.replace("\n", "\n    ")
            lines.append(f"  $ {command_text}\n    > {indented}")
        else:
            lines.append(f"  $ {command_text}")

    if len(commands) > max_commands:
        lines.append(f"  ... ({len(commands) - max_commands} more commands omitted)")

    return "\n".join(lines)


def score_with_llm(
    rec_id: str,
    analysis: dict,
    model: str,
    temperature: float,
    max_tokens: int,
    logger: logging.Logger,
) -> Optional[LLMScore]:
    """
    Stage 3: Call the LLM to score the informational value of a trajectory.

    The LLM scores three orthogonal dimensions (0-3 each):
      - state_action_alignment: Are actions grounded in visible terminal state?
      - task_complexity:        Are there non-trivial decisions in the session?
      - signal_clarity:         Is there a clear observable success/failure signal?

    Retries up to 3 times on LLM failure. Returns None if all attempts fail.
    Token usage and estimated cost (USD) are captured in the returned LLMScore.
    """
    commands = analysis.get("commands_with_output", [])
    if len(commands) <= _LONG_PROMPT_THRESHOLD:
        system_prompt = _SYSTEM_PROMPT_SHORT
    else:
        system_prompt = _SYSTEM_PROMPT_LONG

    user_text = build_llm_input(analysis)

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

            content = resp.choices[0].message.content or ""
            logger.debug("  [%s] LLM raw response: %s", rec_id, content[:500])

            # Extract JSON from response: model may wrap it in markdown or preamble text
            json_str = content
            if "{" in content:
                # Find the content between the first { and the last }
                json_str = content[content.index("{"):content.rindex("}") + 1]

            # Handle invalid escape sequences generated by the LLM (e.g. unescaped \s, \w)
            # This replaces isolated backslashes with double backslashes unless followed by valid JSON escape chars
            json_str = re.sub(r'\\(?![/"\\bfnrtu])', r'\\\\', json_str)

            try:
                raw = json.loads(json_str)
                if not isinstance(raw, dict):
                    raise ValueError("Parsed JSON is not a dictionary")
            except Exception as e:
                # If standard parsing fails, attempt to repair the JSON string
                logger.warning("  [%s] JSON decode error: %s, attempting to repair JSON", rec_id, e)
                try:
                    raw = json_repair.loads(json_str)
                    if not isinstance(raw, dict):
                        raise ValueError("json_repair returned non-dict")
                except Exception as repair_e:
                    logger.warning("  [%s] json_repair failed: %s", rec_id, repair_e)
                    # Fallback to regex if json_repair also fails
                    match = re.search(r'\{[^{}]*\}', json_str, re.DOTALL)
                    if match:
                        json_str = match.group(0)
                        raw = json.loads(json_str)
                        if not isinstance(raw, dict):
                            raise ValueError("Fallback regex parsed JSON is not a dictionary")
                    else:
                        raise e

            saa = int(raw.get("state_action_alignment", 0))
            tc = int(raw.get("task_complexity", 0))
            sc = int(raw.get("signal_clarity", 0))
            reasoning = str(raw.get("reasoning", ""))

            # Guard against empty/degenerate responses (e.g. model returns {})
            if saa == 0 and tc == 0 and sc == 0 and not reasoning:
                logger.warning(
                    "  [%s] LLM attempt %d/3 returned all-zero with no reasoning, raw=%s",
                    rec_id, attempt + 1, content[:300],
                )
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                # Last attempt still degenerate: fall through and accept the zeros

            # Clamp to valid range
            saa = max(0, min(3, saa))
            tc = max(0, min(3, tc))
            sc = max(0, min(3, sc))

            usage = resp.usage or {}
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            
            cost_usd = 0.0

            # 1. Try to read exact cost from LiteLLM proxy headers
            if hasattr(resp, "_hidden_params") and "additional_headers" in resp._hidden_params:
                headers = resp._hidden_params["additional_headers"]
            elif hasattr(resp, "_hidden_params") and "headers" in resp._hidden_params:
                headers = resp._hidden_params["headers"]
            else:
                headers = {}

            # HTTP headers are typically case-insensitive, but httpx/requests might expose them in lower case
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

            # 3. Final fallback: manual estimation based on Sonnet 4.6 pricing ($3/$15 per 1M)
            # Used when proxy headers are missing and local litellm doesn't recognize the model name
            if cost_usd == 0.0 and (prompt_tokens or completion_tokens):
                cost_usd = (prompt_tokens * 3.0 + completion_tokens * 15.0) / 1_000_000

            return LLMScore(
                state_action_alignment=saa,
                task_complexity=tc,
                signal_clarity=sc,
                total=saa + tc + sc,
                reasoning=reasoning,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
            )

        except litellm.InternalServerError as e:
            # Handle Bedrock content_filtered guardrails that are bubbled up by litellm as InternalServerError
            error_str = str(e)
            if "finish_reason" in error_str and "content_filtered" in error_str:
                logger.warning("  [%s] Safety guardrail triggered (content_filtered), scoring as 0.", rec_id)
                # If safety filtering is triggered, treat it as lowest quality (0 score) without retrying.
                return LLMScore(
                    state_action_alignment=0, task_complexity=0, signal_clarity=0, total=0,
                    reasoning="Safety guardrail triggered (content_filtered). Evaluated as 0.",
                )
            # Otherwise, retry normally for genuine internal server errors
            logger.warning("  [%s] LLM InternalServerError attempt %d/3 failed: %s", rec_id, attempt + 1, e)
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
        except litellm.ContextWindowExceededError as e:
            # If the context is still exceeded despite truncation, log it and abort
            logger.error("  [%s] Context window exceeded despite truncation! Error: %s", rec_id, e)
            return None
        except Exception as e:
            logger.warning("  [%s] LLM attempt %d/3 failed: %s", rec_id, attempt + 1, e)
            if attempt < 2:
                time.sleep(5 * (attempt + 1))

    return None


# =============================================================================
# Final Decision Policy
# =============================================================================

# --- Thresholds for the decision matrix ---
# LLM scores three dimensions each 0-3; total range is 0-9.
_THRESHOLD_HIGH_VALUE = 7   # total >= 7 is "high value"   (≈ top 22% of range)
_THRESHOLD_MED_VALUE = 5    # total >= 5 is "medium value" (≈ top 44% of range)


def apply_decision_policy(
    rec_id: str,
    feasibility: FeasibilityReport,
    context_level: int,
    llm_score: Optional[LLMScore],
) -> EvaluationResult:
    """
    Apply the final decision policy based entirely on total score (0-12).
    """
    result = EvaluationResult(
        recording_id=rec_id,
        context_level=context_level,
        llm_score=llm_score,
        feasibility=feasibility,
        high_cost_tags=feasibility.high_cost_tags,
    )

    if feasibility.is_fatal:
        result.reject_reason = feasibility.fatal_reason
    elif feasibility.is_trivial:
        result.reject_reason = feasibility.trivial_reason

    # Calculate total score regardless of feasibility.
    # Even a fatal/trivial recording might have a repo (context_level=3)
    llm_total = llm_score.total if llm_score else 0
    result.total_score = llm_total + context_level

    if result.total_score >= 10:
        result.decision = "ACCEPT_GOLDEN"
    elif result.total_score >= 7:
        result.decision = "ACCEPT_SILVER"
    elif result.total_score >= 4:
        result.decision = "ACCEPT_BRONZE"
    elif result.total_score >= 1:
        result.decision = "REJECT_IRON"
    else:
        result.decision = "REJECT_ZERO"
        if not result.reject_reason:
            result.reject_reason = f"Zero total value (score={result.total_score})"

    return result


# =============================================================================
# I/O Helpers
# =============================================================================


def setup_logger(log_file: Path) -> logging.Logger:
    """Configure and return a logger that writes to both stdout and a log file."""
    logger = logging.getLogger("score_value")
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


def load_json(path: Path) -> dict | None:
    """Load a JSON file, returning None on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_json(path: Path, data: dict) -> None:
    """Atomically save a dict as a formatted JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Save to central output file
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def save_individual_score(rec_dir: Path, result_dict: dict) -> None:
    """Save the score result to the individual recording directory."""
    score_path = rec_dir / "score.json"
    tmp = score_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(score_path)


def result_to_dict(result: EvaluationResult) -> dict:
    """Serialize an EvaluationResult to a JSON-serializable dict."""
    llm = None
    if result.llm_score is not None:
        s = result.llm_score
        llm = {
            "state_action_alignment": s.state_action_alignment,
            "task_complexity": s.task_complexity,
            "signal_clarity": s.signal_clarity,
            "total": s.total,
            "reasoning": s.reasoning,
            "prompt_tokens": s.prompt_tokens,
            "completion_tokens": s.completion_tokens,
            "cost_usd": s.cost_usd,
        }

    feas = None
    if result.feasibility is not None:
        f = result.feasibility
        feas = {
            "is_fatal": f.is_fatal,
            "is_trivial": f.is_trivial,
            "fatal_reason": f.fatal_reason,
            "trivial_reason": f.trivial_reason,
            "high_cost_tags": f.high_cost_tags,
        }

    return {
        "recording_id": result.recording_id,
        "decision": result.decision,
        "total_score": result.total_score,
        "reject_reason": result.reject_reason,
        "context_level": result.context_level,
        "high_cost_tags": result.high_cost_tags,
        "llm_score": llm,
        "feasibility": feas,
    }


# =============================================================================
# Main Orchestrator
# =============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="Score recordings for training data value")
    parser.add_argument(
        "--recordings-dir",
        default=str(Path(__file__).parent.parent / "data" / "recordings"),
        help="Path to recordings directory",
    )
    parser.add_argument("--output", default="output/recording_value_scores.json")
    parser.add_argument("--log-file", default="logs/score_value.log")
    parser.add_argument("--model", default="claude-sonnet-4-5")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    output_file = Path(args.output)
    logger = setup_logger(Path(args.log_file))
    recordings_dir = Path(args.recordings_dir)

    existing: dict = {}
    if args.resume and output_file.exists():
        existing = load_json(output_file) or {}
        logger.info("Resume mode: %d already scored", len(existing))

    all_ids = sorted(
        [d.name for d in recordings_dir.iterdir() if d.is_dir() and d.name.isdigit()],
        key=int,
    )
    if args.limit:
        all_ids = all_ids[: args.limit]

    logger.info("Recordings to evaluate: %d  (model=%s)", len(all_ids), args.model)

    results = dict(existing)
    counts = {
        "accept_golden": 0, "accept_silver": 0, "accept_bronze": 0, "reject_iron": 0, "reject_zero": 0,
        "fatal_reject": 0, "trivial_reject": 0, "llm_reject": 0,
        "error": 0
    }

    # Restore decision counts from previous run so the final summary is accurate.
    for r in existing.values():
        decision = r.get("decision", "")
        if "GOLDEN" in decision:
            counts["accept_golden"] += 1
        elif "SILVER" in decision:
            counts["accept_silver"] += 1
        elif "BRONZE" in decision:
            counts["accept_bronze"] += 1
        elif "IRON" in decision:
            counts["reject_iron"] += 1
        elif "ZERO" in decision or decision == "REJECT":
            counts["reject_zero"] += 1
            # Try to infer the reject reason for historical stats
            feas = r.get("feasibility", {})
            if feas and feas.get("is_fatal"):
                counts["fatal_reject"] += 1
            elif feas and feas.get("is_trivial"):
                counts["trivial_reject"] += 1
            else:
                counts["llm_reject"] += 1

    # Restore accumulated cost/token stats from previous run (resume mode).
    # We sum over every entry that carries an llm_score so the final summary
    # always reflects the true total spend across all runs.
    total_cost_usd = sum(
        (r.get("llm_score") or {}).get("cost_usd", 0.0) for r in existing.values()
    )
    total_prompt_tokens = sum(
        (r.get("llm_score") or {}).get("prompt_tokens", 0) for r in existing.values()
    )
    total_completion_tokens = sum(
        (r.get("llm_score") or {}).get("completion_tokens", 0) for r in existing.values()
    )
    if existing:
        logger.info(
            "Restored prior cost: prompt_tokens=%d  completion_tokens=%d  total_cost=$%.4f",
            total_prompt_tokens, total_completion_tokens, total_cost_usd,
        )

    # -------------------------------------------------------------------------
    # Pre-scan: load all pending recordings once, classify into buckets, and
    # print an Evaluation Plan so the user knows what to expect before any LLM
    # calls are made.  This single pass also avoids loading recordings twice.
    # -------------------------------------------------------------------------
    logger.info("Pre-scanning recordings...")
    pending: list[tuple[str, dict]] = []   # (rec_id, analysis) for recordings that need evaluation
    plan = {"resumed": 0, "load_error": 0, "no_commands": 0,
            "fatal_reject": 0, "trivial_reject": 0,
            "llm_short": 0, "llm_long": 0}

    for rec_id in all_ids:
        if args.resume and rec_id in existing:
            plan["resumed"] += 1
            continue
        rec_dir = recordings_dir / rec_id
        try:
            signals = load_recording(rec_dir)
            analysis = signals_to_dict(signals)
        except Exception as e:
            logger.warning("  Pre-scan: failed to load %s: %s", rec_id, e)
            plan["load_error"] += 1
            continue

        info_path = rec_dir / "info.json"
        if info_path.exists():
            info_data = load_json(info_path) or {}
            analysis["duration"] = parse_duration(info_data.get("duration", "")) or 0.0

        if not analysis.get("commands_with_output"):
            plan["no_commands"] += 1
            continue

        feasibility = assess_feasibility(analysis)
        if feasibility.is_fatal:
            plan["fatal_reject"] += 1
        elif feasibility.is_trivial:
            plan["trivial_reject"] += 1
        elif len(analysis["commands_with_output"]) <= _LONG_PROMPT_THRESHOLD:
            plan["llm_short"] += 1
        else:
            plan["llm_long"] += 1

        pending.append((rec_id, analysis))

    llm_calls = plan["llm_short"] + plan["llm_long"]
    logger.info("=" * 50)
    logger.info("Evaluation Plan:")
    logger.info("  Resumed (already scored): %d", plan["resumed"])
    logger.info("  Load errors:              %d", plan["load_error"])
    logger.info("  No commands (skip):       %d", plan["no_commands"])
    logger.info("  Rule reject — fatal:      %d", plan["fatal_reject"])
    logger.info("  Rule reject — trivial:    %d", plan["trivial_reject"])
    logger.info("  LLM calls — short prompt: %d", plan["llm_short"])
    logger.info("  LLM calls — long prompt:  %d", plan["llm_long"])
    logger.info("  Total LLM calls:          %d", llm_calls)
    logger.info("=" * 50)
    logger.info("Starting evaluation (%d recordings)...", len(pending))

    for i, (rec_id, analysis) in enumerate(pending, 1):
        logger.info("[%d/%d] Evaluating %s ...", i, len(pending), rec_id)
        rec_dir = recordings_dir / rec_id

        # --- Stage 1: Feasibility Assessment ---
        feasibility = assess_feasibility(analysis)

        # --- Stage 2: Contextual Richness ---
        # Always run regardless of feasibility: even rejected recordings may have
        # a valid repo URL that is useful for downstream task synthesis.
        context_level = determine_context_level(analysis)

        if feasibility.is_fatal or feasibility.is_trivial:
            llm_score = None
            if feasibility.is_fatal:
                logger.info("  REJECT(fatal): %s", feasibility.fatal_reason)
            else:
                logger.info("  REJECT(trivial): %s", feasibility.trivial_reason)
        else:
            # --- Stage 3: LLM Value Scoring ---
            llm_score = score_with_llm(
                rec_id, analysis, args.model, args.temperature, args.max_tokens, logger
            )

            if llm_score is None:
                logger.warning("  LLM scoring failed for %s after retries, skipping.", rec_id)
                counts["error"] += 1
                continue

            total_cost_usd += llm_score.cost_usd
            total_prompt_tokens += llm_score.prompt_tokens
            total_completion_tokens += llm_score.completion_tokens

        # --- Final Decision Policy ---
        result = apply_decision_policy(rec_id, feasibility, context_level, llm_score)
        results[rec_id] = result_to_dict(result)

        decision = result.decision
        if "GOLDEN" in decision:
            counts["accept_golden"] += 1
        elif "SILVER" in decision:
            counts["accept_silver"] += 1
        elif "BRONZE" in decision:
            counts["accept_bronze"] += 1
        elif "IRON" in decision:
            counts["reject_iron"] += 1
        else:
            counts["reject_zero"] += 1
            if feasibility.is_fatal:
                counts["fatal_reject"] += 1
            elif feasibility.is_trivial:
                counts["trivial_reject"] += 1
            else:
                counts["llm_reject"] += 1

        if "REJECT" in decision:
            if feasibility.is_fatal:
                reject_type = f"{decision}(fatal)"
            elif feasibility.is_trivial:
                reject_type = f"{decision}(trivial)"
            else:
                reject_type = f"{decision}(llm)"
        else:
            reject_type = decision

        if llm_score:
            cmd_count = len(analysis.get("commands_with_output", []))
            prompt_type = "short" if cmd_count <= _LONG_PROMPT_THRESHOLD else "long"
            value_str = f"{llm_score.total}(llm-{prompt_type})"
        else:
            value_str = "-(rule)"

        logger.info(
            "  -> %s  total=%d  value=%s  context_level=%d  cost_tags=%s  llm_cost=$%.4f%s",
            reject_type,
            result.total_score,
            value_str,
            context_level,
            feasibility.high_cost_tags or "none",
            llm_score.cost_usd if llm_score else 0.0,
            f"  reason={result.reject_reason}" if result.reject_reason else "",
        )

        save_json(output_file, results)
        save_individual_score(rec_dir, results[rec_id])

    logger.info("=" * 50)
    logger.info(
        "accept_golden=%d  accept_silver=%d  accept_bronze=%d  reject_iron=%d  reject_zero=%d (fatal=%d trivial=%d llm=%d)",
        counts["accept_golden"], counts["accept_silver"], counts["accept_bronze"], counts["reject_iron"], counts["reject_zero"],
        counts["fatal_reject"], counts["trivial_reject"], counts["llm_reject"]
    )
    logger.info(
        "resumed=%d  no_cmd_skip=%d  load_error=%d  llm_error=%d",
        plan["resumed"], plan["no_commands"], plan["load_error"], counts["error"],
    )
    logger.info(
        "LLM usage: prompt_tokens=%d  completion_tokens=%d  total_cost=$%.4f",
        total_prompt_tokens, total_completion_tokens, total_cost_usd,
    )
    logger.info("Results saved -> %s", output_file)


if __name__ == "__main__":
    main()
