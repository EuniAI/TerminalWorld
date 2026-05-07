"""
Scrape asciinema.org explore pages to build a recording index.

Scrapes all sources (public, recent, featured, popular) with a shared dedup
set. Uses three end-of-pages signals combined with OR logic so pages are never
missed due to a single signal failing.

Usage:
    python scrape_pages.py                   # scrape all sources, skip existing
    python scrape_pages.py --max-pages 5     # limit to 5 pages per source
"""

import json
import logging
import random
import time
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Support both direct execution and package import
try:
    from .config import CrawlerConfig, EXPLORE_SOURCES
    from .parsers import ExplorePageParser
except ImportError:
    from config import CrawlerConfig, EXPLORE_SOURCES
    from parsers import ExplorePageParser

logger = logging.getLogger(__name__)


class PageScraper:
    """
    Scrapes all asciinema.org explore sources to build a recording index.

    Iterates over every source in EXPLORE_SOURCES sequentially, sharing one
    dedup set so recordings that appear in multiple sources are only written
    once.  Within each source, three signals are combined with OR logic to
    decide whether to continue to the next page (stop only when all agree):
      1. rel="next" link present in pagination nav
      2. active_page_num matches the requested page number
      3. first recording href differs from the previous page's first href
    """

    def __init__(self, config: CrawlerConfig):
        self.config = config
        self.parser = ExplorePageParser()
        self.session = self._create_session()
        self.existing_hrefs: set[str] = set()

    def _create_session(self) -> requests.Session:
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
        return session

    def _build_url(self, url_template: str, page_num: int) -> str:
        return url_template.format(base_url=self.config.base_url, page=page_num)

    def _random_delay(self):
        delay = random.uniform(*self.config.delay_between_requests)
        time.sleep(delay)

    def _load_existing_hrefs(self) -> set[str]:
        hrefs = set()
        if not self.config.index_file.exists():
            logger.info(f"Index file not found, will create: {self.config.index_file}")
            return hrefs
        try:
            with open(self.config.index_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if isinstance(data, dict) and data.get("href"):
                            hrefs.add(data["href"])
                    except json.JSONDecodeError:
                        continue
            logger.info(f"Loaded {len(hrefs)} existing recordings from index")
        except IOError as e:
            logger.warning(f"Failed to load existing index: {e}")
        return hrefs

    def _has_more_pages(
        self,
        result,
        requested_page: int,
        prev_first_href: Optional[str],
    ) -> bool:
        """
        Return True if any signal indicates more pages exist (union / OR logic).

        Signal 1 — rel="next":   pagination nav contains a next-page link.
        Signal 2 — active match: active_page_num equals the requested page,
                   meaning the server returned the page we asked for (not a
                   clamped last page).
        Signal 3 — content diff: the first recording href changed vs the
                   previous page, meaning we are not looping on the same data.
        """
        s1 = result.has_next_page
        s2 = (result.active_page_num is not None and
              result.active_page_num == requested_page)
        s3 = bool(
            result.recordings and
            prev_first_href is not None and
            result.recordings[0].href != prev_first_href
        )
        logger.debug(
            f"has_more_pages page={requested_page}: "
            f"rel_next={s1} active_match={s2} content_diff={s3}"
        )
        return s1 or s2 or s3

    def _scrape_source(
        self,
        source_name: str,
        url_template: str,
        total_new: int,
        total_skipped: int,
        total_pages: int,
    ) -> tuple[int, int, int]:
        """Scrape all pages of a single source. Returns updated counters."""
        page_index = self.config.start_page
        consecutive_all_existing = 0
        prev_first_href: Optional[str] = None

        logger.info(f"── Source: {source_name} ──")

        while True:
            if self.config.max_pages and page_index >= self.config.start_page + self.config.max_pages:
                logger.info(f"Reached max_pages limit ({self.config.max_pages}) for {source_name}")
                break

            url = self._build_url(url_template, page_index)
            logger.info(f"Scraping {source_name} page {page_index} ...")

            try:
                response = self.session.get(url, timeout=self.config.request_timeout)
                response.raise_for_status()
            except requests.RequestException as e:
                logger.error(f"Failed to fetch {url}: {e}")
                raise

            result = self.parser.parse(response.text)

            # --- end-of-pages check (union of all signals) ---
            if not self._has_more_pages(result, page_index, prev_first_href):
                # Process this last page before breaking
                pass  # fall through to recording processing below

            # Track first href for content-diff signal on next iteration
            current_first_href = result.recordings[0].href if result.recordings else None

            # --- dedup and save ---
            new_recordings = []
            page_skipped = 0
            for recording in result.recordings:
                if recording.href in self.existing_hrefs:
                    page_skipped += 1
                    total_skipped += 1
                else:
                    new_recordings.append(recording)
                    self.existing_hrefs.add(recording.href)

            if new_recordings:
                with open(self.config.index_file, "a", encoding="utf-8") as f:
                    for recording in new_recordings:
                        f.write(json.dumps(recording.to_dict()) + "\n")
                        total_new += 1
                logger.info(
                    f"{source_name} p{page_index}: {len(new_recordings)} new, {page_skipped} skipped"
                )
                consecutive_all_existing = 0
            else:
                logger.info(f"{source_name} p{page_index}: all {page_skipped} already exist")
                consecutive_all_existing += 1

            total_pages += 1

            # Stop if all signals say no more pages
            if not self._has_more_pages(result, page_index, prev_first_href):
                logger.info(f"All end-of-pages signals agree for {source_name} at page {page_index}, stopping.")
                break

            # Also stop after 5 consecutive all-existing pages (caught up)
            if consecutive_all_existing >= 5:
                logger.info(
                    f"5 consecutive all-existing pages in {source_name}, caught up to existing data."
                )
                break

            prev_first_href = current_first_href
            page_index += 1
            self._random_delay()

        return total_new, total_skipped, total_pages

    def scrape_all(self) -> tuple[int, int, int]:
        """
        Scrape all explore sources and save new recording metadata to JSONL.

        Sources share a single dedup set so cross-source duplicates are written
        only once.  Returns (total_new, total_skipped, total_pages).
        """
        self.existing_hrefs = self._load_existing_hrefs()
        logger.info(f"Output: {self.config.index_file}")

        total_new = total_skipped = total_pages = 0
        for source_name, url_template in EXPLORE_SOURCES:
            total_new, total_skipped, total_pages = self._scrape_source(
                source_name, url_template, total_new, total_skipped, total_pages
            )

        logger.info(
            f"Scrape complete. New: {total_new}, Skipped: {total_skipped}, Pages: {total_pages}"
        )
        return total_new, total_skipped, total_pages


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
        # Create parent directory if it doesn't exist
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        logging.info(f"📝 Logging to file: {log_file}")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Scrape all asciinema explore sources to build recording index",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scrape_pages.py                   # Scrape all sources, skip existing
  python scrape_pages.py --max-pages 5     # Limit to 5 pages per source
  python scrape_pages.py -v                # Verbose (shows per-signal debug info)

Sources scraped (in order, shared dedup set):
  public, recent, featured, popular
        """,
    )
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Max pages per source (default: unlimited)")
    parser.add_argument("--index-file", type=str,
                        default="../data/asciinema_public_explore_pages.jsonl",
                        help="Output index file path")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose (debug) logging")
    parser.add_argument("--log-file", type=str, default=None,
                        help="Log file path (optional)")

    args = parser.parse_args()
    setup_logging(args.verbose, args.log_file)

    config = CrawlerConfig(
        max_pages=args.max_pages,
        index_file=Path(args.index_file),
    )

    scraper = PageScraper(config)
    scraper.scrape_all()


if __name__ == "__main__":
    main()
