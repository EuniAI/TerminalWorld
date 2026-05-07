#!/usr/bin/env python3
"""
Detect PII, sensitive credentials, and malicious/destructive commands in
terminal recordings.

Scans the plain-text transcript (recording.txt) of each recording and flags
recordings that contain any of the following three categories:

  1. PII (Personally Identifiable Information)
       - Email addresses
       - IPv4 addresses that appear in non-loopback, non-RFC-1918 contexts
       - Phone numbers (E.164 and common national formats)

  2. Sensitive credentials
       - AWS access/secret keys and session tokens
       - Generic API keys / tokens exported via shell variables or CLI flags
       - Private key PEM headers (RSA, EC, DSA, OpenSSH)
       - GitHub / GitLab / HuggingFace personal access tokens
       - JWT bearer tokens
       - Passwords embedded in commands (--password, PGPASSWORD=, etc.)

  3. Malicious or destructive commands
       - Recursive root/home deletions (rm -rf /, rm -rf ~, rm -rf *)
       - Fork bombs
       - Disk wipers (dd of=/dev/sd*, mkfs on raw devices)
       - curl/wget piped to shell execution

Output: output/pii_detection.json
    {
        "<recording_id>": {
            "label": "CLEAN" | "FLAGGED",
            "reasons": ["<category>", ...]   # empty list when CLEAN
        }
    }

Usage:
    python detect_pii.py
    python detect_pii.py --workers 16
    python detect_pii.py --verbose
    python detect_pii.py --output output/pii_detection.json
"""

import argparse
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)


# =============================================================================
# Detection Patterns
# =============================================================================

# --- PII ---

# Email addresses — exclude user@hostname patterns common in terminal output
# (SSH prompts, git log, Docker labels). Require a known TLD (2+ alpha chars)
# AND that the domain part contains at least one dot (i.e., a real FQDN).
# This avoids matching "ubuntu@ip-10-0-1-5" or "user@localhost" style strings.
_RE_EMAIL = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+\.[A-Za-z]{2,}\b'
)

# IPv4 addresses that are NOT loopback (127.x.x.x) or RFC-1918 private
# (10.x, 172.16-31.x, 192.168.x). These are likely real public IPs.
_RE_IPV4 = re.compile(
    r'\b(?!127\.)(?!10\.)(?!172\.(?:1[6-9]|2[0-9]|3[01])\.)(?!192\.168\.)'
    r'(?:\d{1,3}\.){3}\d{1,3}\b'
)

# E.164 international phone (+1-800-555-1234, +44 20 7946 0958, etc.)
_RE_PHONE = re.compile(
    r'(?<!\d)\+\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{1,4}[\s\-.]?\d{1,9}(?!\d)'
)


# --- Sensitive credentials ---

# AWS Access Key ID (20-char string starting with AKIA/ABIA/ACCA/ASIA)
_RE_AWS_KEY_ID = re.compile(r'\b(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b')

# AWS secret key: 40-char base64-like string following "aws_secret" patterns
_RE_AWS_SECRET = re.compile(
    r'(?i)(?:aws.?secret.?access.?key|AWS_SECRET_ACCESS_KEY)\s*[=:]\s*[A-Za-z0-9/+]{40}\b'
)

# AWS session token (very long base64 string, typically >100 chars)
_RE_AWS_SESSION = re.compile(
    r'(?i)(?:aws.?session.?token|AWS_SESSION_TOKEN)\s*[=:]\s*[A-Za-z0-9/+=]{100,}'
)

# PEM private key headers
_RE_PRIVATE_KEY = re.compile(
    r'-----BEGIN\s+(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----'
)

# GitHub personal access tokens (classic: ghp_, fine-grained: github_pat_)
_RE_GITHUB_TOKEN = re.compile(r'\b(?:ghp|ghs|ghu|ghr|github_pat)_[A-Za-z0-9_]{36,}\b')

# GitLab personal access tokens (glpat-)
_RE_GITLAB_TOKEN = re.compile(r'\bglpat-[A-Za-z0-9\-_]{20,}\b')

# HuggingFace tokens (hf_)
_RE_HF_TOKEN = re.compile(r'\bhf_[A-Za-z0-9]{30,}\b')

# JWT bearer tokens (three base64url segments separated by dots)
_RE_JWT = re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b')

