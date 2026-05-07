# Instruction Quality Standard

This document defines the quality criteria for `instruction.md` in terminal benchmark tasks.

## Core Principle: Goal-specification, Not Recipe

The instruction must describe **what** the agent needs to achieve, not **how** to achieve it. Two independent readers should be able to write verification algorithms that agree on any given solution.

## The Prescription Spectrum

| Level | Description | Acceptable? |
|-------|-------------|-------------|
| Level 1 | Pure goal: "Compute Brun's constant to 12 decimal places." | Best |
| Level 2 | Goal + context: mentions a tool or method because it IS the task | Good |
| Level 3 | Goal + method hint: specifies approach but not exact commands | Borderline |
| Level 4 | Recipe: step-by-step commands, flags, pipelines | Never acceptable |

**Target: Level 1-2.** Level 3 only when the method constraint is part of what makes the task interesting.

## What an Instruction MUST Have

1. **The goal** — what the final state or output must be
2. **Verification-relevant constraints** — output file path (absolute, e.g. `/app/result.txt`), format, tolerance
3. **Genuinely necessary context** — e.g. a broken file to fix, a specific library because the task IS about that library

## What an Instruction MUST NOT Have

1. **Step-by-step commands** — no shell pipelines, no `cmake . && make -j`
2. **Numerical constants the agent should derive** — no correction factors, no intermediate values
3. **Intermediate milestones** — only the final verifiable outcome matters
4. **Repository URLs** — unless the specific repo IS the constraint
5. **Lists of available tools** — the agent should discover what's available

> **When rewriting a Level 3-4 instruction down to Level 1-2:** strip commands and method hints, but never remove output file paths, output format requirements, or numeric tolerances — those are verification constraints that the agent needs to know to produce a correct solution. Removing them produces a minimal-looking instruction that an agent cannot actually satisfy.

## Concision

- **2-3 paragraphs** is ideal; longer instructions usually indicate over-specification
- **Human-written tone** — LLM-generated prose (verbose, structured with many headings/bullets) is a red flag
- **Absolute paths only** — always `/app/result.txt`, never `result.txt`

## Test-Instruction Alignment

- Every assertion in `test_state.py` must trace back to a requirement in `instruction.md`
- Every requirement in `instruction.md` should have test coverage
- If tests check a specific output file, that file path must appear in `instruction.md`
- Tests must NOT introduce requirements not mentioned in the instruction

## Evaluation Checklist

When evaluating an instruction, answer these questions:

1. **Is it goal-level?** Could you remove all commands/pipelines and still understand what to achieve?
2. **Is it concise?** Under 3 paragraphs? No LLM-style verbose structure?
3. **Does it use absolute paths?** All output paths like `/app/...`?
4. **Does it avoid over-specification?** No exact commands, no formulas the agent should derive?
5. **Does it align with tests?** Every tested output is mentioned; no untested requirements?

## Pass / Fail Examples

**PASS — Goal-level:**
> "The file `/app/feal.py` implements a FEAL-like encryption function. Implement a chosen plaintext attack that recovers the value of `key[5]`. Your attack should be implemented in `/app/attack.py` and return the uint32 value of `key[5]`. It should run in less than 30 seconds."

- Specifies goal (recover key[5]) and interface (attack.py)
- Does not explain differential cryptanalysis
- Uses absolute paths

**FAIL — Recipe:**
> "Clone primesieve from https://github.com/..., run `cmake . && make -j && sudo make install`. Run `./primesieve 100000000 -p2 | sed '...' | awk '...'` to get partial sum. Apply correction term `4 * 0.6601618... / log(N)` to get extrapolated value."

- Provides every command verbatim
- Gives the formula and constants
- An agent copying the instruction verbatim succeeds — no real difficulty

## Task-Type Notes

### Numerical computation tasks

Remove specific constants and formulas from the instruction — the agent should derive them. Keep only the final verifiable target (value, tolerance, output path). Redirect output to a file so the test can check it without re-running the computation.

**Bad:** "Apply correction term `4 * 0.6601618... / log(N)` to get extrapolated value 1.902167937961."
**Good:** "Compute an extrapolated estimate using the standard method. Write the result to `/app/result.txt`."

### Build-from-source tasks

Building a tool from source (cmake, make, etc.) is a precondition, not the difficulty. The difficulty must come from what the agent does with the built tool, or from non-trivial build obstacles (dependency conflicts, non-standard architecture patches). Do not treat a successful build as the task goal.

### Debug / recovery tasks

Tasks where the environment contains a broken artifact (corrupted file, crashing program, misconfigured service) are naturally goal-level — they rarely need instruction reduction. These are among the best benchmark task types.

## Examples

See `.claude/skills/terminal-task-refiner/examples/` for reference-quality instructions:
- `feal-differential-cryptanalysis/instruction.md` — Level 2: goal + interface spec, nothing more
- `crack-7z-hash/instruction.md` — most concise: one sentence
- `db-wal-recovery/instruction.md` — Level 2 with output format specified
