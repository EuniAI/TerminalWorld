# STEP 4 — Runtime Diagnosis

**Goal:** Analyze trial results, read trial output files, and produce a runtime diagnosis. Determine whether the task has converged.

## Actions

### 4.1 Read trial summaries

Read all trial results:
- `<TASK_DIR>/oracle_trial.json`
- `<TASK_DIR>/nop_trial.json`
- `<TASK_DIR>/partial_trial_1.json` (and 2, 3 if they exist)

### 4.2 Read trial output files

For each trial that has a `trial_dir`, read the relevant output files:

**Oracle trial:**
- `<trial_dir>/agent/oracle.txt` — solve.sh stdout/stderr
- `<trial_dir>/verifier/test-stdout.txt` — pytest output
- `<trial_dir>/verifier/ctrf.json` — per-test pass/fail details
- `<trial_dir>/verifier/reward.txt` — 0 or 1
- `<trial_dir>/result.json` — full trial result

**Nop trial:**
- `<trial_dir>/verifier/test-stdout.txt` — pytest output (should show failures)
- `<trial_dir>/verifier/ctrf.json` — per-test pass/fail details
- `<trial_dir>/verifier/reward.txt` — should be 0
- `<trial_dir>/result.json` — full trial result

**Partial trials** (same structure as oracle — they run as oracle agent with a swapped solve.sh):
- `<trial_dir>/agent/oracle.txt` — partial script stdout/stderr
- `<trial_dir>/verifier/test-stdout.txt` — pytest output (should show failures)
- `<trial_dir>/verifier/reward.txt` — should be 0

If a file doesn't exist, note it as a problem (likely the trial crashed before reaching that stage).

### 4.3 Re-read reference standards

Read the three reference documents again for evaluation context:
- `.claude/skills/terminal-task-refiner/references/instruction_standard.md`
- `.claude/skills/terminal-task-refiner/references/solution_tests_standard.md`
- `.claude/skills/terminal-task-refiner/references/dockerfile_standard.md`

### 4.4 Diagnose and check convergence

Write `<TASK_DIR>/diagnosis_runtime.json`:

```json
{
  "oracle": {
    "reward": 1,
    "build_ok": true,
    "solve_exit_code": 0,
    "tests_passed": ["test_a", "test_b"],
    "tests_failed": ["test_c"],
    "failure_analysis": "test_c fails because solve.sh does not write /app/x.json — OR — input_file_missing: solve.sh reads /app/data.csv but no COPY or RUN in Dockerfile provides it — OR — host_resource_unavailable: solve.sh requires NVIDIA GPU (nvidia-smi not found in container)"
  },
  "nop": {
    "reward": 0,
    "tests_passed": [],
    "tests_failed": ["test_a", "test_b", "test_c"],
    "analysis": "All tests correctly fail when nothing runs"
  },
  "partial": [
    {
      "script": "partial_solve_1.sh",
      "reward": 0,
      "tests_passed": ["test_a"],
      "tests_failed": ["test_b", "test_c"],
      "analysis": "File exists but content is wrong — test_b and test_c correctly reject"
    },
    {
      "script": "partial_solve_2.sh",
      "reward": 1,
      "tests_passed": ["test_a", "test_b", "test_c"],
      "tests_failed": [],
      "analysis": "PROBLEM: partial_solve_2 passes all tests — tests are too weak"
    }
  ],
  "convergent": false,
  "instruction_quality": "Level 2, meets standard",
  "recommended_fixes": [
    "Strengthen test_b to check file content, not just existence",
    "..."
  ],
  "robustness_issues": [
    "test_c checks for 'Phase 1:' label which is solution-invented — not in instruction",
    "..."
  ]
}
```

### 4.5 Convergence check

The task has **converged** when ALL of the following are true:

1. **oracle.reward == 1** — solve.sh runs successfully and all tests pass
2. **nop.reward == 0** — tests correctly fail when nothing runs
3. **All partial rewards == 0** — tests correctly reject every partial solution
4. **instruction quality meets standard** — Level 1-2 per the instruction reference

**If converged:** Proceed to **STEP 6** (finalize).

**If NOT converged:** Proceed to **STEP 5** (fix and loop).

**Special case — `host_resource_unavailable`:** If failure is caused by a host resource dependency (GPU, privileged kernel feature, hardware driver), this is not a code bug — it cannot be fixed by editing artifacts. Classify before proceeding to STEP 5:
- **Case 1:** The task logic is sound; it just requires specific host conditions to run (e.g., a GPU machine). The task is otherwise refineable.
- **Case 2:** The entire workflow is fundamentally tied to the unavailable resource — no equivalent approach exists that avoids it.

## Output

- `<TASK_DIR>/diagnosis_runtime.json`

## Next Step

- Converged → [STEP 6](STEP6.md)
- Not converged → [STEP 5](STEP5.md)
