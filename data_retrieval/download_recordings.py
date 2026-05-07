"""
Download asciinema recordings based on the JSONL index.

Supports three modes:
- incremental: Only download new recordings (skip existing)
- repair: Check and fix existing recordings (re-download missing fields/files)
- refresh: Force re-download descriptions and update all fields for all recordings

In repair/refresh mode, also scans recordings directory to find recordings
not in index and process them as well.

Supports multi-threaded downloading for faster processing.

Usage:
    python download_recordings.py --mode incremental  # Default, only new
    python download_recordings.py --mode repair       # Fix existing (index + directory)
    python download_recordings.py --mode repair --index-only  # Fix only index entries
    python download_recordings.py --mode refresh      # Force update all fields
    python download_recordings.py --workers 8         # Use 8 parallel workers
"""

import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Support both direct execution and package import
try:
    from .config import CrawlerConfig
    from .parsers import CastDescriptionParser
except ImportError:
    from config import CrawlerConfig
    from parsers import CastDescriptionParser

logger = logging.getLogger(__name__)

# Required fields that should be present in info.json
# These include both index fields (from explore page) and description fields (from detail page)
REQUIRED_FIELDS = [
    # Fields from index (explore page)
    "href", "duration", "author", "submit_time",
    # Fields from description page
    "title", "asciicast_version",
    "environment", "terminal_cols", "terminal_rows"
]

# Fields that come from the index file (explore page scraping)
INDEX_FIELDS = ["href", "duration", "author", "submit_time"]


