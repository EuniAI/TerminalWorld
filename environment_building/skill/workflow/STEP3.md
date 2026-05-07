# Step 3: Verify with Target Commands

## 🎯 High-Level Objective
The goal of Step 3 is to confirm that the built image actually works by running the target commands from `solve.sh` inside the container.

**Environment Maturity Ladder:** The verification script automatically computes a maturity level based on how many commands pass. From lowest to highest:
- **Buildability** (Level 0) — The environment recipe successfully compiles into a static image without resolving to errors (`docker build` exit code == 0).
- **Launchability** (Level 1) — The static image can be successfully launched into a living, responsive container instance (Container stays alive; shell responds).
- **Testability** (Level 2) — Essential toolchains and dependencies within the container are verifiable via targeted diagnostic probes (Diagnostic probes like `--version`, `import` pass).
- **Runnability** (Level 3) — The environment is capable of executing at least one user-specified business or target command (>= 1 Target Command passes).
- **Replayability** (Level 4) — The majority of the original user workflow can be seamlessly replayed within the synthesized environment (>= 60% Target Commands pass).
- **Reproducibility** (Level 5) — The complete sequence of user operations is flawlessly replicated, representing a functionally equivalent environment (100% Target Commands pass).

Your goal is to reach the highest level possible. If you have reached a meaningful level and the remaining failures are not genuinely fixable environment issues, it is acceptable to stop at `Runnability` or `Replayability` rather than looping indefinitely. The final maturity level is recorded automatically in `build_metadata.json`.

**CRITICAL BOUNDARY:** Your fixes must be strictly limited to modifying `<OUTPUT_DIR>/Dockerfile` or the generated Compose file. **NEVER** modify the user's source code, dependency manifest files, or any scripts in this repository to work around an error.

## 📥 Inputs
- `<IMAGE_TAG>` — the Docker image built in Step 2.
- `<OUTPUT_DIR>` — the output directory.
- `<TASK_DIR>/solution/solve.sh` — the reference solution script (source of target commands).

*(Hint: To quickly see relevant files in a repository, read the `files_found` list in `<OUTPUT_DIR>/project_detection_<reponame>.json`. If standard approaches fail, use your exploration tools to read nested config files or build scripts for deep inspection rather than guessing dependencies.)*

## ⚙️ Execution Phases

### Phase 1: Run the Verification Script

Pass `<TASK_DIR>/solution/solve.sh` directly to the verification script. The script reads the `.sh` file automatically and executes commands in a **single, persistent shell session** — `cd`, `export`, and variable assignments carry forward naturally.

```bash
python3 .claude/skills/docker-env-builder/scripts/verify_commands.py \
    --image-tag <IMAGE_TAG> \
    --project-dir <OUTPUT_DIR> \
    --target-commands-file <TASK_DIR>/solution/solve.sh \
    --output-file <OUTPUT_DIR>/verification_results.json \
    --timeout 300
```

The output JSON includes `metadata`, `metrics` (success rates, authenticity), and `details` (per-command results, timeouts, failures).

### Phase 2: Handle Failures and Rebuild

1. **Read the Results**
   Read the generated `<OUTPUT_DIR>/verification_results.json`. Pay close attention to `metrics.target_commands.success_rate` and the `details` array.

2. **Diagnose Timeouts and State Loss**
   - A timeout (exit code 124) means the command was interactive/TUI or hung. Treat it as `timed_out`, do not count it as an environment failure.
   - **CRITICAL TIMEOUT BEHAVIOR:** When a timeout occurs, the underlying shell is forcibly restarted to recover. This means **all previous shell state (`cd`, `export`) is lost** for subsequent commands. If a command fails immediately after a timeout (e.g., "file not found"), it is likely due to this state loss, not a missing dependency. Ignore such cascading failures.

