"""
Shared configuration for the asciinema crawler.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# All explore sources to crawl. Each entry is (name, url_template).
# {base_url} and {page} are substituted at crawl time.
EXPLORE_SOURCES: list[tuple[str, str]] = [
    ("public",   "{base_url}/explore/public?page={page}"),
    ("recent",   "{base_url}/explore/recordings/recent?page={page}"),
    ("featured", "{base_url}/explore/recordings/featured?page={page}"),
    ("popular",  "{base_url}/explore/recordings/popular?page={page}"),
]


@dataclass
class CrawlerConfig:
    """
    Configuration for the asciinema crawler.

    All settings have sensible defaults. Customize by passing values:
        config = CrawlerConfig(retry_count=5)
    """

    # URLs
    base_url: str = "https://asciinema.org"

    # Output paths
    output_dir: Path = field(default_factory=lambda: Path("../data/recordings"))
    index_file: Path = field(default_factory=lambda: Path("../data/asciinema_public_explore_pages.jsonl"))

    # Crawling parameters
    start_page: int = 1
    max_pages: Optional[int] = None  # None = unlimited per source

    # Request settings
    request_timeout: int = 120
    retry_count: int = 3
    retry_backoff_factor: float = 1.0
    delay_between_requests: tuple[float, float] = (1.0, 3.0)

    # HTTP headers
    user_agent: str = "Mozilla/5.0 (compatible; AsciinemaArchiver/1.0)"

    def __post_init__(self):
        """Convert string paths to Path objects."""
        if isinstance(self.output_dir, str):
            self.output_dir = Path(self.output_dir)
        if isinstance(self.index_file, str):
            self.index_file = Path(self.index_file)

