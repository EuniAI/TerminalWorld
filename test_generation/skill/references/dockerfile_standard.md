# Dockerfile Quality Standard

This document defines the quality criteria for `environment/Dockerfile` in terminal benchmark tasks.

## Core Purpose

The Dockerfile sets up the environment in which the agent will work. It must provide the basic tools and dependencies needed, but must NOT contain the solution or test infrastructure.

## Hard Requirements

### 1. No Solution Leakage (Anti-cheat)

- **NEVER** `COPY solution/` or `COPY tests/` into the image — the Harbor harness handles these separately
- **NEVER** run `solve.sh` or any solution logic during `docker build`
- **NEVER** pre-create files that tests will check (e.g. `/app/result.txt`) — these must be created by the agent's solution at runtime
- The answer must not be embedded in Docker image layers or pre-computed files

### 2. WORKDIR

- Use `WORKDIR /app` as the standard working directory

### 3. Base Image

- Choose an appropriate base image for the task (e.g. `ubuntu:22.04`, `python:3.11`, `node:20`)
- Avoid unnecessarily large images when a smaller one suffices
- Avoid `latest` tags — use specific version tags for reproducibility

### 4. Apt Package Management

- Always pair `apt-get update` with package installation in the same `RUN` layer
- Clean up after: `rm -rf /var/lib/apt/lists/*`
- Do **NOT** pin apt package versions (they break as repos update)

```dockerfile
# Correct
RUN apt-get update && \
    apt-get install -y curl git && \
    rm -rf /var/lib/apt/lists/*

# Wrong — pinned versions break over time
RUN apt-get install -y curl=7.81.0-1ubuntu1.6
```

### 5. Test Dependencies Belong in test.sh

- `pytest`, `uv`, and other test-only tools belong in `tests/test.sh`, NOT in the Dockerfile
- The Dockerfile should only install what the **agent** needs to solve the task

### 6. Dependency Scope

The Dockerfile should be **minimal** — only provide what's needed to make the container a viable starting point. Complex environment configuration and service deployment are part of the task itself.

- **Dockerfile provides:** base OS, basic system tools (curl, git, etc.), language runtimes if needed
- **solve.sh handles:** installing task-specific software, configuring services, deploying components, any setup that requires domain expertise

The guiding principle: if setting something up requires expertise or non-trivial steps, it's part of the task and belongs in solve.sh. The Dockerfile is just a clean starting point.

### 7. Docker Compose

Some tasks require `docker-compose.yaml` (multi-container setups, privileged mode, custom networks, volume mounts). When present:

- The file **must** be named `docker-compose.yaml` in `environment/` — Harbor only recognizes this exact name. Variants (`compose.yml`, `docker-compose.yml`, `compose.yaml`) are silently ignored. The audit script auto-renames them.
- It should define the infrastructure topology only, not embed solution logic
- Services that the task interacts with (databases, message queues, etc.) can be declared here
- Same anti-cheat rules apply: no pre-running solutions, no embedding answers

### 8. Docker-in-Docker: Use DooD, Not DinD

Tasks that require running Docker commands inside the container **must use DooD (Docker-out-of-Docker)**, not DinD (Docker-in-Docker).

- **DinD** (starting a nested `dockerd` inside the container) requires `unshare` with elevated kernel privileges. This is **NOT supported** on the Harbor benchmark host — `unshare` fails even inside `--privileged` containers on this machine.
- **DooD** (mounting the host's Docker socket) works correctly and is the required pattern.

```yaml
# docker-compose.yaml — correct DooD pattern
services:
  app:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      DOCKER_HOST: unix:///var/run/docker.sock
```

The Dockerfile should install the Docker CLI (not `docker.io` which includes the daemon — use `docker-ce-cli` or install just the `docker` binary), and solve.sh should use Docker directly without starting `dockerd`.

If the original task uses DinD (`dockerd` startup in solve.sh or docker-compose), **convert it to DooD** — this does not change the task's core objective (the agent still uses Docker the same way), it only changes where the daemon runs.

### 9. Environment Reliability

For tasks with complex environments (running services, databases, specific configurations), the Dockerfile must **reliably** reproduce that complexity. A task that fails because the environment is flaky — not because the agent lacks skill — is not a valid benchmark task. Test the Dockerfile build independently before blaming the solution or tests.

### 10. Environment Pre-initialization Scripts

If `environment/` contains setup or seed scripts (e.g. `seed-databases.sh`, `init.sql`, `create_users.py`), the Dockerfile **must** call them during `docker build` — do NOT leave them unused in the directory.

These scripts pre-populate the container so the agent starts in a ready, realistic environment. An unused seed script means the agent arrives in an empty state, causing test failures that have nothing to do with the agent's skill.

```dockerfile
# Correct — seed script called at build time
COPY seed-databases.sh /tmp/
RUN bash /tmp/seed-databases.sh && rm /tmp/seed-databases.sh
```

Rule 1 (no solution leakage) still applies: only call environment initialization scripts — do NOT call scripts that run the solution or pre-create files that tests will verify.

### 11. Synthesized Input Data

If `solve.sh` reads files that are not available in the environment and cannot be created by the agent during task execution (input datasets, configuration files, seed resources), they must be provided in the Docker image. Follow this resolution order:

1. **Download** — check `solve.sh` and `source/info.json` for a `curl`/`wget`/`git clone` URL. If found, add a `RUN` download step to the Dockerfile.
2. **Synthesize** — if no URL is available: infer the file's content and structure from how solve.sh uses it and from `source/info.json`. Place the file in `environment/` and add a `COPY` step with a comment:

```dockerfile
# synthesized: data.csv not available from source; inferred from solve.sh
COPY data.csv /app/data.csv
```

**This is distinct from Rule 1 (No Solution Leakage):** only provide files solve.sh *reads as input* — never pre-create files solve.sh is supposed to *produce* as output.

## Evaluation Checklist

1. **No solution leakage?** No COPY of solution/ or tests/; no pre-run of solve.sh; no pre-created output files?
2. **WORKDIR /app?** Standard working directory set?
3. **Appropriate base image?** Reasonable choice, specific version tag?
4. **Clean apt usage?** `apt-get update` + `rm -rf /var/lib/apt/lists/*`, no pinned versions?
5. **No test deps?** pytest/uv not in Dockerfile?
6. **Minimal scope?** Only base OS + basic tools; complex setup/deployment in solve.sh?
7. **Docker Compose?** If present, defines infrastructure only, no solution logic?
8. **DinD → DooD?** If task uses Docker-in-Docker, convert to socket-mount pattern?
9. **Pre-init scripts called?** If `environment/` has setup/seed scripts, are they called in a `RUN` step?
10. **Input dependencies satisfied?** Does the Dockerfile provide (via download or synthesized `COPY`) all files that solve.sh reads but does not create?
