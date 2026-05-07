#!/usr/bin/env python3
"""
Analyze recording durations from asciinema recordings directory.

This script reads info.json files from the recordings directory and extracts
duration information. Output is a dictionary with recording_id as key and
duration (in seconds) as value. None indicates missing or invalid duration.
"""

import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)

# Maximum reasonable duration: 24 hours (anything longer is likely bad data)
MAX_REASONABLE_DURATION = 24 * 3600  # 86400 seconds


def parse_duration(duration_str: str) -> float | None:
    """
    Parse duration string to seconds.
    
    Formats:
    - "0:11" -> 11 seconds
    - "7:49" -> 7 minutes 49 seconds = 469 seconds
    - "1:29:25" -> 1 hour 29 minutes 25 seconds = 5365 seconds
    
    Returns None for invalid/unreasonable values.
    """
    if not duration_str:
        return None
    
    parts = duration_str.split(":")
    try:
        if len(parts) == 2:
            # mm:ss
            minutes, seconds = int(parts[0]), int(parts[1])
            total = minutes * 60 + seconds
        elif len(parts) == 3:
            # hh:mm:ss
            hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
            total = hours * 3600 + minutes * 60 + seconds
        else:
            return None
        
        # Filter unreasonable values
        if total <= 0 or total >= MAX_REASONABLE_DURATION:
            return None
        
        return float(total)
    except ValueError:
        return None


def format_duration(seconds: float) -> str:
    """Format seconds to human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f}m"
    else:
        hours = seconds / 3600
        return f"{hours:.1f}h"


def get_duration(recording_dir: Path) -> tuple[str, float | None]:
    """
    Get duration from a recording's info.json file.
    
    Returns:
        Tuple of (recording_id, duration_in_seconds) where duration is None if:
        - info.json doesn't exist or can't be read
        - duration field is missing
        - duration value is invalid or unreasonable (> 24h)
    """
    recording_id = recording_dir.name
    info_path = recording_dir / "info.json"
    
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        duration_str = data.get("duration", "")
        duration = parse_duration(duration_str)
        logger.debug(f"{recording_id}: {duration}s")
        return recording_id, duration
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"{recording_id}: failed to read info.json - {e}")
        return recording_id, None


def save_plot(durations: list[float], output_path: Path):
    """Save duration distribution plot using matplotlib."""
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        logger.warning("matplotlib not installed. Skipping plot generation.")
        logger.info("Install with: pip install matplotlib numpy")
        return
    
    # Create figure with subplots
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10))
    fig.suptitle("Recording Duration Distribution", fontsize=14, fontweight="bold")
    
    # 1. Histogram with log scale x-axis
    positive_durations = [d for d in durations if d > 0]
    if positive_durations:
        bins = np.logspace(np.log10(max(1, min(positive_durations))), np.log10(max(positive_durations) + 1), 50)
        ax1.hist(positive_durations, bins=bins, edgecolor="black", alpha=0.7, color="steelblue")
        ax1.set_xscale("log")
    ax1.set_xlabel("Duration (seconds, log scale)")
    ax1.set_ylabel("Count")
    ax1.set_title("Duration Distribution (Log Scale)")
    ax1.grid(True, alpha=0.3)
    
    # Add vertical lines for common thresholds
    for threshold, color, label in [
        (30, "red", "30s"),
        (60, "orange", "1m"),
        (300, "green", "5m"),
        (600, "purple", "10m"),
    ]:
        ax1.axvline(x=threshold, color=color, linestyle="--", label=label)
    ax1.legend()
    
    # 2. Histogram with predefined bins
    bin_edges = [0, 10, 30, 60, 120, 300, 600, 1800, 3600, 7200, 14400, MAX_REASONABLE_DURATION]
    bin_labels = ["0-10s", "10-30s", "30s-1m", "1-2m", "2-5m", "5-10m", "10-30m", "30m-1h", "1-2h", "2-4h", "4-24h"]
    
    counts = []
    for i in range(len(bin_edges) - 1):
        count = sum(1 for d in durations if bin_edges[i] <= d < bin_edges[i + 1])
        counts.append(count)
    
    bars = ax2.bar(range(len(bin_labels)), counts, color="steelblue", edgecolor="black", alpha=0.7)
    ax2.set_xticks(range(len(bin_labels)))
    ax2.set_xticklabels(bin_labels, rotation=45, ha="right")
    ax2.set_xlabel("Duration Range")
    ax2.set_ylabel("Count")
    ax2.set_title("Duration Distribution (Binned)")
    ax2.grid(True, alpha=0.3, axis="y")
    
    # Add count labels on bars
    for bar, count in zip(bars, counts):
        if count > 0:
            ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f'{count:,}',
                    ha='center', va='bottom', fontsize=8)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    logger.info(f"Plot saved to: {output_path}")
    plt.close()


def main():
    parser = argparse.ArgumentParser(
        description="Extract recording durations from asciinema recordings directory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Output format:
    JSON dictionary with recording_id as key and duration (seconds) as value.
    None indicates missing or invalid duration data.

Examples:
    python analyze_duration.py
    python analyze_duration.py --plot
    python analyze_duration.py --workers 16 --verbose
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
        default=Path(__file__).parent / "output" / "duration_analysis.json",
        help="Output file path for duration results (default: output/duration_analysis.json)",
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Generate duration distribution plot",
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
    
    logger.info(f"Starting duration analysis with {args.workers} workers")
    
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
    results: dict[str, float | None] = {}  # recording_id -> duration
    total_dirs = len(recording_dirs)
    
    logger.info(f"Extracting durations with {args.workers} workers...")
    
    processed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(get_duration, d): d for d in recording_dirs}
        
        for future in as_completed(futures):
            recording_id, duration = future.result()
            results[recording_id] = duration
            processed += 1
            
            if processed % 1000 == 0:
                logger.info(f"Processed {processed:,}/{total_dirs:,}...")
    
    logger.info(f"Processed {processed:,} recordings")
    
    # Write output as sorted dictionary
    sorted_results = dict(sorted(results.items()))
    
    with open(args.output_file, 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    logger.info(f"Duration results written to: {args.output_file}")
    
    # Write per-recording analysis.json (merge with existing data)
    logger.info("Writing per-recording analysis.json files...")
    per_recording_written = 0
    for recording_id, duration in results.items():
        analysis_path = args.recordings_dir / recording_id / "analysis.json"
        existing = {}
        if analysis_path.exists():
            try:
                with open(analysis_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError):
                existing = {}
        existing["duration"] = duration
        with open(analysis_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        per_recording_written += 1
    logger.info(f"Per-recording analysis.json written for {per_recording_written:,} recordings")
    
    # Generate plot if requested
    valid_durations = [d for d in results.values() if d is not None]
    if args.plot and valid_durations:
        plot_path = args.output_file.parent / "duration_distribution.png"
        save_plot(valid_durations, plot_path)
    
    # Print summary
    total = len(results)
    valid = len(valid_durations)
    invalid = total - valid
    
    if total > 0 and valid_durations:
        # Basic statistics
        sorted_durations = sorted(valid_durations)
        min_duration = min(valid_durations)
        max_duration = max(valid_durations)
        avg_duration = sum(valid_durations) / len(valid_durations)
        median_duration = sorted_durations[len(sorted_durations) // 2]
        
        # Percentiles
        p25 = sorted_durations[int(len(sorted_durations) * 0.25)]
        p50 = sorted_durations[int(len(sorted_durations) * 0.50)]
        p75 = sorted_durations[int(len(sorted_durations) * 0.75)]
        
        # Duration distribution by ranges
        ranges = [
            (0, 30, "< 30s"),
            (30, 60, "30s - 1m"),
            (60, 300, "1m - 5m"),
            (300, 600, "5m - 10m"),
            (600, 1800, "10m - 30m"),
            (1800, 3600, "30m - 1h"),
            (3600, float('inf'), "> 1h"),
        ]
        
        range_counts = []
        for low, high, label in ranges:
            count = sum(1 for d in valid_durations if low <= d < high)
            pct = count / valid * 100 if valid > 0 else 0
            range_counts.append((label, count, pct))
        
        summary = f"""
