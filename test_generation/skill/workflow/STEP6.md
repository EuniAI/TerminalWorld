# STEP 6 — Finalize

**Goal:** Write the final refinement metadata and clean up intermediate files.

## Actions

### 6.1 Run finalize script

```bash
python3 .claude/skills/terminal-task-refiner/scripts/finalize.py \
  --task-dir <TASK_DIR> \
  --status <refined|partial|failed|infeasible> \
  --oracle-reward <0|1> \
  --nop-reward <0|1> \
  --iterations <N>
```

Status values:
- `refined` — oracle=1, nop=0, instruction meets standard (fully converged)
- `partial` — some criteria met but not all (e.g. hit iteration limit)
- `failed` — unable to make meaningful progress (transient failure, will be retried)
- `infeasible` — task is fundamentally broken and cannot be fixed (permanently skipped in future runs); omit `--oracle-reward`, `--nop-reward`, `--iterations`

Use `infeasible` (not `failed`) when the task is structurally unsolvable: the recording captures a workflow that requires unavailable external resources (live cloud credentials, proprietary hardware, licensed software), the expected outcome is ambiguous or unverifiable, or the task domain is outside the scope of terminal benchmarks.

### 6.2 Verify archival

The finalize script automatically archives all intermediate files into `<TASK_DIR>/refinement/`, preserving the full refinement history for debugging and analysis. Verify the script output confirms archival.

### 6.3 Final artifact check

Verify the task directory structure is clean:
- `environment/` — Dockerfile (and docker-compose.yaml if needed)
- `solution/` — solve.sh only
- `tests/` — test.sh, test_state.py
- `instruction.md`
- `task.toml`
- `refinement_metadata.json` — created by finalize
- `refinement/` — archived intermediate files

## Output

- `<TASK_DIR>/refinement_metadata.json`

## Done

The task refinement is complete. Report the final status to the user.
