# STEP 1 — Static Diagnosis

**Goal:** Read all task artifacts, audit their structure, and produce a diagnosis. Do NOT modify any files in this step.

## Actions

### 1.1 Run the audit script

```bash
python3 .claude/skills/terminal-task-refiner/scripts/audit_artifacts.py \
  --task-dir <TASK_DIR> \
  --output <TASK_DIR>/audit.json
```

Read `audit.json` to understand file existence, sizes, test function count, and harbor-canary status.

### 1.2 Read all artifact files

Read every artifact in full:
- `<TASK_DIR>/instruction.md`
- `<TASK_DIR>/solution/solve.sh`
- `<TASK_DIR>/tests/test_state.py`
- `<TASK_DIR>/tests/test.sh`
- `<TASK_DIR>/environment/Dockerfile`
- `<TASK_DIR>/environment/docker-compose.yaml` (if exists)
- `<TASK_DIR>/task.toml`

### 1.3 Read the reference standards and examples

Read all reference documents:
- `.claude/skills/terminal-task-refiner/references/instruction_standard.md`
- `.claude/skills/terminal-task-refiner/references/solution_tests_standard.md`
- `.claude/skills/terminal-task-refiner/references/dockerfile_standard.md`
- `.claude/skills/terminal-task-refiner/references/task_toml_standard.md`
- `.claude/skills/terminal-task-refiner/references/task_purposes.json`
- `.claude/skills/terminal-task-refiner/references/operation_types.json`

If you are unsure what "good" looks like for any artifact, read the example tasks in `.claude/skills/terminal-task-refiner/examples/`:
- `examples/db-wal-recovery/` — gold standard (state-based tests, three-way alignment)
- `examples/feal-differential-cryptanalysis/` — best instruction quality (Level 2, concise)
- `examples/crack-7z-hash/` — most concise (one-sentence instruction, minimal test)

### 1.4 Diagnose

Identify issues in each artifact. Consider:

