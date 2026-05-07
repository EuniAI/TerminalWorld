# task.toml Standard

This document defines how to evaluate and adjust `task.toml` fields during task refinement.
The same rules apply both to the automated `refine_task_toml.py` script and to the
`terminal-task-refiner` skill workflow (STEP 1 static diagnosis).

## Guiding Principle

**Err on the side of larger values, not smaller.**
A timeout that is slightly generous wastes a few seconds. A timeout that is too tight causes
the agent or verifier to fail on a task it could have solved, producing a false negative in
benchmark results. The only constraint is that values must not be absurdly inflated beyond
what the task actually requires (which would waste cluster resources).

---

## Fields to Adjust

### `[agent] timeout_sec`

Time budget for the AI agent to solve the task.

**Classification guide (read `solution/solve.sh` and `instruction.md`):**

| Scenario | Recommended timeout |
|----------|-------------------|
| Pure file editing, scripting, simple CLI operations | 900s |
| Package install + config + run (moderate) | 1800s |
| Large compilation (LLVM, GCC, complex C++ projects) | 3600s |
| Model training, heavy data processing, large downloads | 3600–7200s |
| Exceptional tasks (Windows VM, full deep learning training) | 7200s+ |

**Signal words in solve.sh / instruction:**
- `make -j`, `cmake`, `cargo build`, `go build` on large codebase → ≥1800s
- `pip install torch`, `apt-get install` of many packages → ≥1800s
- `wget`/`curl` of large files (model weights, datasets >1GB) → ≥3600s
- ML training loops, numerical optimization, sampling → ≥3600s
- Simple `git`, `grep`, `awk`, config edits → 900s is fine

**Never go below 600s.** The agent needs time to think and iterate.

---

### `[verifier] timeout_sec`

Time budget for running `tests/test_state.py` via pytest.

**Default rule: set equal to `agent.timeout_sec`.**

Most verifier tests only read files written by solve.sh and perform quick assertions.
Setting verifier = agent is safe and consistent with the terminal-bench-2 reference benchmark.

**Exception — you MAY keep verifier lower than agent only when ALL of these hold:**
1. The test file contains no `subprocess`, `os.system`, `check_output`, or `Popen` calls
2. The test only reads files / checks hashes / validates structure (pure state inspection)
3. Even then, keep verifier at a minimum of **600s** — never below that, because pytest
   startup and import overhead can be significant inside slow Docker environments

**If the test runs any subprocess:** verifier timeout must be ≥ agent timeout.
**The current template default of 120s is almost always wrong** — treat it as a bug to fix.

---

### `[environment] memory_mb`

Container memory limit for both agent and verifier.

**Defaults by workload:**

| Workload | memory_mb |
|----------|-----------|
| Default (scripting, git, text processing) | 4096 |
| Package-heavy environments (many apt/pip installs) | 4096 |
| Compilation of large C/C++/Rust projects | 4096–8192 |
| ML inference (loading PyTorch/TF model) | 8192–16384 |
| ML training or large-batch inference | 16384+ |
| Explicit memory-intensive task (redis, databases with large datasets) | 8192+ |

**Signal words:** `torch`, `tensorflow`, `transformers`, `huggingface`, `model.pt`,
`load_model`, `faiss`, `embeddings`, `train`, `fit` → ≥8192MB

The original template default is 2048MB, which is too low for most real tasks.
**Upgrade 2048 → 4096 as the safe minimum**, unless the task is provably trivial.

---

### `[environment] cpus`

CPU allocation.

| Scenario | cpus |
|----------|------|
| Default | 1 |
| `make -j4`, `make -j8`, parallel compilation flags | 2–4 |
| Explicit multi-process workload (mcmc, parallel sampling) | 4 |
| ML training with explicit worker threads | 2–4 |

**Do not exceed 4** unless the task explicitly requires it. Most terminal tasks are single-threaded.

---

### `[environment] storage_mb`

Disk space limit.

| Scenario | storage_mb |
|----------|------------|
| Default | 10240 (10GB) |
| Large dataset download (>5GB) | 20480–51200 |
| Model weights (LLM, vision models) | 20480–102400 |
| Video processing, large archives | 20480+ |

**Default 10240 is acceptable for most tasks.** Only increase when solve.sh or the
Dockerfile downloads/generates files that clearly exceed 10GB.

---

### `[environment] build_timeout_sec`

Time to build the Docker image.

| Scenario | build_timeout_sec |
|----------|-----------------|
| Default (pre-built image, minimal apt installs) | 600 |
| Many apt/pip packages | 600–1200 |
| Large compilation inside Dockerfile (e.g. building from source) | 1200–3600 |

---

### `[environment] allow_internet`

Whether the container can reach the internet during agent execution.

**Set `allow_internet = true` only if** solve.sh or the task requires network access at
runtime (not just during Docker build):
- `apt-get install` during solve.sh
- `pip install` during solve.sh
- `wget`/`curl` to external URLs during solve.sh
- API calls to external services

**Do NOT set true** if all dependencies are installed in the Dockerfile and solve.sh
only uses local files.

---

## Fields to NEVER Change

- `[metadata]` — all fields (task_source, author, category, tags, difficulty, etc.)
- `version`
- `[environment] docker_image` — set by environment builder, not task toml refiner
- `[environment] gpus` / `gpu_types` — only if explicitly required by task; never add unless certain
- `[environment] mcp_servers` / `skills_dir` — advanced features, leave as-is

---

## Output Format

When patching task.toml, output only fields that actually need changing.
Preserve all existing values that are already correct or conservatively large.
Do not downgrade a value that is already generous — only upgrade values that are dangerously low.

Reasoning should be explicit: for each changed field, cite the specific evidence from
`solve.sh`, `instruction.md`, or `Dockerfile` that justifies the change.
