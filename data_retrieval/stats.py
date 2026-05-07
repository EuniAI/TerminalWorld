"""
Statistics tool for analyzing crawled asciinema recordings.

Provides detailed statistics about:
- All recordings in the directory
- File completeness (info.json, recording.txt)
- Field completeness in info.json
- Distribution by asciicast version, environment, etc.

Usage:
    python stats.py                     # Full statistics (scan directory)
    python stats.py --use-index         # Use index file instead
    python stats.py --missing           # Show only incomplete recordings
    python stats.py --missing --limit 20  # Limit output
    python stats.py --json              # Output as JSON
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

# Support both direct execution and package import
try:
    from .config import CrawlerConfig
except ImportError:
    from config import CrawlerConfig


# Required fields that should be present in info.json
REQUIRED_FIELDS = [
    "href", "duration", "author", "submit_time",
    "title", "asciicast_version",
    "environment", "terminal_cols", "terminal_rows"
]


class RecordingStats:
    """Analyze crawled recordings and generate statistics."""

    def __init__(self, config: CrawlerConfig, use_index: bool = False):
        self.config = config
        self.use_index = use_index
        self.index_entries: list[dict] = []
        self.recordings: dict[str, dict] = {}  # record_id -> analysis result
        
    def load_index(self) -> int:
        """Load index file and return entry count."""
        if not self.config.index_file.exists():
            print(f"❌ Index file not found: {self.config.index_file}")
            return 0
        
        with open(self.config.index_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict) and data.get("href"):
                        self.index_entries.append(data)
                except json.JSONDecodeError:
                    pass
        
        return len(self.index_entries)

    def analyze_recording(self, record_id: str) -> dict:
        """Analyze a single recording directory."""
        record_dir = self.config.output_dir / record_id
        info_path = record_dir / "info.json"
        
        result = {
            "id": record_id,
            "exists": record_dir.exists(),
            "has_info": False,
            "has_txt": False,
            "missing_fields": [],
            "info_data": None,
        }

        if not record_dir.exists():
            return result

        # Check info.json
        if info_path.exists():
            result["has_info"] = True
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    info_data = json.load(f)
                result["info_data"] = info_data

                # Check required fields
                for field in REQUIRED_FIELDS:
                    value = info_data.get(field)
                    if value is None or value == "unknown" or value == "No title found":
                        result["missing_fields"].append(field)

            except (json.JSONDecodeError, IOError):
                result["missing_fields"] = ["corrupted_info"]

        # Check txt transcript
        txt_path = record_dir / "recording.txt"
        result["has_txt"] = txt_path.exists()

        return result

    def scan_directory(self) -> int:
        """Scan recordings directory and return count of directories found."""
        if not self.config.output_dir.exists():
            print(f"❌ Recordings directory not found: {self.config.output_dir}")
            return 0
        
        count = 0
        for item in self.config.output_dir.iterdir():
            if item.is_dir() and item.name.isdigit():
                count += 1
                self.recordings[item.name] = self.analyze_recording(item.name)
        
        return count
    
    def analyze_all(self):
        """Analyze all recordings from index."""
        for entry in self.index_entries:
            href = entry.get("href", "")
            record_id = href.split("/")[-1] if href else ""
            if record_id:
                self.recordings[record_id] = self.analyze_recording(record_id)

    def get_summary(self) -> dict:
        """Generate summary statistics."""
        total_recordings = len(self.recordings)
        total_exists = sum(1 for r in self.recordings.values() if r["exists"])
        
        # File completeness
        has_info = sum(1 for r in self.recordings.values() if r["has_info"])
        has_txt = sum(1 for r in self.recordings.values() if r["has_txt"])

        # Fully complete (info + txt transcript, no missing metadata fields)
        complete = sum(
            1 for r in self.recordings.values()
            if r["has_info"] and r["has_txt"] and not r["missing_fields"]
        )

        # Incomplete reasons distribution
        incomplete_reasons = Counter()
        missing_field_counts = Counter()
        for r in self.recordings.values():
            if r["exists"]:
                if not r["has_info"]:
                    incomplete_reasons["missing_info.json"] += 1
                if not r["has_txt"]:
                    incomplete_reasons["missing_txt"] += 1
                for field in r["missing_fields"]:
                    incomplete_reasons[f"missing_field:{field}"] += 1
                    missing_field_counts[field] += 1
        
        # Version distribution
        version_counts = Counter()
        environment_counts = Counter()
        
        for r in self.recordings.values():
            if r["info_data"]:
                version = r["info_data"].get("asciicast_version", "unknown")
                version_counts[version] += 1
                
                env = r["info_data"].get("environment")
                if env:
                    environment_counts[env] += 1
        
        result = {
            "total_recordings": total_recordings,
            "files": {
                "has_info": has_info,
                "has_txt": has_txt,
            },
            "complete_recordings": complete,
            "incomplete_recordings": total_exists - complete,
            "incomplete_reasons": dict(incomplete_reasons.most_common()),
            "missing_field_distribution": dict(missing_field_counts.most_common()),
            "version_distribution": dict(version_counts.most_common()),
            "top_environments": dict(environment_counts.most_common(20)),
        }
        
        # Add index-specific fields if using index
        if self.use_index:
            total_index = len(self.index_entries)
            result["total_index_entries"] = total_index
            result["total_downloaded"] = total_exists
            result["not_downloaded"] = total_index - total_exists
        
        return result

    def get_incomplete_recordings(self) -> list[dict]:
        """Get list of incomplete recordings."""
        incomplete = []
        
        for record_id, analysis in self.recordings.items():
            issues = []
            
            if not analysis["exists"]:
                issues.append("not_downloaded")
            else:
                if not analysis["has_info"]:
                    issues.append("missing_info.json")
                if not analysis["has_txt"]:
                    issues.append("missing_txt")
                if analysis["missing_fields"]:
                    issues.append(f"missing_fields: {', '.join(analysis['missing_fields'])}")
            
            if issues:
                incomplete.append({
                    "id": record_id,
                    "url": f"https://asciinema.org/a/{record_id}",
                    "issues": issues,
                })
        
        return incomplete

    def get_not_downloaded(self) -> list[str]:
        """Get list of record IDs that haven't been downloaded yet."""
        not_downloaded = []
        for record_id, analysis in self.recordings.items():
            if not analysis["exists"]:
                not_downloaded.append(record_id)
        return not_downloaded