# Generic API key / token assignment in shell variables or CLI flags.
# Matches patterns like: export API_KEY="abc123...", --api-key abc123, TOKEN=abc123
_RE_GENERIC_TOKEN = re.compile(
    r'(?i)(?:export\s+)?(?:[A-Z_]{3,}(?:_KEY|_TOKEN|_SECRET|_PASSWORD|_PASSWD|_PWD))\s*=\s*["\']?[A-Za-z0-9/+\-_.]{20,}["\']?'
)

# Password in command-line flags or common env vars
_RE_PASSWORD_FLAG = re.compile(
    r'(?i)(?:--password|--passwd|-p\s+|PGPASSWORD=|MYSQL_ROOT_PASSWORD=|MYSQL_PASSWORD=)\s*["\']?[^\s"\']{6,}'
)


# --- Malicious / destructive commands ---

# Recursive deletion of root, home, or wildcard in sensitive locations
_RE_DESTRUCTIVE_RM = re.compile(
    r'\brm\s+(?:[^\n]*\s)?-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+(?:/\s|/\*|~/|~\s|~\*|\*\s)'
    r'|\brm\s+(?:[^\n]*\s)?-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+(?:/\s|/\*|~/|~\s|~\*|\*\s)'
)

# Fork bomb variants
_RE_FORK_BOMB = re.compile(
    r':\(\)\s*\{.*?:\|:.*?\}|:\(\)\{.*?:\|:.*?\}'
)

# Disk wiper: dd writing to raw block devices (/dev/sda, /dev/nvme0n1, etc.)
_RE_DISK_WIPE = re.compile(
    r'\bdd\b[^\n]*\bof=/dev/(?:sd[a-z]|nvme\d|xvd[a-z]|vd[a-z]|hd[a-z])\b'
)

# mkfs on raw block devices (not on loop devices or image files)
_RE_MKFS = re.compile(
    r'\bmkfs(?:\.[a-z0-9]+)?\s+(?:/dev/(?:sd[a-z]|nvme\d|xvd[a-z]|vd[a-z]|hd[a-z]))\b'
)

# curl/wget piped to shell where the URL is a raw IP address (not a domain).
# Standard tool installers (nvm, Homebrew, Poetry, etc.) use domain names and
# are legitimate developer workflows — flagging those would cause many false
# positives. Only flag IP-based fetch-and-exec, which has no legitimate use.
_RE_CURL_PIPE_SHELL = re.compile(
    r'(?:curl|wget)\s[^\n]*https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}[^\n]*\|\s*(?:bash|sh|zsh|dash|python|perl|ruby)\b'
)


# =============================================================================
# Detection logic
# =============================================================================

# Map of (category_label, compiled_regex) pairs
_CHECKS: list[tuple[str, re.Pattern]] = [
    # PII
    ("pii_email",           _RE_EMAIL),
    ("pii_public_ipv4",     _RE_IPV4),
    ("pii_phone",           _RE_PHONE),
    # Credentials
    ("cred_aws_key_id",     _RE_AWS_KEY_ID),
    ("cred_aws_secret",     _RE_AWS_SECRET),
    ("cred_aws_session",    _RE_AWS_SESSION),
    ("cred_private_key",    _RE_PRIVATE_KEY),
    ("cred_github_token",   _RE_GITHUB_TOKEN),
    ("cred_gitlab_token",   _RE_GITLAB_TOKEN),
    ("cred_hf_token",       _RE_HF_TOKEN),
    ("cred_jwt",            _RE_JWT),
    ("cred_generic_token",  _RE_GENERIC_TOKEN),
    ("cred_password_flag",  _RE_PASSWORD_FLAG),
    # Malicious/destructive
    ("malicious_rm_rf",     _RE_DESTRUCTIVE_RM),
    ("malicious_fork_bomb", _RE_FORK_BOMB),
    ("malicious_disk_wipe", _RE_DISK_WIPE),
    ("malicious_mkfs",      _RE_MKFS),
    ("malicious_curl_pipe", _RE_CURL_PIPE_SHELL),
]

LABEL_CLEAN   = "CLEAN"
LABEL_FLAGGED = "FLAGGED"


def scan_transcript(txt_path: Path) -> list[str]:
    """
    Scan a plain-text transcript for PII, credentials, and malicious commands.

    Returns a list of matched category labels (empty if none found).
    """
    try:
        text = txt_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug(f"Cannot read {txt_path}: {exc}")
        return []

    matched = []
    for label, pattern in _CHECKS:
        if pattern.search(text):
            matched.append(label)
            logger.debug(f"  Matched: {label}")

    return matched


