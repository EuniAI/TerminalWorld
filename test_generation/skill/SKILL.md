---
name: terminal-task-refiner
description: >
  Iteratively refines terminal benchmark task artifacts (environment, solution,
  tests, instruction) so they pass oracle, nop, and partial Harbor trials and
  meet quality standards. Input: a standard TerminalBench-format task draft.
---

# Terminal Task Refiner

Takes a standard TerminalBench-format task draft and iteratively refines its artifacts until it passes oracle, nop, and partial Harbor trials and the instruction meets quality standards.

## When to Use This Skill

Invoke this skill when:
- Refining an existing terminal task's artifacts to meet TerminalBench standards

## Do NOT Use This Skill For

- Creating entirely new tasks from scratch
- Building new Docker environments from scratch

## Prerequisites

- Harbor framework installed (`uv tool install harbor` or `pip install harbor`)
- Docker installed and running
- Task directory containing the 5 standard artifacts:
  - `environment/Dockerfile` or `environment/docker-compose.yaml`
  - `solution/solve.sh`
  - `tests/test.sh`, `tests/test_state.py`
  - `instruction.md`
  - `task.toml`

## Convergence Criteria

The task is **refined** when ALL of the following are true:

1. **Oracle trial reward = 1** — solve.sh runs, produces correct artifacts, all tests pass
2. **Nop trial reward = 0** — tests correctly reject the empty state (nothing ran)
3. **All partial trial rewards = 0** — tests correctly reject every partial solution (LLM-generated incomplete solve scripts)
4. **Instruction quality** — meets Level 1-2 on the prescription spectrum (see `references/instruction_standard.md`)

## Workflow

Execute these steps in order. The workflow includes a loop (STEP 3->4->5->3) that iterates until convergence or the iteration limit.

1. **[STEP 1: Static Diagnosis](workflow/STEP1.md)** — Audit artifact structure, read all files and reference standards, produce `diagnosis_static.json`
2. **[STEP 2: Initial Fix](workflow/STEP2.md)** — Fix structural/obvious issues from the static diagnosis
3. **[STEP 3: Run Harbor Trials](workflow/STEP3.md)** — Run oracle, nop, and partial trials
4. **[STEP 4: Runtime Diagnosis](workflow/STEP4.md)** — Analyze trial outputs, check convergence, produce `diagnosis_runtime.json`
5. **[STEP 5: Runtime Fix](workflow/STEP5.md)** — Fix issues from runtime diagnosis, then loop back to STEP 3
6. **[STEP 6: Finalize](workflow/STEP6.md)** — Write `refinement_metadata.json` and clean up

**Maximum iterations:** 5 loops through STEP 3->4->5. If not converged after 5 iterations, finalize with `--status partial`.

**Infeasibility:** If at any point you determine the task is fundamentally broken and cannot be fixed (e.g. requires unavailable external resources, unverifiable outcome, out-of-scope domain), finalize immediately with `--status infeasible` (no `--oracle-reward`/`--nop-reward`/`--iterations` needed). Infeasible tasks are permanently skipped in all future runs.

## Script Reference

All scripts are deterministic — they collect facts or execute actions, but make no judgments.

Run `python3 .claude/skills/terminal-task-refiner/scripts/<script>.py --help` for full parameter details.

- **[scripts/audit_artifacts.py](scripts/audit_artifacts.py)** — Audit file existence, sizes, test function count, harbor-canary presence
- **[scripts/run_trial.py](scripts/run_trial.py)** — Run a Harbor trial (oracle, nop, or partial), write summary JSON with file paths
- **[scripts/finalize.py](scripts/finalize.py)** — Write `refinement_metadata.json` and archive intermediate files into `refinement/`

## Reference Standards

These documents define the quality criteria for each artifact. Read them during STEP 1 and STEP 4 to inform your diagnosis.

- **[references/instruction_standard.md](references/instruction_standard.md)** — Instruction quality: prescription level, concision, path usage, test alignment
- **[references/solution_tests_standard.md](references/solution_tests_standard.md)** — Solution and tests coordination: state-based testing, three-way alignment, nop-safety, harbor-canary
- **[references/dockerfile_standard.md](references/dockerfile_standard.md)** — Dockerfile quality: no solution leakage, clean apt, WORKDIR, dependency scope

## Examples

Three reference-quality tasks are provided in `examples/`. When you are unsure what good artifacts look like, read these before diagnosing or fixing:

- **[examples/db-wal-recovery](examples/db-wal-recovery/)** — Gold standard: state-based tests, clean three-way alignment, goal-level instruction
- **[examples/feal-differential-cryptanalysis](examples/feal-differential-cryptanalysis/)** — Best instruction quality: Level 2, concise interface spec, functional test
- **[examples/crack-7z-hash](examples/crack-7z-hash/)** — Most concise: one-sentence instruction, minimal state test

See [examples/README.md](examples/README.md) for what each example demonstrates.

## Diagnostic Files

These intermediate files are written during the workflow:

| File | Written by | Purpose |
|------|-----------|---------|
| `audit.json` | STEP 1 (script) | Mechanical audit of file structure |
| `diagnosis_static.json` | STEP 1 (agent) | Pre-trial diagnosis of artifact issues |
| `oracle_trial.json` | STEP 3 (script) | Oracle trial summary with file paths |
| `nop_trial.json` | STEP 3 (script) | Nop trial summary with file paths |
| `partial_trial_*.json` | STEP 3 (script) | Partial trial summaries (1-3) |
| `solution/partial_solve_*.sh` | STEP 1 (agent) | LLM-generated partial solve scripts (1-3) |
| `diagnosis_runtime.json` | STEP 4 (agent) | Post-trial diagnosis with convergence check |
| `refinement_metadata.json` | STEP 6 (script) | Final refinement status and metrics |

All intermediate files (except `refinement_metadata.json`) are archived into `<TASK_DIR>/refinement/` during STEP 6 finalization. The `refinement/trials/` subdirectory contains the full Harbor trial output (agent logs, test stdout, reward files).