def print_summary(stats: RecordingStats):
    """Print formatted summary statistics."""
    summary = stats.get_summary()
    
    print("\n" + "=" * 60)
    print("📊 ASCIINEMA CRAWL STATISTICS")
    print("=" * 60)
    
    # Overall progress
    total = summary["total_recordings"]
    
    if stats.use_index:
        # Index-based mode
        total_index = summary["total_index_entries"]
        downloaded = summary["total_downloaded"]
        not_downloaded = summary["not_downloaded"]
        progress = (downloaded / total_index * 100) if total_index > 0 else 0
        
        print(f"\n📁 Overall Progress (Index-based):")
        print(f"   Total in index:     {total_index:,}")
        print(f"   Downloaded:         {downloaded:,} ({progress:.1f}%)")
        print(f"   Not downloaded:     {not_downloaded:,}")
    else:
        # Directory-based mode
        print(f"\n📁 Overall Statistics (Directory scan):")
        print(f"   Total recordings:   {total:,}")
    
    # File completeness
    files = summary["files"]
    print(f"\n📄 File Completeness (of {total:,} recordings):")
    print(f"   info.json:          {files['has_info']:,}")
    print(f"   recording.txt:      {files['has_txt']:,}")
    
    # Quality
    complete = summary["complete_recordings"]
    incomplete = summary["incomplete_recordings"]
    quality = (complete / total * 100) if total > 0 else 0
    
    print(f"\n✅ Data Quality:")
    print(f"   Complete:           {complete:,} ({quality:.2f}%)")
    print(f"   Incomplete:         {incomplete:,}")
    
    # Incomplete reasons
    incomplete_reasons = summary.get("incomplete_reasons", {})
    if incomplete_reasons:
        print(f"\n⚠️  Incomplete Reasons Distribution:")
        for reason, count in incomplete_reasons.items():
            print(f"   {reason}: {count:,}")
    
    # Version distribution
    versions = summary["version_distribution"]
    if versions:
        print(f"\n📦 Asciicast Version Distribution:")
        for version, count in versions.items():
            pct = (count / total * 100) if total > 0 else 0
            print(f"   {version}: {count:,} ({pct:.1f}%)")
    
    # Top environments
    environments = summary["top_environments"]
    if environments:
        print(f"\n🖥️  Top Environments:")
        for env, count in list(environments.items())[:10]:
            print(f"   {env}: {count:,}")
    
    print("\n" + "=" * 60)


