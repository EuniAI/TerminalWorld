# STEP 5 — Runtime Fix + Loop

**Goal:** Fix issues identified in the runtime diagnosis, then loop back to STEP 3 for re-testing.

## Actions

### 5.1 Read the diagnosis

Read `<TASK_DIR>/diagnosis_runtime.json` and its `recommended_fixes`.

### 5.2 Apply fixes

Fix artifacts based on the diagnosis. Common patterns:

| Symptom | Fix Target |
|---------|-----------|
| Oracle build failed | Fix Dockerfile (missing dependency, bad base image) |
| solve.sh non-zero exit | Fix solve.sh (command error) or Dockerfile (missing tool) |
| Oracle tests partially fail | Fix solve.sh (add missing state writes) or fix test_state.py (path alignment) |
| Nop reward = 1 (tests pass vacuously) | Strengthen test_state.py — add file existence checks + content assertions |
| Partial reward = 1 (tests too weak) | Strengthen test_state.py — add content/value assertions that only the full solution can satisfy |
| Instruction not meeting standard | Rewrite instruction.md to Level 1-2 per the reference |
| Test checks specific constraint (format, sort order, naming, timestamp format) not in instruction | Classify: **core** constraint (agent needs to know) → add the specification to `instruction.md`; **non-core** implementation detail (agent can choose freely) → relax or remove the test |
| `input_file_missing` in failure_analysis | Check `solve.sh` and `source/info.json` for a `curl`/`wget`/`git clone` URL; if found, add `RUN curl/wget` to Dockerfile. If no URL: synthesize from solve.sh + info.json (read `source/recording.txt` only if still insufficient), place file in `environment/`, add `# synthesized: <file> not available from source; inferred from solve.sh` comment + `COPY <file> /app/<file>` |
| `host_resource_unavailable` — Case 1 (task is valid, just needs specific host) | Add a brief requirement note to `instruction.md` (e.g., *"Note: this task requires a host with an NVIDIA GPU."*). Do not modify solve.sh or Dockerfile. |
| `host_resource_unavailable` — Case 2 (workflow fundamentally tied to unavailable hardware) | Attempt to replace the host-dependent commands in `solve.sh` with equivalent alternatives that do not require the hardware. If no equivalent exists, proceed to STEP 6 with `--status partial` and record the host dependency as the reason. |
| `git://` protocol timeout (oracle build fails with connection refused on `git clone git://github.com/...`) | Replace `git://github.com/` with `https://github.com/` in the Dockerfile. |
| Cloud credentials failure (solve.sh exits with AWS/GCP/Azure auth error) | Resolution order: (1) simulate cloud APIs locally (LocalStack for AWS, MinIO for S3); (2) simulate compute nodes with Docker containers (e.g., `kind`/`k3s` for Kubernetes, SSH-accessible Docker container for remote-host tasks); (3) rewrite solve.sh to use an offline-equivalent workflow; (4) if none is feasible, proceed to STEP 6 with `--status partial`. Never embed real credentials. |
| Outdated or removed package (pip/npm install fails, or import fails with API error) | Check `source/info.json` recording timestamp and look up the package version available then. Resolution order: (1) pin to the recording-era version in Dockerfile; (2) adapt solve.sh to the current API if the task objective is preserved; (3) if the package is gone with no equivalent, proceed to STEP 6 with `--status partial`. |

### 5.3 Fix principles

- **Fix one category at a time** — don't try to fix everything at once if the issues span multiple artifacts
- **Maintain three-way alignment** — `test_state.py` is ground truth: if you add or tighten a test that checks a specific output path, update `instruction.md` to explicitly tell the agent to write there. An agent that solves the task but writes to the wrong path will fail the test.
- **Prefer minimal changes** — don't rewrite the entire solve.sh when adding one redirect suffices
- **Preserve what's already passing** — before editing test_state.py or solve.sh, note which tests are currently passing (from the oracle trial output). Do not remove or alter those assertions/steps — only add or fix what's broken.
- **Read the actual error output** — the oracle.txt and test-stdout.txt files contain the real error messages; use them to pinpoint the exact issue
- **Last resort: read the source recording** — only if oracle keeps failing and solve.sh is clearly missing steps that can't be inferred from the error output, read `<TASK_DIR>/source/recording.txt` to see the original terminal session. Avoid reading it speculatively; the file can be large.
- **Regenerate partial solutions if solve.sh changed** — if you modified solve.sh, regenerate `partial_solve_*.sh` to match the new solution structure

### 5.4 Iteration limit

Track how many times you've been through the STEP 3→4→5 loop. **Maximum 5 iterations.** If the task hasn't converged after 5 iterations:

- Write the current state of `diagnosis_runtime.json`
- Proceed to STEP 6 with `--status partial` instead of `--status refined`
- The task may need manual intervention

## Output

Modified artifact files in `<TASK_DIR>/`.

## Next Step

Return to [STEP 3](STEP3.md) to re-run trials.
