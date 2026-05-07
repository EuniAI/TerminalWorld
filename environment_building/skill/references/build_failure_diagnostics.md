# Build Failure Diagnostics

When `docker build` fails during the iterative loop, use these commands and patterns to diagnose the issue quickly.

## Quick Diagnostic Checklist

1. **Read the build log** -- the error is usually in the last few lines
2. **Identify which RUN step failed** -- note the step number
3. **Check the exit code** -- `docker inspect --format='{{.State.ExitCode}}' <container>`
4. **Shell into a failed build** -- use the last successful layer to debug:
   ```bash
   # Run a shell in the image at the last successful build stage
   docker run --rm -it <last-successful-image-id> /bin/bash
   # Then manually run the failing command to see detailed output
   ```
5. **Check if it's a network issue** -- try `docker build --network=host`
6. **Check disk space** -- `docker system df` (use `docker builder prune -f` to clean up safely)

## Fast Iteration Strategies

When rebuilding after a fix, reuse cached layers from the previous attempt to avoid re-downloading packages and re-compiling from scratch:

```bash
# Enable BuildKit for faster, parallel layer builds (set once in shell)
export DOCKER_BUILDKIT=1

# Build with cache from the previous (possibly failed) image
docker build --cache-from <IMAGE_TAG> -t <IMAGE_TAG> <DIR>/

# If no previous image exists, --cache-from is silently ignored (safe to always use)
```

This is especially useful during the iterative fix loop: early layers (base image, system packages) are cached even if a later layer fails.

## Common Build Error Patterns

| Error Message | Likely Cause | Fix |
|---------------|-------------|-----|
| `E: Unable to locate package <pkg>` | Wrong package name for distro | Check distro-specific package name |
| `E: Release file is not valid yet` | Clock skew in container | Add `--allow-releaseinfo-change` or fix host clock |
| `NO_PUBKEY <key>` / GPG error | Missing signing key | `apt-key adv --keyserver keyserver.ubuntu.com --recv-keys <key>` |
| `Could not resolve host` | DNS failure during build | Use `docker build --network=host` |
| `pip: externally-managed-environment` | Modern distro pip restriction | Use `--break-system-packages` or create a venv |
| `npm ERR! ERESOLVE` | npm dependency conflict | Try `npm install --legacy-peer-deps` |
| `cargo build` fails with linker errors | Missing C libraries | Install `build-essential` and relevant `-dev` packages |
| `go: module requires Go >= 1.XX` | Go version too old | Update the Go download URL to the required version |
| `fatal: repository not found` | Wrong repo URL or private repo | Verify URL; private repos need auth tokens |
| `Temporary failure resolving 'archive.ubuntu.com'` | DNS in Docker build | `docker build --network=host` or configure Docker DNS |
| `Permission denied` / `EACCES` | File/dir permission issue | Add `chmod`/`chown`, or run as root during build |
| `Killed` (exit code 137) | Out of memory (OOM) | Increase Docker memory limit, or reduce parallelism (`make -j1`, `npm install --max-old-space-size=512`) |
| `no space left on device` | Disk full | `docker builder prune -f` to clear build cache |
| Build hangs / no output for >10 min | Network timeout or stuck download | Cancel, retry with `--network=host`; pin dependency versions to avoid slow resolution |
| `exec format error` | Architecture mismatch (amd64 binary on arm64 or vice versa) | Add `FROM --platform=linux/amd64` or `docker build --platform linux/amd64` |
| `rosetta error: ... is not registered` | Rosetta not installed on Apple Silicon | `softwareupdate --install-rosetta --agree-to-license`, or use `--platform linux/amd64` |
| Segfault during build on Apple Silicon | QEMU emulation bug with heavy compilation | Try `docker build --platform linux/amd64` with latest Docker Desktop; or build natively if ARM packages available |
| `Malformed input or unmappable character` / garbled non-ASCII output | Missing UTF-8 locale in container | Add to Dockerfile: `ENV LANG=C.UTF-8 LC_ALL=C.UTF-8`; or pass `-e LANG=C.UTF-8 -e LC_ALL=C.UTF-8` to `docker run` |

## Cleaning Up Between Attempts

```bash
# Remove the failed image to avoid cache issues
docker rmi <IMAGE_TAG> 2>/dev/null || true

# If layers are stuck, rebuild without cache
python3 .claude/skills/docker-env-builder/scripts/build_image.py \
    --image-tag <IMAGE_TAG> \
    --dockerfile-dir <OUTPUT_DIR> \
    --no-cache

# If disk is running low (clears build cache safely without affecting other containers)
docker builder prune -f
```