class RecordingDownloader:
    """
    Downloads individual recordings based on the JSONL index.
    
    Supports three modes:
    - incremental: Only download new recordings (default)
    - repair: Check existing recordings and fix missing fields/files
    - refresh: Force re-download descriptions and update all fields
    
    Supports multi-threaded downloading with configurable workers.
    """

    def __init__(self, config: CrawlerConfig, max_workers: int = 4):
        self.config = config
        self.max_workers = max_workers
        self.parser = CastDescriptionParser()
        
        # Thread-local storage for sessions (each thread gets its own session)
        self._local = threading.local()
        
        # Thread-safe counters
        self._lock = threading.Lock()
        self._success_count = 0
        self._fail_count = 0
        self._skip_count = 0

    def _get_session(self) -> requests.Session:
        """
        Get thread-local HTTP session with automatic retry.
        
        Each thread gets its own session instance to avoid conflicts.
        Clears cookies on each call to prevent 431 "Request Header Fields Too Large" errors.
        """
        if not hasattr(self._local, 'session'):
            session = requests.Session()
            
            retry_strategy = Retry(
                total=self.config.retry_count,
                backoff_factor=self.config.retry_backoff_factor,
                status_forcelist=[429, 500, 502, 503, 504],
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            
            session.headers.update({"User-Agent": self.config.user_agent})
            self._local.session = session
        
        # Clear cookies to prevent 431 errors from cookie accumulation
        self._local.session.cookies.clear()
        
        return self._local.session

    def _random_delay(self):
        """Sleep for a random duration."""
        delay = random.uniform(*self.config.delay_between_requests)
        time.sleep(delay)

    def _load_index(self) -> list[dict]:
        """Load all entries from the JSONL index file."""
        entries = []
        
        if not self.config.index_file.exists():
            logger.error(f"❌ Index file not found: {self.config.index_file}")
            logger.error("Run scrape_pages.py first to build the index.")
            return entries
        
        with open(self.config.index_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict) and data.get("href"):
                        entries.append(data)
                except json.JSONDecodeError:
                    logger.warning(f"Skipping invalid JSON: {line[:50]}...")
        return entries

    def _scan_recordings_dir(self) -> list[str]:
        """Scan recordings directory and return list of recording IDs."""
        recording_ids = []
        
        if not self.config.output_dir.exists():
            logger.warning(f"⚠️  Recordings directory not found: {self.config.output_dir}")
            return recording_ids
        
        for item in self.config.output_dir.iterdir():
            if item.is_dir() and item.name.isdigit():
                recording_ids.append(item.name)
        
        return recording_ids

    def _create_entry_from_id(self, record_id: str) -> dict:
        """Create a minimal entry dict from a recording ID."""
        return {"href": f"/a/{record_id}"}

    def _get_recording_paths(self, record_id: str) -> tuple[Path, Path]:
        """Get directory and info.json path for a recording."""
        record_dir = self.config.output_dir / record_id
        info_path = record_dir / "info.json"
        return record_dir, info_path

    def _download_description(self, href: str) -> dict:
        """Download and parse a recording's description page."""
        url = f"{self.config.base_url}{href}"
        logger.debug(f"Fetching description: {url}")
        
        response = self._get_session().get(url, timeout=self.config.request_timeout)
        response.raise_for_status()
        
        return self.parser.parse(response.text).to_dict()

    def _download_txt_file(self, href: str, download_urls: dict = None) -> bytes:
        """
        Download a recording's plain text version.
        
        Uses parsed download_urls if available, otherwise constructs URL manually.
        """
        # Prefer parsed URL from info.json
        if download_urls and download_urls.get("txt"):
            txt_url = download_urls["txt"]
            # Handle relative URLs
            if txt_url.startswith("/"):
                url = f"{self.config.base_url}{txt_url}"
            else:
                url = txt_url
        else:
            # Fallback: construct URL manually
            url = f"{self.config.base_url}{href}.txt"
        
        logger.debug(f"Fetching txt: {url}")
        response = self._get_session().get(url, timeout=self.config.request_timeout)
        response.raise_for_status()
        
        return response.content

    def _check_info_fields(self, info_data: dict) -> list[str]:
        """Check which required fields are missing in info.json."""
        missing = []
        for field in REQUIRED_FIELDS:
            value = info_data.get(field)
            if not self._is_valid_value(value):
                missing.append(field)
        return missing

    def _is_valid_value(self, value) -> bool:
        """Check if a value is valid (not a placeholder or None)."""
        if value is None:
            return False
        if isinstance(value, str):
            return value not in ("", "unknown", "No title found", "No description found")
        return True

    def download_single(self, entry: dict, mode: str = "incremental") -> bool:
        """
        Download a single recording.
        
        Args:
            entry: Recording metadata from index
            mode: "incremental" (skip existing), "repair" (check and fix), or "refresh" (force update all)
            
        Returns:
            True on success, False on error
        """
        href = entry.get("href")
        if not href:
            logger.warning("Entry missing 'href', skipping")
            return False

        record_id = href.split("/")[-1]
        record_dir, info_path = self._get_recording_paths(record_id)
        record_dir.mkdir(parents=True, exist_ok=True)

        # Determine if we need to download/update info.json
        need_info_update = False
        info_data = None
        
        if not info_path.exists():
            need_info_update = True
            logger.info(f"📥 [NEW] {record_id}: info.json not found")
        elif mode == "refresh":
            # Force update all fields
            need_info_update = True
            logger.info(f"🔄 [REFRESH] {record_id}: Force updating all fields")
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    info_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass  # Will be overwritten anyway
        elif mode == "repair":
            # Check if existing info.json has missing fields
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    info_data = json.load(f)
                missing_fields = self._check_info_fields(info_data)
                if missing_fields:
                    need_info_update = True
                    logger.info(f"🔧 [REPAIR] {record_id}: Missing fields: {', '.join(missing_fields)}")
            except (json.JSONDecodeError, IOError) as e:
                need_info_update = True
                logger.warning(f"⚠️  [REPAIR] {record_id}: Corrupted info.json: {e}")

        # Download/update info.json if needed
        if need_info_update:
            try:
                logger.info(f"Downloading description: {record_id}")
                desc_data = self._download_description(href)
                
                # Start with description data as base
                full_data = desc_data.copy()
                
                # Check if entry has meaningful data (more than just href)
                entry_has_data = len(entry) > 1
                
                if entry_has_data:
                    # Merge entry fields into full_data
                    # INDEX_FIELDS (href, duration, author, submit_time) always override desc_data
                    # Other fields only added if not already in desc_data
                    for key, value in entry.items():
                        if self._is_valid_value(value):
                            if key in INDEX_FIELDS or key not in full_data:
                                full_data[key] = value
                elif info_data:
                    # If entry is minimal (not in index), try to preserve index fields from old info.json
                    for field in INDEX_FIELDS:
                        if field in info_data and self._is_valid_value(info_data[field]):
                            full_data[field] = info_data[field]
                
                # Ensure href is always set
                if not full_data.get("href"):
                    full_data["href"] = href
                
                with open(info_path, "w", encoding="utf-8") as f:
                    json.dump(full_data, f, indent=2, ensure_ascii=False)
                logger.info(f"✅ Saved: {info_path}")
                info_data = full_data
                self._random_delay()
                
            except requests.RequestException as e:
                logger.error(f"❌ Failed description for {record_id}: {e}")
                return False

        # Load info if not already loaded
        if info_data is None:
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    info_data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"❌ Failed to read {info_path}: {e}")
                return False

        # Get download URLs from info.json (may be empty for older entries)
        download_urls = info_data.get("download_urls", {})

        # Download txt file if needed
        txt_path = record_dir / "recording.txt"
        if not txt_path.exists():
            try:
                logger.info(f"📥 Downloading txt: {record_id}")
                txt_data = self._download_txt_file(href, download_urls)
                
                with open(txt_path, "wb") as f:
                    f.write(txt_data)
                logger.info(f"✅ Saved: {txt_path}")
                self._random_delay()
                
            except requests.RequestException as e:
                logger.warning(f"⚠️  Failed txt for {record_id}: {e}")
                # txt download failure is not critical
        elif mode == "repair":
            logger.debug(f"✓ Exists: {txt_path}")

        return True

    def _should_skip(self, entry: dict, mode: str) -> bool:
        """
        Check if a recording should be skipped.
        
        Returns:
            True if should skip, False if needs processing
        """
        # In refresh mode: never skip, always re-download description
        if mode == "refresh":
            return False
        
        href = entry.get("href", "unknown")
        record_id = href.split("/")[-1] if href != "unknown" else "unknown"
        record_dir, info_path = self._get_recording_paths(record_id)
        
        if not info_path.exists():
            return False
        
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info_data = json.load(f)
            txt_path = record_dir / "recording.txt"

            # In incremental mode: skip if txt transcript already exists
            if mode == "incremental":
                if txt_path.exists():
                    return True

            # In repair mode: skip only if txt exists AND all metadata fields are complete
            elif mode == "repair":
                missing_fields = self._check_info_fields(info_data)
                if txt_path.exists() and not missing_fields:
                    return True

        except (json.JSONDecodeError, IOError, KeyError):
            pass  # Will be handled by download_single
        
        return False

    def _process_entry(self, entry: dict, mode: str, index: int, total: int) -> str:
        """
        Process a single entry (for use in ThreadPoolExecutor).
        
        Returns:
            "success", "fail", or "skip"
        """
        href = entry.get("href", "unknown")
        record_id = href.split("/")[-1] if href != "unknown" else "unknown"
        
        # Check if should skip
        if self._should_skip(entry, mode):
            logger.debug(f"⏭️  [SKIP] {record_id}")
            return "skip"
        
        logger.info(f"[{index}/{total}] Processing: {href}")
        
        try:
            if self.download_single(entry, mode):
                return "success"
            else:
                return "fail"
        except Exception as e:
            logger.error(f"❌ Error processing {href}: {e}")
            return "fail"

    def download_all(self, mode: str = "incremental", scan_dir: bool = True) -> tuple[int, int, int]:
        """
        Download all recordings from the index using parallel workers.
        
        In repair/refresh mode with scan_dir=True, also scans recordings directory
        to find recordings not in index.
        
        Args:
            mode: "incremental", "repair", or "refresh"
            scan_dir: Whether to also scan recordings directory (only for repair/refresh)
            
        Returns:
            Tuple of (success_count, fail_count, skip_count)
        """
        entries = self._load_index()
        index_count = len(entries)
        
        # In repair/refresh mode, also scan recordings directory
        if mode in ("repair", "refresh") and scan_dir:
            # Build set of IDs from index entries
            index_ids = set()
            for entry in entries:
                href = entry.get("href", "")
                record_id = href.split("/")[-1] if href else ""
                if record_id:
                    index_ids.add(record_id)
            
            # Scan directory for additional recordings
            dir_ids = self._scan_recordings_dir()
            extra_ids = [rid for rid in dir_ids if rid not in index_ids]
            
            if extra_ids:
                logger.info(f"📂 Found {len(extra_ids)} recordings in directory not in index")
                # Create entries for recordings not in index
                for record_id in extra_ids:
                    entries.append(self._create_entry_from_id(record_id))
        
        total = len(entries)
        
        if total == 0:
            logger.warning("⚠️  No entries found in index file")
            return 0, 0, 0
        
        if mode in ("repair", "refresh") and scan_dir:
            logger.info(f"🚀 Starting download ({mode} mode): {total} recordings "
                       f"({index_count} from index, {total - index_count} from directory), "
                       f"{self.max_workers} workers")
        else:
            logger.info(f"🚀 Starting download ({mode} mode): {total} recordings, {self.max_workers} workers")
        
        # Reset counters
        self._success_count = 0
        self._fail_count = 0
        self._skip_count = 0
        
        # Use single-threaded mode if workers = 1
        if self.max_workers <= 1:
            for i, entry in enumerate(entries, 1):
                result = self._process_entry(entry, mode, i, total)
                if result == "success":
                    self._success_count += 1
                elif result == "fail":
                    self._fail_count += 1
                else:
                    self._skip_count += 1
        else:
            # Multi-threaded mode
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self._process_entry, entry, mode, i, total): entry
                    for i, entry in enumerate(entries, 1)
                }
                
                try:
                    processed = 0
                    for future in as_completed(futures):
                        result = future.result()
                        
                        with self._lock:
                            if result == "success":
                                self._success_count += 1
                            elif result == "fail":
                                self._fail_count += 1
                            else:
                                self._skip_count += 1
                            
                            processed += 1
                            if processed % 100 == 0:
                                logger.info(
                                    f"📊 Progress: {processed}/{total} "
                                    f"(✅ {self._success_count}, ❌ {self._fail_count}, ⏭️  {self._skip_count})"
                                )
                                
                except KeyboardInterrupt:
                    logger.warning("⚠️  Interrupted by user, shutting down...")
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise

        logger.info(
            f"🏁 Complete. Success: {self._success_count}, "
            f"Failed: {self._fail_count}, Skipped: {self._skip_count}"
        )
        return self._success_count, self._fail_count, self._skip_count