3. **Diagnose Genuine Failures**
   For commands that explicitly failed (non-zero exit, not a timeout, and not a cascading failure from a previous timeout):
   - **Check for a known Special Case first.** Look up the error in the `special_cases` table in [STEP 2](STEP2.md). If it matches (e.g. GPU required, systemd required), record it in `special_cases_encountered` and move on. Do NOT rebuild.
   - **Is it a host resource dependency?** If the failure requires hardware or host-level capabilities that cannot be provided inside a container (GPU, specific kernel modules, hardware drivers), this is not fixable by editing the Dockerfile. Classify the situation: **Case 1** — the task is otherwise valid and just needs specific host conditions (e.g., an NVIDIA GPU machine); note this in `build_metadata.json` so the instruction can be updated to reflect the requirement. **Case 2** — the entire workflow depends on the unavailable resource with no equivalent alternative; accept the current maturity level and record the host dependency as the reason.
   - **Is it a `git://` protocol timeout?** If `git clone git://github.com/...` fails with a connection timeout (the `git://` protocol is often blocked), replace `git://github.com/` with `https://github.com/` in the Dockerfile `RUN` step. Do NOT rebuild for this alone — batch it with other fixes.
   - **Is it a cloud credentials failure?** If solve.sh fails with cloud auth errors (AWS credentials not found, GCP auth error, Azure login required), the task depends on real cloud infrastructure. Resolution order: (1) **Simulate cloud APIs locally** — replace the cloud service with a local container equivalent (e.g., LocalStack for AWS APIs, MinIO for S3); (2) **Simulate compute nodes with Docker** — if the task interacts with cloud VMs or cluster nodes (SSH, kubectl, etc.), replace them with local Docker containers or a local cluster (e.g., `kind`/`k3s` for Kubernetes, a Docker container with SSH for remote-host tasks); (3) **Rewrite as offline-equivalent** — if the core task objective can be preserved without any cloud calls, redesign the workflow to use local files or services; (4) **If none is feasible** — accept the current maturity level and record the cloud dependency as the reason. Never embed real credentials in the Dockerfile or image.
   - **Is it an outdated or removed package?** If a package installation or import fails because the package has changed since the recording (renamed, removed, API-breaking update), check `source/info.json` for the recording timestamp and look up the package version available at that time in the registry history (PyPI, npm, etc.). Resolution order: (1) **Pin to recording-era version** — add a version pin in the Dockerfile `RUN` step; (2) **Adapt to current version** — if the API change is minor and the task objective is preserved, update the relevant commands in the Dockerfile; (3) **If the package is gone with no equivalent** — accept the current maturity level and record the dependency as the reason.
   - **Is it an environment issue?** If the failure indicates a missing tool, library, or misconfigured service (e.g., `command not found`, `libGL.so missing`), fix it by updating the Dockerfile/Compose file and rebuilding. *(Critical Rule: If a dependency genuinely cannot be installed — e.g. upstream repo is dead, or architecture is incompatible — you must fail and report the reason. NEVER write fake shell scripts or mock files to bypass the error.)*
   - **Is it a missing input file?** If the failure is `No such file or directory` on a file that solve.sh reads but does not create, this is an environment gap — the input file was not provided in the image. Fix: (1) check `env_signals.json`, `solve.sh`, and `source/info.json` for a download URL; if found, add a `RUN curl/wget` step to the Dockerfile; (2) if no URL is available, synthesize the file from solve.sh + info.json (read `source/recording.txt` only if still insufficient), place it in the output directory, and add a `COPY` to the Dockerfile with a comment `# synthesized: <file> not available from source; inferred from solve.sh`. Counts toward the 5-attempt limit.
   - **Is it a user code issue?** If the failure is purely due to user logic (e.g., a Python `SyntaxError` in their script, or a failing unit test assertion), the environment successfully reproduced the user's error. Treat this as a successful verification. Do NOT rebuild.

4. **Fix and Rebuild (If necessary)**
   If you identified a genuine environment issue:
   - Update `<OUTPUT_DIR>/Dockerfile` or the Compose file to install the missing dependency.
   - If the stack is still running, tear it down first:
     ```bash
     IMAGE_TAG=<IMAGE_TAG> COMPOSE_PROJECT_NAME=<IMAGE_TAG> docker compose down
     ```
   - Return to [STEP 2](STEP2.md) and rebuild. Count this as an additional build attempt toward the 5-attempt limit.

   If the remaining failures are not fixable environment issues, or you have already reached a meaningful maturity level, accept the current level and proceed to cleanup.

## 📤 Output

| Artifact | Description |
|---|---|
| `<OUTPUT_DIR>/verification_results.json` | Contains `metadata`, `metrics`, and `details` for the target command run. |

---
**Next Step:** After verification completes, return to [SKILL.md](../SKILL.md) to run the final cleanup script and deliver the environment.