- **instruction.md**: Is it Level 1-2? Is it concise? Does it use absolute paths? Does it avoid over-specification? Does it explicitly state all format/ordering/naming constraints that tests will need to verify?
- **solve.sh**: Does it produce persistent artifacts? Does it actually compute (not hardcode)? Do output paths match instruction.md? Only if you are confident that solve.sh is missing key steps (e.g. clearly truncated, skips an obvious phase), read `<TASK_DIR>/source/recording.txt` — the original terminal session solve.sh was synthesized from. Avoid reading it speculatively; the file can be large.
- **test_state.py**: Is it state-based (no subprocess)? Does it have harbor-canary? Do paths match solve.sh? Would it fail under nop? Does it avoid solution-invented labels/names not in the instruction? Does it handle system-default variability (e.g. DB user host) and kernel-auto-created resources?
- **test.sh**: Does it correctly invoke pytest on `test_state.py` using the `uvx` pattern (not `pip install pytest`)? Are test-only dependencies installed here, not in Dockerfile?
- **harbor-canary coverage**: Every text file in the task must contain the harbor-canary GUID comment — `test_state.py`, `test.sh`, `solve.sh`, `Dockerfile`, `instruction.md`, and `task.toml`. Flag any missing as `error`.
- **task.toml resource fields**: Read `task_toml_standard.md` and check each adjustable field against the current values. Flag as `warning` any field that is dangerously low (may cause false negatives). Flag as `ok` if already generous. Specifically check: `verifier.timeout_sec` vs `agent.timeout_sec` (should be equal unless verifier is trivially fast); `environment.memory_mb` (2048 is the unsafe template default — upgrade if the task is not trivially light); `environment.cpus` (increase if solve.sh uses parallel compilation or multi-process workloads); `environment.allow_internet` (set true only if solve.sh downloads at runtime, not just during Docker build); `environment.storage_mb` (increase if solve.sh downloads large datasets or model weights). Never flag metadata fields.
- **Dockerfile**: No COPY solution/tests? No pre-run of solve? Clean apt usage? WORKDIR /app? If `environment/` has setup/seed scripts, does the Dockerfile call them in a `RUN` step? Does the Dockerfile provide all files that solve.sh reads but does not create? (If not — flag as `error` in `priority_fixes`.)
- **Three-way alignment**: Are file paths consistent across instruction → solve.sh → test_state.py? When a test checks a specific constraint not stated in `instruction.md`, judge: **core** (agent needs to be told) → add to instruction; **non-core** (agent's free choice) → relax or remove the test.
- **Demo/tutorial task**: Does instruction say "demonstrate", "run a session showing", "explore" — without specifying exact function interfaces or output values? Does `test_state.py` check strings that only appear in the reference solution's specific run? If yes, **skip 1.5 and go directly to 1.6** — the required fix is a full restructure of all three artifacts (see solution_tests_standard.md "Demo and Tutorial Tasks").

### 1.5 Generate partial solutions

Read `solve.sh` carefully and generate 1-3 partial solve scripts that represent **semantically meaningful incomplete solutions**. Save them as:
- `<TASK_DIR>/solution/partial_solve_1.sh`
- `<TASK_DIR>/solution/partial_solve_2.sh`
- `<TASK_DIR>/solution/partial_solve_3.sh` (optional)

Each partial script should represent a different type of incomplete work. Good patterns:

| Pattern | Example |
|---------|---------|
| Creates output file but with wrong/empty content | `touch /app/result.txt` |
| Installs tools but doesn't run the computation | Only the setup commands from solve.sh |
| Runs computation but doesn't save results | Computation runs but no redirect to file |
| Completes first half of a multi-step workflow | Only the first logical block of solve.sh |

**Guidelines:**
- Each partial script must be valid bash (no syntax errors) — it should run without crashing
- Each should produce a **different subset** of the expected state
- They should test whether `test_state.py` can distinguish partial from complete work
- Do NOT make trivially empty scripts (that's what the nop trial tests)

### 1.6 Write diagnosis

Assign multi-labels from the taxonomy files read in 1.3, then write `<TASK_DIR>/diagnosis_static.json`:

```json
{
  "task_purpose": ["system-administration", "security"],
  "operation_type": ["system-configuration", "network"],
  "instruction": {
    "issues": ["Level 3 prescription — contains method hints that should be removed", "..."],
    "severity": "warning"
  },
  "solution": {
    "issues": ["Output path /app/result.txt not written", "..."],
    "severity": "error"
  },
  "tests": {
    "issues": ["Missing harbor-canary GUID", "..."],
    "severity": "error"
  },
  "dockerfile": {
    "issues": [],
    "severity": "ok"
  },
  "task_toml": {
    "issues": ["verifier.timeout_sec=120 is too low — should equal agent.timeout_sec=900", "memory_mb=2048 is the unsafe template default; Dockerfile installs heavy packages"],
    "severity": "warning"
  },
  "three_way_alignment": {
    "status": "broken",
    "broken_links": ["instruction says /app/output.json but solve.sh writes to /app/result.txt"]
  },
  "priority_fixes": [
    "Add harbor-canary GUID to all files missing it: test_state.py, test.sh, solve.sh, Dockerfile, instruction.md, task.toml",
    "Align output path across all three artifacts to /app/result.txt",
    "task.toml: set verifier.timeout_sec=900.0 (equal to agent); upgrade memory_mb from 2048 to 4096",
    "..."
  ]
}
```

Severity levels: `"ok"`, `"warning"`, `"error"`

For a demo/tutorial task, `priority_fixes` should contain a single restructure entry:
```json
{
  "task_purpose": ["demo-showcase"],
  "operation_type": ["program-execution"],
  "instruction": { "issues": ["Asks to 'demonstrate X'; no function interfaces specified"], "severity": "error" },
  "tests": { "issues": ["Checks implementation-specific strings not derivable from instruction"], "severity": "error" },
  "priority_fixes": [
    "Restructure: define function interfaces in instruction.md, rewrite solve.sh to implement them, rewrite test_state.py to call them with fixed inputs"
  ]
}
```

**IMPORTANT:** `priority_fixes` should list only structural/obvious issues that STEP 2 should fix before running trials. Runtime-discoverable issues (build failures, test failures) should be left for the trial loop.

## Output

- `<TASK_DIR>/audit.json` — mechanical audit from script
- `<TASK_DIR>/diagnosis_static.json` — your diagnosis
- `<TASK_DIR>/solution/partial_solve_1.sh` (and 2, 3) — partial solve scripts for testing

## Next Step

Proceed to [STEP 2](STEP2.md).
