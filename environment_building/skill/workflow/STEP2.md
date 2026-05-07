# Step 2: Build the Docker Image

## 🎯 High-Level Objective
The goal of Step 2 is to build a Docker image from the Dockerfile produced in Step 1 and, if the build fails, systematically diagnose and fix the failure within a bounded number of attempts. The build script handles log capture and attempt tracking automatically; your job is to classify failures correctly and apply targeted fixes — without guessing or over-modifying the Dockerfile.

**CRITICAL BOUNDARY:** Your fixes must be strictly limited to modifying `<OUTPUT_DIR>/Dockerfile` or the generated Compose file (`<OUTPUT_DIR>/docker-compose.y*ml`). **NEVER** modify the user's source code, dependency manifest files, or any scripts in this repository to work around an error.

## 📥 Inputs
- `<OUTPUT_DIR>/Dockerfile` — the Dockerfile written and validated in Step 1.
- `<OUTPUT_DIR>/docker-compose.yaml` (or `.yml`) — (optional) the Compose file generated in Step 1.
- `<IMAGE_TAG>` — the Docker image name and tag to build (e.g. `myproject:latest`). Choose a consistent tag; it is reused in Step 3.

*(Hint: To quickly see relevant files in a repository, read the `files_found` list in `<OUTPUT_DIR>/project_detection_<reponame>.json`. If standard approaches fail, use your exploration tools to read nested config files or build scripts for deep inspection rather than guessing dependencies.)*

## ⚙️ Execution Phases

### Phase 1: Run the Build
Use the build script to build and capture logs in one atomic operation:

```bash
# --image-tag:       Docker image name:tag to build (required)
# --dockerfile-dir:  directory containing the Dockerfile (required)
# --cache-from:      image to reuse cached layers from (optional)
# --no-cache:        force full rebuild, ignoring all cache (optional)
# --build-timeout:   kill build after N seconds (default: 1200 — increase for very large images)
# Returns JSON: {success, exit_code, attempt, duration_s, image_size_mb, log_file, compose_mode}

# First attempt
python3 .claude/skills/docker-env-builder/scripts/build_image.py \
    --image-tag <IMAGE_TAG> \
    --dockerfile-dir <OUTPUT_DIR>

# Subsequent attempts (reuse cached layers to save time)
python3 .claude/skills/docker-env-builder/scripts/build_image.py \
    --image-tag <IMAGE_TAG> \
    --dockerfile-dir <OUTPUT_DIR> \
    --cache-from <IMAGE_TAG>
```

The script runs `DOCKER_BUILDKIT=1 docker build -t <IMAGE_TAG> .` in `<OUTPUT_DIR>`, captures the full log to `<OUTPUT_DIR>/build.log`, auto-increments the attempt counter, and returns structured JSON with `success`, `exit_code`, `attempt`, `duration_s`, `image_size_mb`, `log_file`, and `compose_mode`.

If the build **succeeds**, proceed to [STEP 3](STEP3.md).

### Phase 2: Diagnose Build Failures

If the build fails, run the log parser first to get a structured summary:

```bash
# Returns JSON: {failed_step, failed_line, error_tail}
python3 .claude/skills/docker-env-builder/scripts/parse_build_log.py \
    --log-file <OUTPUT_DIR>/build.log
```

Use `failed_step` and `failed_line` to locate the failing Dockerfile instruction, and `error_tail` as the concentrated error context. If the parser output is insufficient, read `build.log` in full.

When diagnosing a failure, determine whether the error is a **fundamental limitation** (a "special case") or a **fixable configuration issue**. Apply this decision order:

#### 1. Is this a known Special Case? (Check first)

Before attempting any fixes, check if the error is caused by a known limitation detected in the signals.
Read the `special_cases` array in `<OUTPUT_DIR>/env_signals.json`. If the error symptoms match a detected special case (e.g., systemd init errors, GUI display errors, interactive prompts hanging):
- Follow the recommended action provided in the `detail` field.
- Record the case in the `special_cases_encountered` array in `build_metadata.json`.
- **Do NOT count this as a build attempt and do NOT modify the Dockerfile.** Move on to the next step.

#### 2. Is this a Fixable Error? (Classify and retry)

If the error does not match any detected special cases, it is a fixable issue. Classify it and apply the targeted fix (max 5 attempts total):

- **Private / Deleted Repository or Submodule** ("fatal: could not read Username", "404 Not Found", "Permission denied (publickey)", "Repository not found")
  → **CRITICAL:** Do NOT attempt to find alternative mirrors, change the repository URL, or search the web for workarounds. Do not substitute a fork, renamed repository, unofficial package source, or "equivalent" project unless the user explicitly supplied that alternative evidence. First determine whether the unavailable repository or submodule is actually required for the current build/verification path. If it is a hard dependency, stop retrying, save the current Dockerfile and error log, set `"status": "Exception"` or `"status": "blocked_private_repo"` in `build_metadata.json` if possible, and finish the task. If it is not required for the environment work you can still complete, record the limitation clearly and continue.
- **Transient / Network errors** ("Could not resolve host", "Connection timed out")
  → Do NOT modify the Dockerfile. Retry, or pass `--network=host`.
- **Dependency errors** ("Unable to locate package", "No matching distribution found")
  → Add the missing package/library and rebuild.
- **Dependency Conflicts (Python pip / npm resolver errors)**
  → Do NOT blindly change the base image. Review the dependencies in the repository (`requirements.txt`, `setup.py`, etc.). Attempt to explicitly loosen or remove the strict version constraint on the failing package in your `RUN pip install` or `RUN npm install` command (e.g. change `==2.0` to `>=2.0` or remove the version entirely) to allow the resolver to find a compatible graph.
- **Configuration / Syntax errors** ("unknown instruction", "COPY failed", "Permission denied")
  → Fix the specific Dockerfile line causing the failure.
- **Resource errors** ("no space left on device", "Killed", exit code 137)
  → Run `docker builder prune -f` to free space, then retry.
- **Unrecognised errors**
  → Consult [references/build_failure_diagnostics.md](../references/build_failure_diagnostics.md) for additional patterns.

After applying a fix, rebuild by running `build_image.py` again (it auto-increments the attempt counter). If cached layers appear to cause stale failures, add `--no-cache`. Repeat until the build succeeds or 5 attempts are exhausted.

If still failing after 5 attempts, save the current Dockerfile and error log, set `"status": "build_failed"` in the metadata, and report what was tried.

## 📤 Output

| Artifact | Description |
|---|---|
| Docker image `<IMAGE_TAG>` | The built image, available for `docker run` or `docker compose up` in Step 3 |
| `<OUTPUT_DIR>/build.log` | Full build output for diagnostics |
| `<OUTPUT_DIR>/build_metadata.json` | Build attempt counter, final status, `compose_mode`, and `special_cases_encountered` |

---
**Next Step:** Proceed to [STEP 3](STEP3.md).
