#!/usr/bin/env python3
"""
Detects whether an asciinema recording contains TUI (Text User Interface) content
by scanning the plain-text transcript (recording.txt) for known TUI tool invocations.

Since the .txt format has ANSI escape codes stripped, we detect TUI usage by
matching known full-screen / ncurses applications invoked as shell commands
(after a prompt character or at the start of a line).
"""

import argparse
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)


# =============================================================================
# TUI tool invocation patterns
# =============================================================================

# Full-screen / ncurses applications whose invocation in a shell command is a
# reliable signal that the session contains TUI content.
_TUI_TOOLS = [
    # Text editors
    "vim", "vi", "nvim", "nano", "emacs", "pico", "joe", "mcedit", "micro",
    # Pagers (interactive mode — invoked directly, not as a pipeline destination)
    "less", "more", "most",
    # File managers
    "mc", "ranger", "nnn", "vifm", "lf", "broot",
    # System / resource monitors
    "htop", "top", "btop", "glances", "iotop", "iftop", "nethogs", "bpytop",
    # Multiplexers
    "tmux", "screen", "byobu", "zellij",
    # Interactive REPLs / DB clients that use full-screen UI
    "ipython", "pgcli", "mycli", "litecli", "ncdu",
    # Misc ncurses tools
    "cmus", "mutt", "neomutt", "irssi", "weechat", "tui-rs",
]

# Match a TUI tool invoked as a command:
#   - after a shell prompt terminator ($, #, %, >)  e.g. "$ vim file"
#   - at the very start of a line                   e.g. "vim file" in a script
# The tool name must be followed by whitespace or end-of-line (not mid-word).
_RE_TUI_INVOCATION = re.compile(
    r'(?:(?:^|[$#%>]\s+))(' + '|'.join(re.escape(t) for t in _TUI_TOOLS) + r')(?:\s|$)',
    re.MULTILINE,
)


# =============================================================================
# Classification labels
# =============================================================================

LABEL_CLI_ONLY     = "CLI_ONLY"
LABEL_CONTAINS_TUI = "CONTAINS_TUI"
LABEL_NO_DATA      = "NO_DATA"


# =============================================================================
# Detection functions
# =============================================================================

def scan_txt_for_tui(txt_path: Path) -> bool:
    """
    Scan a plain-text transcript for known TUI tool invocations.

    Looks for TUI tool names invoked as shell commands — after a prompt
    character ($, #, %, >) or at the start of a line — followed by
    whitespace or end-of-line to avoid matching mid-word occurrences.

    Returns True if any TUI tool invocation is detected.
    """
    try:
        text = txt_path.read_text(encoding="utf-8", errors="replace")
        return bool(_RE_TUI_INVOCATION.search(text))
    except OSError:
        return False


def classify_recording(recording_dir: Path) -> tuple[str, str]:
    """
    Classify a single recording directory for TUI content.

    Reads recording.txt and applies keyword-based TUI tool detection.

    Returns:
        (recording_id, label) where label is one of:
        - LABEL_CLI_ONLY:     no TUI tool invocations detected
        - LABEL_CONTAINS_TUI: at least one TUI tool invocation found
        - LABEL_NO_DATA:      recording.txt not found
    """
    recording_id = recording_dir.name
    txt_path = recording_dir / "recording.txt"

    if not txt_path.exists():
        logger.warning(f"{recording_id}: recording.txt not found")
        return recording_id, LABEL_NO_DATA

    has_tui = scan_txt_for_tui(txt_path)
    label = LABEL_CONTAINS_TUI if has_tui else LABEL_CLI_ONLY
    logger.debug(f"{recording_id}: {label}")
    return recording_id, label


def main():
    parser = argparse.ArgumentParser(
        description="Classify asciinema recordings for TUI content using keyword matching",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Detection method:
    Scans recording.txt for known TUI tool invocations (vim, nano, htop,
    tmux, ranger, etc.) appearing as shell commands after a prompt character
    or at the start of a line.

Examples:
    python classify_tui.py
    python classify_tui.py --workers 16
        """
    )
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "recordings",
        help="Path to recordings directory",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=Path(__file__).parent / "output" / "tui_classification.json",
        help="Output file path for classification results (default: output/tui_classification.json)",
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
        help="Log file path (optional, defaults to console only)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output (sets log level to DEBUG)",
    )
    
    args = parser.parse_args()
    
    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    
    handlers = [logging.StreamHandler()]
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file))
    
    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=handlers,
        force=True,
    )
    
    logger.info(f"Starting TUI classification with {args.workers} workers")
    
    # Validate recordings directory
    if not args.recordings_dir.exists():
        logger.error(f"Recordings directory not found: {args.recordings_dir}")
        return 1
    
    # Create output directory if needed
    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Find recording directories (directories containing info.json)
    logger.info(f"Scanning recordings in: {args.recordings_dir}")
    recording_dirs = sorted(
        [p.parent for p in args.recordings_dir.glob("*/info.json")],
        key=lambda p: p.name
    )
    logger.info(f"Found {len(recording_dirs):,} recordings")
    
    # Process recordings with parallel workers
    results: dict[str, str] = {}  # recording_id -> label
    total_dirs = len(recording_dirs)
    
    logger.info(f"Classifying recordings with {args.workers} workers...")
    
    processed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(classify_recording, d): d for d in recording_dirs}
        
        for future in as_completed(futures):
            recording_id, label = future.result()
            results[recording_id] = label
            processed += 1
            
            if processed % 100 == 0:
                logger.info(f"Processed {processed:,}/{total_dirs:,}...")
    
    logger.info(f"Processed {processed:,} recordings")
    
    # Write output as sorted dictionary
    sorted_results = dict(sorted(results.items()))
    
    with open(args.output_file, 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    logger.info(f"Classification results written to: {args.output_file}")
    
    # Write per-recording analysis.json (merge with existing data)
    logger.info("Writing per-recording analysis.json files...")
    per_recording_written = 0
    for recording_id, label in results.items():
        analysis_path = args.recordings_dir / recording_id / "analysis.json"
        existing = {}
        if analysis_path.exists():
            try:
                with open(analysis_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError):
                existing = {}
        existing["tui_classification"] = label
        with open(analysis_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        per_recording_written += 1
    logger.info(f"Per-recording analysis.json written for {per_recording_written:,} recordings")
    
    # Print summary
    total = len(results)
    cli_only = sum(1 for label in results.values() if label == LABEL_CLI_ONLY)
    contains_tui = sum(1 for label in results.values() if label == LABEL_CONTAINS_TUI)
    no_data = sum(1 for label in results.values() if label == LABEL_NO_DATA)
    
    if total > 0:
        summary = f"""
============================================================
Classification Summary
============================================================
  Total recordings:      {total:,}
  CLI_ONLY:              {cli_only:,} ({cli_only/total*100:.1f}%)
  CONTAINS_TUI:          {contains_tui:,} ({contains_tui/total*100:.1f}%)
  NO_DATA:               {no_data:,} ({no_data/total*100:.1f}%)
"""
        print(summary)
        logger.info(f"Summary: Total={total:,}, CLI_ONLY={cli_only:,}, CONTAINS_TUI={contains_tui:,}, NO_DATA={no_data:,}")
    else:
        logger.warning("No recordings found to classify")
    
    if no_data > 0:
        logger.warning(f"{no_data:,} recordings have no valid recording file")
    
    logger.info("Classification complete!")
    
    return 0


if __name__ == "__main__":
    exit(main())
