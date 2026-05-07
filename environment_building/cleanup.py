#!/usr/bin/env python3
"""Cleanup script for Docker environment building.

Safely tears down any remaining Docker containers, compose networks,
and images associated with the given image tag, and removes the cloned
repositories to free up disk space.

Usage:
    python3 scripts/cleanup.py \
        --output-dir /path/to/output \
        --image-tag terminalworld-env-1060
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _has_compose(output_dir: Path) -> bool:
    return any(
        (output_dir / n).exists()
        for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
    )


def cleanup_container(image_tag: str) -> None:
    """Force remove the verification container if it exists."""
    container_name = f"verify-{image_tag}"
    print(f"Cleaning up container: {container_name}...")
    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=30,
        )
    except Exception as e:
        print(f"Warning: Failed to remove container {container_name}: {e}")


def cleanup_compose(output_dir: Path, image_tag: str) -> None:
    """Tear down the compose stack and its volumes."""
    print("Cleaning up compose stack and volumes...")
    env = os.environ.copy()
    env["IMAGE_TAG"] = image_tag
    env["COMPOSE_PROJECT_NAME"] = image_tag

    try:
        subprocess.run(
            ["docker", "compose", "down", "-v"],
            cwd=str(output_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=60,
        )
    except Exception as e:
        print(f"Warning: Failed to tear down compose stack: {e}")


def cleanup_repos(output_dir: Path) -> None:
    """Remove the cloned repositories directory."""
    repos_dir = output_dir / "repos"
    if repos_dir.exists() and repos_dir.is_dir():
        print(f"Cleaning up repository clones in {repos_dir}...")
        try:
            shutil.rmtree(repos_dir)
        except Exception as e:
            print(f"Warning: Failed to remove {repos_dir}: {e}")
    else:
        print(f"No repositories to clean up at {repos_dir}.")


def cleanup_image(image_tag: str) -> None:
    """Force remove the Docker image to free up disk space."""
    print(f"Cleaning up image: {image_tag}...")
    try:
        subprocess.run(
            ["docker", "rmi", "-f", image_tag],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=30,
        )
    except Exception as e:
        print(f"Warning: Failed to remove image {image_tag}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up Docker resources and cloned repos.")
    parser.add_argument("--output-dir", required=True, help="Path to the output directory")
    parser.add_argument("--image-tag", required=True, help="Docker image tag used for the build")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        print(f"Output directory {output_dir} does not exist. Nothing to clean up.")
        sys.exit(0)

    # 1. Container Cleanup
    cleanup_container(args.image_tag)

    # 2. Compose Cleanup
    if _has_compose(output_dir):
        cleanup_compose(output_dir, args.image_tag)

    # 3. Repo Cleanup
    cleanup_repos(output_dir)

    # 4. Image Cleanup
    cleanup_image(args.image_tag)

    print("Cleanup complete.")


if __name__ == "__main__":
    main()
