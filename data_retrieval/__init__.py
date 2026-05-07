"""
Terminal Recording Crawling Package

A crawler for downloading terminal recordings from asciinema.org.
Only .txt transcripts and info.json metadata are collected; raw cast files
and generated media are intentionally excluded per the project's data ethics policy.

Modules:
    config.py              - Shared configuration
    parsers.py             - HTML parsers
    scrape_pages.py        - Scrape explore pages to build index
    download_recordings.py - Download recordings from index

Usage:
    # Step 1: Scrape pages to build index
    python scrape_pages.py --start-page 1

    # Step 2: Download recordings (.txt transcripts + info.json metadata)
    python download_recordings.py --output-dir ./recordings
"""

from .config import CrawlerConfig
from .parsers import (
    ExplorePageParser,
    CastDescriptionParser,
    RecordingMetadata,
    CastDescription,
    ExplorePageResult,
)
from .scrape_pages import PageScraper
from .download_recordings import RecordingDownloader

__all__ = [
    "CrawlerConfig",
    "PageScraper",
    "RecordingDownloader",
    "ExplorePageParser",
    "CastDescriptionParser",
    "RecordingMetadata",
    "CastDescription",
    "ExplorePageResult",
]

__version__ = "1.0.0"