============================================================
Duration Analysis Summary
============================================================
Total Recordings:        {total:,}
  Valid durations:       {valid:,} ({valid/total*100:.1f}%)
  Invalid/missing:       {invalid:,} ({invalid/total*100:.1f}%)

Duration Statistics (valid recordings only):
  Min:                   {format_duration(min_duration)} ({min_duration:.1f}s)
  Max:                   {format_duration(max_duration)} ({max_duration:.1f}s)
  Mean:                  {format_duration(avg_duration)} ({avg_duration:.1f}s)
  Median:                {format_duration(median_duration)} ({median_duration:.1f}s)
  
  Percentiles:
    25th (p25):          {format_duration(p25)} ({p25:.1f}s)
    50th (p50):          {format_duration(p50)} ({p50:.1f}s)
    75th (p75):          {format_duration(p75)} ({p75:.1f}s)

Duration Distribution:
"""
        for label, count, pct in range_counts:
            bar_length = int(pct / 2) if pct > 0 else 0  # Scale to max 50 chars
            bar = "█" * bar_length
            summary += f"  {label:<12} {bar:<50} {count:>6,} ({pct:>5.1f}%)\n"
        
        summary += "============================================================"
        
        print(summary)
        logger.info(f"Summary: Total={total:,}, Valid={valid:,}, Invalid={invalid:,}, "
                   f"Mean={avg_duration:.1f}s, Median={median_duration:.1f}s")
    else:
        logger.warning("No valid durations found to analyze")
    
    logger.info("Duration analysis complete!")
    
    return 0


if __name__ == "__main__":
    exit(main())