def setup_logging(verbose: bool = False, log_file: str = None):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)
    
    if log_file:
        file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        logging.info(f"📝 Logging to file: {log_file}")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Download asciinema recordings from index",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  incremental  Only download new recordings (skip existing) [default]
  repair       Check existing recordings and fix missing fields/files
  refresh      Force re-download descriptions and update all fields

Examples:
  python download_recordings.py --mode incremental
  python download_recordings.py --mode repair --log-file repair.log
  python download_recordings.py --mode repair --index-only  # Only repair index entries
  python download_recordings.py --mode refresh  # Force update all info.json
  python download_recordings.py --workers 8  # Use 8 parallel workers
        """,
    )
    parser.add_argument("--index-file", type=str, default="../data/asciinema_public_explore_pages.jsonl")
    parser.add_argument("--output-dir", type=str, default="../data/recordings")
    parser.add_argument("--mode", type=str, choices=["incremental", "repair", "refresh"], default="incremental",
                        help="Download mode: incremental (new only), repair (fix missing), or refresh (update all)")
    parser.add_argument("--index-only", action="store_true",
                        help="Only process recordings from index (skip directory scan in repair/refresh mode)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of parallel workers (default: 4, use 1 for sequential)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--log-file", type=str, default=None, help="Log file path (optional)")

    args = parser.parse_args()
    setup_logging(args.verbose, args.log_file)

    config = CrawlerConfig(
        index_file=Path(args.index_file),
        output_dir=Path(args.output_dir),
    )
    
    downloader = RecordingDownloader(config, max_workers=args.workers)
    downloader.download_all(mode=args.mode, scan_dir=not args.index_only)


if __name__ == "__main__":
    main()
