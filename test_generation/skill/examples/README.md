# Example Tasks

Three reference-quality tasks from TerminalBench. When you are unsure what "good" looks like for any artifact, read these examples.

---

## `db-wal-recovery` — Gold Standard (State-Based Testing)

**What it demonstrates:**
- State-based tests that read a file and verify its exact content
- A concrete output path (`/app/recovered.json`) referenced consistently across all three artifacts
- Instruction that specifies the goal and output format without prescribing method
- Dockerfile that provides only the necessary OS tools (sqlite3, xxd) — not the solution

**Why it's a good reference:** `test_state.py` has 6 layered assertions (file exists → valid JSON → correct structure → sorted → 11 records → WAL decrypted). Each layer would fail under nop. Classic three-way alignment.

---

## `feal-differential-cryptanalysis` — Best Instruction Quality

**What it demonstrates:**
- Level 2 instruction: states the goal, names the interface (`attack(encrypt_fn)`), gives one constraint (< 30 seconds), nothing more
- Functional test that calls the agent's code directly rather than reading a file — a valid alternative to state-based testing
- Minimal Dockerfile with a single Python dep (feal.py compiled module)

**Why it's a good reference:** The instruction is extremely tight. It tells the agent *what* to produce and *how it will be called*, not *how to implement it*. Use this as a model for high-difficulty tasks with a clear interface.

---

## `crack-7z-hash` — Most Concise

**What it demonstrates:**
- One-sentence instruction — the entire task is defined in a single line
- Pure state-based test (file exists + content matches a single word)
- Complex environment setup (building John the Ripper from source) lives entirely in the Dockerfile because that setup is a prerequisite, not the task itself

**Why it's a good reference:** Illustrates the lower bound of instruction verbosity. If the task can be stated in one sentence with a concrete output file, do so.