def classify_recording(recording_dir: Path) -> tuple[str, dict]:
    """
    Classify a single recording directory.

    Returns (recording_id, result_dict) where result_dict has keys:
        label   – "CLEAN" or "FLAGGED"
        reasons – list of matched category labels
    """
    recording_id = recording_dir.name
    txt_path = recording_dir / "recording.txt"

    if not txt_path.exists():
        logger.debug(f"{recording_id}: recording.txt not found, skipping")
        return recording_id, {"label": LABEL_CLEAN, "reasons": []}

    reasons = scan_transcript(txt_path)
    label = LABEL_FLAGGED if reasons else LABEL_CLEAN

    if label == LABEL_FLAGGED:
        logger.debug(f"{recording_id}: FLAGGED — {', '.join(reasons)}")

    return recording_id, {"label": label, "reasons": reasons}


# =============================================================================
# Entry point
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect PII, credentials, and malicious commands in terminal recordings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Detection categories:
  PII         pii_email, pii_public_ipv4, pii_phone
  Credentials cred_aws_key_id, cred_aws_secret, cred_aws_session,
              cred_private_key, cred_github_token, cred_gitlab_token,
              cred_hf_token, cred_jwt, cred_generic_token, cred_password_flag
  Malicious   malicious_rm_rf, malicious_fork_bomb, malicious_disk_wipe,
              malicious_mkfs, malicious_curl_pipe

Output JSON format:
  { "<id>": { "label": "CLEAN" | "FLAGGED", "reasons": [...] } }

Examples:
  python detect_pii.py
  python detect_pii.py --workers 16 --verbose
  python detect_pii.py --output output/pii_detection.json
        """,
    )
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "recordings",
        help="Path to recordings directory (default: ../data/recordings)",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=Path(__file__).parent / "output" / "pii_detection.json",
        help="Output JSON file path (default: output/pii_detection.json)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of parallel workers (default: 8)",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Optional log file path",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )

    args = parser.parse_args()

    # Logging setup
    log_level = logging.DEBUG if args.verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )

    if not args.recordings_dir.exists():
        logger.error(f"Recordings directory not found: {args.recordings_dir}")
        return 1

    args.output_file.parent.mkdir(parents=True, exist_ok=True)

    # Discover recording directories (those containing info.json)
    recording_dirs = sorted(
        [p.parent for p in args.recordings_dir.glob("*/info.json")],
        key=lambda p: p.name,
    )
    total = len(recording_dirs)
    logger.info(f"Found {total:,} recordings in {args.recordings_dir}")
    logger.info(f"Starting PII/credential/malicious detection with {args.workers} workers...")

    results: dict[str, dict] = {}
    processed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(classify_recording, d): d for d in recording_dirs}
        for future in as_completed(futures):
            recording_id, result = future.result()
            results[recording_id] = result
            processed += 1
            if processed % 1000 == 0:
                logger.info(f"Processed {processed:,}/{total:,}...")

    logger.info(f"Processed {processed:,} recordings")

    # Write sorted output
    sorted_results = dict(sorted(results.items()))
    with open(args.output_file, "w", encoding="utf-8") as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    logger.info(f"Results written to: {args.output_file}")

    # Summary
    flagged = [rid for rid, r in results.items() if r["label"] == LABEL_FLAGGED]
    clean   = total - len(flagged)

    # Count by category
    category_counts: dict[str, int] = {}
    for r in results.values():
        for reason in r["reasons"]:
            category_counts[reason] = category_counts.get(reason, 0) + 1

    summary_lines = [
        "",
        "=" * 60,
        "PII / Credential / Malicious Detection Summary",
        "=" * 60,
        f"  Total recordings:  {total:,}",
        f"  CLEAN:             {clean:,} ({clean/total*100:.1f}%)" if total else "  CLEAN: 0",
        f"  FLAGGED:           {len(flagged):,} ({len(flagged)/total*100:.1f}%)" if total else "  FLAGGED: 0",
    ]
    if category_counts:
        summary_lines.append("")
        summary_lines.append("  Breakdown by category:")
        for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
            summary_lines.append(f"    {cat:<30} {count:,}")
    summary_lines.append("=" * 60)
    print("\n".join(summary_lines))

    return 0


if __name__ == "__main__":
    exit(main())
