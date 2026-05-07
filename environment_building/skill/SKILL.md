---
name: docker-env-builder
description: >
  Builds Docker environments from environment signals and a target solution script.
  Creates a Dockerfile (and optional docker-compose.yaml) that reproduces
  a working execution environment with the right base image, dependencies,
  and tools installed. Use when asked to build, reconstruct, or reproduce
  a Docker environment from environment signals and solve.sh.
---

# Docker Environment Builder

Build a working Docker environment from environment signals and a target solution script.

## When to Use This Skill

Invoke this skill when:
- Building a Docker environment to reproduce a specific execution context
- Creating a container where certain commands, tools, or code can run
- The prompt contains `env_signals.json` and `solve.sh` as inputs

## Do NOT Use This Skill For

- General-purpose production Dockerfile optimization (no spec provided)
- Validating or auditing existing Dockerfiles
- Managing Docker images, registries, or orchestration
- Deploying to production environments

## Prerequisites

- Docker installed and running on the host
- `<OUTPUT_DIR>/env_signals.json` and `<TASK_DIR>/solution/solve.sh` available

## Core Principles (No-Mock / Authenticity Policy)

This skill reconstructs real runnable environments. Do not produce
"verification by illusion".

Hard prohibitions:
- Fake binaries (scripts that impersonate real tools just to satisfy checks)
- Fake modules/stubs to satisfy imports without real dependency installation
- Static output replay as a substitute for real command execution
- Embedding replay/run/check task logic in `docker build` layers

Allowed:
- Skip irreproducible interactive steps with explicit evidence
- Record non-blocking authenticity warnings in verification output (`mode=warn`)
- Keep Dockerfile environment-focused; run command replay/verification outside
  build layers

External references:
- External links, repository locations, documentation pages, issue threads, blog posts, and similar references may contain important evidence. You may open/read them when they look relevant, and you must decide for yourself which ones materially affect environment reconstruction.
- If an external resource is private, inaccessible, dead, or otherwise problematic, do NOT invent substitutes. Do not search for mirrors, forks, renamed repositories, replacement packages, or "equivalent" projects unless the user explicitly provided that alternative evidence.

## Workflow

Execute these steps in order. Each step links to the next.

1. **[Step 1: Write the Dockerfile](workflow/STEP1.md)** — Read `env_signals.json` and `solve.sh`, optionally scan source repos, and write the `Dockerfile` (and optional `docker-compose.yaml`) directly. Lint before building.
2. **[Step 2: Build the Docker Image](workflow/STEP2.md)** — Build the image with log capture, diagnose failures with the log parser, and apply targeted fixes within a 5-attempt limit.
3. **[Step 3: Verify with Target Commands](workflow/STEP3.md)** — Run `solve.sh` commands inside the container, handle timeouts and failures, and feed genuine environment issues back into Step 2.

Once Step 3 completes, the task is finished. The final artifacts (`Dockerfile`, `docker-compose.yaml`, and `verification_results.json`) will be automatically preserved in `<OUTPUT_DIR>`.

## Script Reference

**Parameter help**: Each script supports `--help`. Run
`python3 .claude/skills/docker-env-builder/scripts/<script>.py --help`
for the full parameter list when the inline comments in the workflow are not enough.

**Script debugging**: Do NOT read script sources upfront. If a script produces
unexpected output, exits with a non-zero code for no obvious reason, or behaves
differently than described, read the relevant source to determine whether it is
a **script bug** (wrong logic, missing edge case) or a **scenario issue**
(unexpected input, environment mismatch), then decide whether to work around it
or adjust your inputs.

- [scripts/detect_project.py](scripts/detect_project.py) — project detection logic
- [scripts/lint_dockerfile.py](scripts/lint_dockerfile.py) — Dockerfile linting rules
- [scripts/build_image.py](scripts/build_image.py) — Docker build execution and log capture
- [scripts/verify_commands.py](scripts/verify_commands.py) — command verification against the image
- [scripts/parse_build_log.py](scripts/parse_build_log.py) — structured extraction from build logs
- [scripts/cleanup.py](scripts/cleanup.py) — safe teardown of Docker resources and repo clones
