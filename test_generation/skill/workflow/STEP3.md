# STEP 3 — Run Harbor Trials

**Goal:** Run both oracle and nop trials to test the current state of the task artifacts.

## Actions

### 3.1 Oracle trial

Run the oracle trial (executes `solve.sh`, then runs tests):

```bash
python3 .claude/skills/terminal-task-refiner/scripts/run_trial.py \
  --task-dir <TASK_DIR> --mode oracle --output <TASK_DIR>/oracle_trial.json
```

### 3.2 Nop trial

Run the nop trial (does nothing, then runs tests — tests should fail):

```bash
python3 .claude/skills/terminal-task-refiner/scripts/run_trial.py \
  --task-dir <TASK_DIR> --mode nop --output <TASK_DIR>/nop_trial.json
```

### 3.3 Partial trials

Run partial trials for each `partial_solve_*.sh` in `<TASK_DIR>/solution/`. Each should produce reward=0 (tests reject incomplete work):

```bash
python3 .claude/skills/terminal-task-refiner/scripts/run_trial.py \
  --task-dir <TASK_DIR> --mode partial \
  --partial-script <TASK_DIR>/solution/partial_solve_1.sh \
  --output <TASK_DIR>/partial_trial_1.json
```

Repeat for `partial_solve_2.sh` → `partial_trial_2.json`, etc.

## Notes

- Each trial builds a fresh Docker image and removes it on completion (`delete=True`). There is no image reuse between trials — every trial starts clean.
- All trials may take several minutes each. Wait for each to complete before proceeding.

## Output

- `<TASK_DIR>/oracle_trial.json` — oracle trial summary
- `<TASK_DIR>/nop_trial.json` — nop trial summary
- `<TASK_DIR>/partial_trial_1.json` (and 2, 3) — partial trial summaries

## Next Step

Proceed to [STEP 4](STEP4.md).
