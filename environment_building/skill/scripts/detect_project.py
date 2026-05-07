#!/usr/bin/env python3
"""Scan a source code repository and report raw facts.

Deterministic project analysis — no LLM calls, no inference.
Outputs only observable facts from the repository filesystem:
which key files exist, what version strings are written in lock files,
and what packages are listed in dependency manifests.
All interpretation of these facts is left to the agent.

Usage:
    # Clone the repo first, then scan the local directory
    git clone --depth 1 https://github.com/user/repo /tmp/repo
    python3 scripts/detect_project.py --repo-dir /tmp/repo

Output JSON:
{
  "files_found": ["requirements.txt", "manage.py", "package.json", ".env.example"],
  "parsed_dependencies": {
    "requirements.txt": ["django", "psycopg2-binary"],
    "package.json": ["react", "webpack"]
  },
  "python_version": "3.11",
  "node_version": "18",
  "go_version": null,
  "existing_dockerfile": false,
  "existing_compose": false
}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# =========================================================================
# Files to scan for existence (unified list — no categorisation)
# =========================================================================

# Every file in this list is checked for existence under the repo root.
# Subdirectory paths (e.g. "cmd/main.go") are checked as-is.
# The agent decides what each file's presence means.
_SCAN_FILES: list[str] = [
    # Python
    "requirements.txt", "setup.py", "pyproject.toml", "Pipfile",
    "poetry.lock", "environment.yml", "setup.cfg", "tox.ini",
    "manage.py", "wsgi.py", "asgi.py", "app.py",
    "main.py", "run.py", "server.py", "__main__.py",
    "runtime.txt", ".python-version", "uv.lock", "uv.toml",
    "pytest.ini", ".coveragerc",
    # Node.js
    "package.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "next.config.js", "next.config.mjs", "next.config.ts",
    "nuxt.config.ts", "nuxt.config.js",
    "vite.config.ts", "vite.config.js",
    "angular.json", "svelte.config.js",
    "index.js", "server.js", "app.js", "main.js",
    "index.ts", "server.ts", "app.ts", "main.ts",
    ".nvmrc", ".node-version", "package-lock.json",
    # Go
    "go.mod", "go.sum", "main.go", "cmd/main.go",
    # Rust
    "Cargo.toml", "Cargo.lock",
    "main.rs", "src/main.rs",
    "rust-toolchain", "rust-toolchain.toml",
    # Ruby
    "Gemfile", "Gemfile.lock", "Rakefile",
    ".ruby-version",
    # PHP
    "composer.json", "composer.lock", "artisan",
    "index.php", "public/index.php",
    # Java
    "pom.xml", "build.gradle", "build.gradle.kts", "gradlew",
    "Main.java", "src/Main.java", ".java-version", "mvnw",
    "src/main/resources/application.properties", "src/main/resources/application.yml",
    "src/main/webapp/WEB-INF/web.xml", "META-INF/MANIFEST.MF",
    # C/C++
    "Makefile", "CMakeLists.txt", "configure", "configure.ac",
    "meson.build", "SConstruct",
    # TypeScript
    "tsconfig.json", "tsconfig.node.json",
    "tsconfig.app.json", "tsconfig.base.json",
    # Docker
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "compose.yml", "compose.yaml",
    # CI/CD & Automation (Crucial for system dependencies)
    ".github/workflows/main.yml", ".github/workflows/build.yml", ".github/workflows/test.yml", ".github/workflows/ci.yml",
    ".gitlab-ci.yml", ".travis.yml", "circle.yml", ".circleci/config.yml",
    # Environment & config
    ".env.example", ".env.sample", ".env.template",
    "Procfile",
    # Misc
    "prisma/schema.prisma", "mypy.ini",
]


# =========================================================================
# Parsers for known dependency manifest files
# =========================================================================

def _parse_requirements_txt(path: Path) -> list[str]:
    deps = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        pkg = re.split(r"[>=<!\[;@]", line)[0].strip()
        if pkg:
            deps.append(pkg)
    return deps


def _parse_package_json(path: Path) -> tuple[list[str], str | None]:
    try:
        data = json.loads(path.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return [], None
    deps = list(data.get("dependencies", {}).keys())
    deps += list(data.get("devDependencies", {}).keys())
    node_ver = data.get("engines", {}).get("node")
    return deps, node_ver


def _parse_go_mod(path: Path) -> tuple[list[str], str | None]:
    text = path.read_text(errors="replace")
    go_ver_m = re.search(r"^go\s+(\d+\.\d+)", text, re.MULTILINE)
    go_ver = go_ver_m.group(1) if go_ver_m else None
    requires = re.findall(r"^\s+([\w./-]+)\s+v", text, re.MULTILINE)
    return requires, go_ver


def _parse_gemfile(path: Path) -> list[str]:
    deps = []
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r"""^\s*gem\s+['"]([^'"]+)['"]""", line)
        if m:
            deps.append(m.group(1))
    return deps


def _parse_composer_json(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return []
    deps = list(data.get("require", {}).keys())
    deps += list(data.get("require-dev", {}).keys())
    return [d for d in deps if not d.startswith("php") and d != "ext-*"]


def _parse_cargo_toml(path: Path) -> list[str]:
    deps = []
    in_deps = False
    for line in path.read_text(errors="replace").splitlines():
        if re.match(r"^\[.*dependencies.*\]", line):
            in_deps = True
            continue
        if line.startswith("[") and in_deps:
            in_deps = False
            continue
        if in_deps:
            m = re.match(r'^(\S+)\s*=', line)
            if m:
                deps.append(m.group(1))
    return deps


def _parse_pom_xml(path: Path) -> list[str]:
    text = path.read_text(errors="replace")
    return re.findall(r"<artifactId>([^<]+)</artifactId>", text)


# =========================================================================
# Version extraction from lock / toolchain files
# =========================================================================

def _detect_python_version(repo_dir: Path) -> str | None:
    for name in ("runtime.txt", ".python-version"):
        p = repo_dir / name
        if p.exists():
            m = re.search(r"(\d+\.\d+)", p.read_text(errors="replace"))
            if m:
                return m.group(1)
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.exists():
        m = re.search(
            r'requires-python\s*=\s*["\']>=?(\d+\.\d+)',
            pyproject.read_text(errors="replace"),
        )
        if m:
            return m.group(1)
    return None


def _detect_node_version(repo_dir: Path) -> str | None:
    for name in (".nvmrc", ".node-version"):
        p = repo_dir / name
        if p.exists():
            m = re.search(r"(\d+[\.\d]*)", p.read_text(errors="replace").strip())
            if m:
                return m.group(1)
    return None


# =========================================================================
# Main detection logic
# =========================================================================

def detect(repo_dir: Path) -> dict:
    """Scan a repo directory and return raw observable facts."""

    # --- Files found ---
    files_found: set[str] = {f for f in _SCAN_FILES if (repo_dir / f).exists()}

    # Also pick up any extra requirements*.txt files not in the static list
    for extra in sorted(repo_dir.glob("requirements*.txt")):
        files_found.add(extra.name)

    # --- Parsed dependencies (manifest file contents) ---
    parsed_deps: dict[str, list[str]] = {}
    nv_pkg: str | None = None

    if (repo_dir / "requirements.txt").exists():
        parsed_deps["requirements.txt"] = _parse_requirements_txt(
            repo_dir / "requirements.txt"
        )

    # Parse any additional requirements*.txt files (requirements-dev.txt, etc.)
    for extra_req in sorted(repo_dir.glob("requirements*.txt")):
        name = extra_req.name
        if name not in parsed_deps:
            parsed_deps[name] = _parse_requirements_txt(extra_req)

    if (repo_dir / "package.json").exists():
        deps, nv_pkg = _parse_package_json(repo_dir / "package.json")
        parsed_deps["package.json"] = deps

    if (repo_dir / "go.mod").exists():
        deps, go_version = _parse_go_mod(repo_dir / "go.mod")
        parsed_deps["go.mod"] = deps
    else:
        go_version = None

    if (repo_dir / "Gemfile").exists():
        parsed_deps["Gemfile"] = _parse_gemfile(repo_dir / "Gemfile")

    if (repo_dir / "composer.json").exists():
        parsed_deps["composer.json"] = _parse_composer_json(repo_dir / "composer.json")

    if (repo_dir / "Cargo.toml").exists():
        parsed_deps["Cargo.toml"] = _parse_cargo_toml(repo_dir / "Cargo.toml")

    if (repo_dir / "pom.xml").exists():
        parsed_deps["pom.xml"] = _parse_pom_xml(repo_dir / "pom.xml")

    # --- Version strings (read from explicit lock/toolchain files) ---
    python_version = _detect_python_version(repo_dir)
    # .nvmrc / .node-version take precedence; fall back to package.json engines
    node_version = _detect_node_version(repo_dir) or nv_pkg

    # --- Docker file existence ---
    existing_dockerfile = (repo_dir / "Dockerfile").exists()
    existing_compose = any(
        (repo_dir / n).exists()
        for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
    )

    return {
        "files_found": sorted(files_found),
        "parsed_dependencies": parsed_deps,
        "python_version": python_version,
        "node_version": node_version,
        "go_version": go_version,
        "existing_dockerfile": existing_dockerfile,
        "existing_compose": existing_compose,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan a local repo directory and report raw facts (files, deps, versions).",
    )
    parser.add_argument("--repo-dir", required=True, help="Path to local repo directory")
    parser.add_argument("--output-file", default="",
                        help="Write JSON to file (default: stdout)")
    args = parser.parse_args()

    repo_dir = Path(args.repo_dir)
    if not repo_dir.is_dir():
        print(json.dumps({"error": f"Directory not found: {repo_dir}"}))
        sys.exit(1)

    result = detect(repo_dir)
    output_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output_file:
        Path(args.output_file).write_text(output_json, encoding="utf-8")
        print(f"Project scan written to {args.output_file}")
        summary = {k: v for k, v in result.items() if k != "parsed_dependencies"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(output_json)


if __name__ == "__main__":
    main()