def print_incomplete(stats: RecordingStats, limit: Optional[int] = None):
    """Print list of incomplete recordings."""
    incomplete = stats.get_incomplete_recordings()
    
    if not incomplete:
        print("\n✅ All recordings are complete!")
        return
    
    print(f"\n⚠️  Incomplete Recordings ({len(incomplete):,} total):")
    print("-" * 60)
    
    display = incomplete[:limit] if limit else incomplete
    
    for item in display:
        print(f"\n📍 {item['id']}")
        print(f"   URL: {item['url']}")
        print(f"   Issues: {', '.join(item['issues'])}")
    
    if limit and len(incomplete) > limit:
        print(f"\n... and {len(incomplete) - limit:,} more")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Statistics for crawled asciinema recordings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python stats.py                      # Full statistics (scan directory)
  python stats.py --use-index          # Use index file instead
  python stats.py --missing            # Show incomplete recordings
  python stats.py --missing --limit 20 # Limit output
  python stats.py --not-downloaded     # List recordings not yet downloaded (requires --use-index)
  python stats.py --output-file        # Save to stats.json
  python stats.py --output-file custom.json  # Save to custom file
        """,
    )
    parser.add_argument("--use-index", action="store_true",
                        help="Use index file instead of scanning directory")
    parser.add_argument("--index-file", type=str, default="../data/asciinema_public_explore_pages.jsonl",
                        help="Index file path (used with --use-index)")
    parser.add_argument("--recordings-dir", type=str, default="../data/recordings",
                        help="Recordings directory to scan")
    parser.add_argument("--missing", action="store_true",
                        help="Show incomplete recordings")
    parser.add_argument("--not-downloaded", action="store_true",
                        help="List recordings not yet downloaded (only works with --use-index)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of items to show")
    parser.add_argument("--output-file", type=str, nargs='?', const="stats.json", default=None,
                        help="Output statistics as JSON to file (default: stats.json)")
    
    args = parser.parse_args()
    
    # Validate arguments
    if args.not_downloaded and not args.use_index:
        print("❌ Error: --not-downloaded requires --use-index", file=sys.stderr)
        sys.exit(1)
    
    config = CrawlerConfig(
        index_file=Path(args.index_file),
        output_dir=Path(args.recordings_dir),
    )
    
    stats = RecordingStats(config, use_index=args.use_index)
    
    if args.use_index:
        print("🔍 Loading index...", file=sys.stderr)
        count = stats.load_index()
        print(f"   Found {count:,} entries", file=sys.stderr)
        
        print("🔍 Analyzing recordings...", file=sys.stderr)
        stats.analyze_all()
        print("   Done!", file=sys.stderr)
    else:
        print("🔍 Scanning recordings directory...", file=sys.stderr)
        count = stats.scan_directory()
        print(f"   Found {count:,} recordings", file=sys.stderr)
    
    if args.output_file:
        args.output_file.parent.mkdir(parents=True, exist_ok=True)
        
        # JSON output to file
        output = {
            "summary": stats.get_summary(),
        }
        if args.missing:
            output["incomplete"] = stats.get_incomplete_recordings()
        if args.not_downloaded:
            output["not_downloaded"] = stats.get_not_downloaded()
        
        with open(args.output_file, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"\n✅ Statistics saved to: {args.output_file}", file=sys.stderr)
    else:
        if args.not_downloaded:
            not_downloaded = stats.get_not_downloaded()
            print(f"\n📥 Not Downloaded ({len(not_downloaded):,} total):")
            display = not_downloaded[:args.limit] if args.limit else not_downloaded
            for record_id in display:
                print(f"   {record_id} - https://asciinema.org/a/{record_id}")
            if args.limit and len(not_downloaded) > args.limit:
                print(f"\n... and {len(not_downloaded) - args.limit:,} more")
        elif args.missing:
            print_incomplete(stats, args.limit)
        else:
            print_summary(stats)


if __name__ == "__main__":
    main()

