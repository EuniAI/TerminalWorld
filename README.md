# TerminalWorld

**TerminalWorld** is a scalable data engine that automatically reverse-engineers high-fidelity evaluation tasks from in-the-wild terminal recordings. Processing 80,870 asciinema recordings, it yields a benchmark of **1,530 validated terminal tasks** spanning 19 real-world categories and 1,280 unique commands — authentic and scalable by construction.

> **Paper:** [*TerminalWorld: Benchmarking Agents on Real-World Terminal Tasks*](https://arxiv.org/pdf/2605.22535)
> **Code:** [https://github.com/EuniAI/TerminalWorld](https://github.com/EuniAI/TerminalWorld)  
> **Dataset:** [https://huggingface.co/datasets/EuniAI/TerminalWorld](https://huggingface.co/datasets/EuniAI/TerminalWorld)

---

## Overview

Existing terminal benchmarks rely on manual expert curation, which introduces an adversarial bias and cannot scale with evolving developer practices. TerminalWorld addresses this by reverse-engineering evaluation tasks from real developer recordings shared on [asciinema.org](https://asciinema.org), inheriting their authenticity by construction.

The pipeline operates in four stages:

```
asciinema recordings (80,870)
        │
        ▼  1. Data Retrieval & Filtering
   9,492 high-quality recordings
        │
        ▼  2. Task Synthesis
   instruction.md + solve.sh (reference solution)
        │
        ▼  3. Environment Reproduction
   Dockerfile + docker-compose.yaml (5,035 reproduced)
        │
        ▼  4. Test Suite Generation & Validation
   1,530 validated tasks (AllPassing / Nop / Partial trials)
```

---

## Repository Structure

```
TerminalWorld/
├── data_retrieval/          # Stage 1a: crawl and download asciinema recordings
│   ├── scrape_pages.py      #   Index explore feeds (public / recent / featured / popular)
│   ├── download_recordings.py #  Download .txt transcripts + info.json metadata
│   ├── parsers.py           #   HTML parsers for listing and detail pages
│   ├── config.py            #   Shared crawler configuration
│   └── stats.py             #   Dataset statistics
│
├── data_filtering/          # Stage 1b: filter recordings by quality criteria
│   ├── detect_pii.py        #   Flag PII, credentials, and malicious commands
│   ├── classify_tui.py      #   Detect TUI tool invocations (vim, htop, tmux, …)
│   ├── detect_external_urls.py # Identify and verify external repository links
│   ├── analyze_duration.py  #   Extract and analyze recording durations
│   ├── score_value.py       #   Two-stage LLM quality scoring (feasibility + value)
│   └── filter_recordings.py #   Combine all filters into a single filtering pass
│
├── task_synthesis/          # Stage 2: synthesize task instruction and reference solution
│   ├── generate_instruction.py  # LLM-based outcome-oriented instruction generation
│   ├── extract_solution.py      # LLM-based reference solution extraction from transcript
│   └── generate_task_metadata.py
│
├── environment_building/    # Stage 3: reproduce executable Docker environments
│   ├── build_environment.py  # LLM agent: synthesize and refine Dockerfile
│   ├── analyze_recording.py  # Parse transcript to extract environment signals
│   ├── batch_build.py        # Parallel batch environment reproduction
│   └── monitor.py
│
└── test_generation/         # Stage 4: generate and validate test suites
    ├── generate_tests.py     # LLM agent: snapshot-guided test suite generation
    ├── refine_task.py        # Trial-based refinement loop (AllPassing/Nop/Partial)
    ├── batch_refine.py       # Parallel batch refinement
    └── skill/                # Agent skill definitions and task format references
```

---

## Pipeline Details

### Stage 1 — Data Retrieval & Filtering

We index asciinema's public explore feeds and download the plain-text transcript (`recording.txt`) and metadata (`info.json`) for each recording. Raw cast files and generated media are intentionally **not** collected, in accordance with asciinema's terms of service.

Recordings are then filtered by five sequential criteria:
1. **Privacy & Safety** — exclude PII, exposed credentials, and malicious/destructive commands (`detect_pii.py`)
2. **CLI-only** — discard recordings that invoke TUI applications (vim, nano, htop, …) (`classify_tui.py`)
3. **Docker reproducibility** — remove recordings dependent on inaccessible URLs, Windows environments, or proprietary software (`detect_external_urls.py`)
4. **Minimum length** — eliminate excessively short or aborted sessions (`analyze_duration.py`)
5. **LLM quality scoring** — filter opaque or purely exploratory sessions using a two-stage scoring framework: rule-based feasibility pre-check followed by LLM scoring on three dimensions — *state-action alignment*, *task complexity*, and *signal clarity* (`score_value.py`)

**Result:** 80,870 → **9,492** high-quality recordings.

### Stage 2 — Task Synthesis

An LLM distills each transcript into two artifacts:
- **Instruction** (`instruction.md`): outcome-oriented natural language goal; describes *what* to achieve, never *how*; specifies required output paths and formats.
- **Reference solution** (`solve.sh`): clean, executable bash script extracted from the transcript; redirects final results to explicit file paths (e.g., `/app/result.txt`) for deterministic verification.

### Stage 3 — Environment Reproduction

An LLM agent synthesizes a `Dockerfile` (and `docker-compose.yaml` for multi-service tasks) by inferring dependencies from the reference solution. If the recording includes an external repository link, the agent clones and scans it to infer precise requirements. Fake binaries, stubbed dependencies, and bypasses of real software installation are explicitly prohibited.

The agent then enters an execution-feedback loop: build the image → parse build logs → launch the container → execute the reference solution step by step → feed runtime anomalies back for targeted repair.

**Result:** 9,492 → **5,035** reproduced environments.

### Stage 4 — Test Suite Generation & Validation

The agent captures pre- and post-execution filesystem snapshots in the Docker environment and generates state-based test assertions calibrated to the actual final state. Tests target persistent artifacts (file existence, content hashes, structured outputs) and avoid brittle non-deterministic checks (timestamps, process IDs).

Each test suite is then refined through three execution trials in fresh containers:

| Trial | Execution | Requirement | Guarantees |
|-------|-----------|-------------|------------|
| **AllPassing** | Full reference solution | All tests pass | Task *solvability* |
| **Nop** | Nothing (empty state) | All tests fail | Task *non-triviality* |
| **Partial** | Truncated / ablated solution | At least one test fails | Test *discriminability* |

A task is admitted only if all three trials pass simultaneously. Failed suites are iteratively repaired; tasks that exceed the computational budget are discarded.

**Result:** 5,035 → **1,530** validated tasks.

---

## The TerminalWorld Benchmark

| | Full Set | Verified Subset |
|--|--|--|
| **Tasks** | 1,530 | 200 |
| **Categories** | 19 | 19 |
| **Unique commands** | 1,280 | — |
| **Commands absent from Terminal-Bench** | 91% | — |
| **Human review** | Automated validation | ✓ (4 expert annotators) |

The **Verified** subset of 200 tasks was manually reviewed by four authors with 3+ years of terminal development experience. Each task was executed end-to-end inside the Docker environment to verify functional correctness and artifact alignment.

Benchmarking on the Verified subset across 8 frontier LLMs and 6 agent frameworks shows that the best system (Claude Opus 4.7 + Terminus-2) achieves a **62.5% pass rate**, with TerminalWorld scores only weakly correlated with Terminal-Bench scores (Pearson *r* = 0.20), confirming that in-the-wild recordings probe capabilities that expert-curated benchmarks miss.

---

## Ethics & Data Policy

- We collect only publicly listed `.txt` transcripts and their coupled metadata via asciinema's standard download links, in full compliance with `robots.txt`.
- Raw `.cast` files and generated media are **never downloaded or redistributed**.
- The released benchmark contains only synthesized artifacts (instructions, environments, tests) with hyperlinks back to the original recordings — no original transcripts are redistributed.
- Recordings are filtered for PII, credentials, and malicious content before any synthesis occurs.

See the paper's ethics section for a full discussion of copyright compliance and the right-to-be-forgotten architecture.
