"""
HTML parsers for asciinema.org pages.

Provides two parsers:
- ExplorePageParser: Parses listing pages at /explore/public
- CastDescriptionParser: Parses individual recording pages at /a/{id}
"""

from dataclasses import dataclass, field
from typing import Optional
import logging
import re

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class RecordingMetadata:
    """
    Metadata for a single recording extracted from the explore listing page.
    
    Fields:
        href: URL path like "/a/648882"
        title: Recording title
        duration: Duration string like "2:30"
        author: Author's username
        submit_time: ISO datetime string
    """
    href: Optional[str] = None
    title: Optional[str] = None
    duration: Optional[str] = None
    author: Optional[str] = None
    submit_time: Optional[str] = None

    @property
    def record_id(self) -> Optional[str]:
        """Extract numeric ID from href. Example: '/a/648882' -> '648882'"""
        if self.href:
            return self.href.split("/")[-1]
        return None

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return {
            "href": self.href,
            "title": self.title,
            "duration": self.duration,
            "author": self.author,
            "submit_time": self.submit_time,
        }


@dataclass
class CastDescription:
    """
    Detailed information from a recording's individual page.

    Fields:
        title: Full title (may differ from listing page)
        description: User-provided description text
        asciicast_version: "v1", "v2", "v3", or "unknown"
        total_views: View count as string
        environment: Environment info string (e.g., "macOS • zsh", "Linux • xterm • bash")
        terminal_cols: Terminal width
        terminal_rows: Terminal height
        download_urls: Dict containing only the .txt transcript URL
    """
    title: str = "No title found"
    description: str = "No description found"
    asciicast_version: str = "unknown"
    total_views: str = "unknown"
    environment: Optional[str] = None
    terminal_cols: Optional[int] = None
    terminal_rows: Optional[int] = None
    download_urls: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "asciicast_version": self.asciicast_version,
            "total_views": self.total_views,
            "environment": self.environment,
            "terminal_cols": self.terminal_cols,
            "terminal_rows": self.terminal_rows,
            "download_urls": self.download_urls,
        }


@dataclass
class ExplorePageResult:
    """
    Complete parsing result from an explore listing page.

    has_next_page: True when the pagination nav contains a rel="next" link.
    active_page_num: best-effort current page number from the active pagination
        element; None if not found.
    recordings: recording metadata extracted from this page.
    """
    has_next_page: bool = True
    active_page_num: Optional[int] = None
    recordings: list[RecordingMetadata] = field(default_factory=list)


class ExplorePageParser:
    """
    Parser for asciinema.org/explore/public listing pages.
    
    Extracts recording metadata from the paginated public recordings list.
    Each page typically contains 12 recordings.
    """

    def parse(self, html_content: str) -> ExplorePageResult:
        """
        Parse an explore page and extract all recording metadata.

        Args:
            html_content: Raw HTML string

        Returns:
            ExplorePageResult with pagination signals and list of recordings
        """
        soup = BeautifulSoup(html_content, "lxml")
        result = ExplorePageResult()

        result.active_page_num = self._extract_active_page_num(soup)
        result.has_next_page = self._has_next_page(soup)

        for div_info in soup.find_all("div", class_="info"):
            metadata = self._extract_recording_metadata(div_info)
            result.recordings.append(metadata)
            logger.debug(f"Extracted: {metadata.href} - {metadata.title}")

        logger.info(f"Page {result.active_page_num}: {len(result.recordings)} recordings, has_next={result.has_next_page}")
        return result

    def _has_next_page(self, soup: BeautifulSoup) -> bool:
        """Return True if the pagination nav contains a rel="next" link."""
        nav = soup.find("nav", class_="pagination")
        if nav and nav.find("a", rel="next"):
            return True
        return False

    def _extract_active_page_num(self, soup: BeautifulSoup) -> Optional[int]:
        """
        Extract current page number from pagination.

        Supports two formats:
        - New (2026+): <nav class="pagination"><a class="active">N</a>
        - Old: <li class="active page-item">N</li>

        Note: on the new site the nav only renders up to 10 page links at a
        time, so active_page_num caps at the highest visible link when the
        requested page exceeds it.  Use has_next_page as the primary
        end-of-pages signal; active_page_num is supplementary.
        """
        nav = soup.find("nav", class_="pagination")
        if nav:
            active_a = nav.find("a", class_="active")
            if active_a:
                try:
                    return int(active_a.text.strip())
                except ValueError:
                    logger.warning(f"Invalid page number: {active_a.text}")

        active_page_li = soup.find("li", class_="active page-item")
        if active_page_li:
            try:
                return int(active_page_li.text.strip())
            except ValueError:
                logger.warning(f"Invalid page number: {active_page_li.text}")

        return None

    def _extract_recording_metadata(self, div_info) -> RecordingMetadata:
        """
        Extract metadata from a single recording's info div.
        
        Expected HTML structure:
            <div class="info">
                <a href="/a/123">Title</a>
                <span class="duration">2:30</span>
                <span class="author-avatar"><a title="username">...</a></span>
                <small><time datetime="2024-01-01T12:00:00Z">...</time></small>
            </div>
        """
        metadata = RecordingMetadata()

        a_tag = div_info.find("a")
        if a_tag:
            metadata.href = a_tag.get("href")
            metadata.title = a_tag.text.strip() if a_tag.text else None

        duration_span = div_info.find("span", class_="duration")
        if duration_span:
            metadata.duration = duration_span.text.strip()

        author_span = div_info.find("span", class_="author-avatar")
        if author_span:
            author_a = author_span.find("a")
            if author_a:
                metadata.author = author_a.get("title")

        small_tag = div_info.find("small")
        if small_tag:
            time_tag = small_tag.find("time")
            if time_tag:
                metadata.submit_time = time_tag.get("datetime")

        return metadata


class CastDescriptionParser:
    """
    Parser for individual recording pages at asciinema.org/a/{id}.
    
    Extracts detailed information including description text,
    file format version, and view count.
    """

    def parse(self, html_content: str) -> CastDescription:
        """
        Parse a recording's description page.
        
        Args:
            html_content: Raw HTML string
            
        Returns:
            CastDescription with all extracted fields
        """
        soup = BeautifulSoup(html_content, "html.parser")
        result = CastDescription()

        result.title = self._extract_title(soup)
        result.description = self._extract_description(soup)
        result.asciicast_version = self._extract_version(soup)
        result.total_views = self._extract_total_views(soup)

        # Extract environment info (e.g., "macOS • zsh", "Linux • xterm • bash")
        result.environment = self._extract_environment_info(soup)

        # Extract terminal size from player script
        size_info = self._extract_terminal_size(soup)
        result.terminal_cols = size_info.get("cols")
        result.terminal_rows = size_info.get("rows")

        # Extract .txt transcript download URL only
        result.download_urls = self._extract_download_urls(soup)

        # Log missing fields for debugging
        self._check_missing_fields(result, html_content)

        logger.debug(f"Parsed: {result.title} ({result.asciicast_version})")
        return result
    
    def _check_missing_fields(self, result: CastDescription, html_content: str):
        """
        Check for missing fields and log status.
        
        Logs both successful extraction and missing fields for debugging.
        """
        # Extract recording ID from HTML for logging
        record_id = "unknown"
        id_match = re.search(r'/a/(\d+)', html_content)
        if id_match:
            record_id = id_match.group(1)
        
        missing_fields = []
        
        # Check critical fields (description is optional, so skip it)
        if result.title == "No title found":
            missing_fields.append("title")
        if result.asciicast_version == "unknown":
            missing_fields.append("asciicast_version")
        if result.total_views == "unknown":
            missing_fields.append("total_views")

        # Check environment field
        if not result.environment:
            missing_fields.append("environment")

        # Check terminal size
        if not result.terminal_cols:
            missing_fields.append("terminal_cols")
        if not result.terminal_rows:
            missing_fields.append("terminal_rows")

        # Check that the .txt transcript URL was found
        if not result.download_urls or not result.download_urls.get("txt"):
            missing_fields.append("txt_download_url")
        
        # Log with clear markers
        if missing_fields:
            logger.warning(
                f"⚠️  [MISSING] Recording {record_id}: {', '.join(missing_fields)} | "
                f"URL: https://asciinema.org/a/{record_id}"
            )
        else:
            logger.info(
                f"✅ [SUCCESS] Recording {record_id}: All fields extracted successfully"
            )

    def _extract_title(self, soup: BeautifulSoup) -> str:
        """
        Extract title, trying section.even.info first,
        then falling back to the page <title> tag.
        """
        section_info = soup.find("section", class_="even info")
        if section_info:
            title_h2 = section_info.find("h2")
            if title_h2 and hasattr(title_h2, "text"):
                return title_h2.text.strip()

        title_elem = soup.find("title")
        if title_elem:
            return title_elem.text.replace("- asciinema.org", "").strip()

        return "No title found"

    def _extract_description(self, soup: BeautifulSoup) -> str:
        """Extract description from div.description."""
        desc_div = soup.find("div", class_="description")
        if desc_div:
            return desc_div.text.strip()
        return "No description found"

    def _extract_version(self, soup: BeautifulSoup) -> str:
        """
        Extract asciicast format version from download modal.
        Looks for "asciicast vN format" text pattern.
        """
        download_modal = soup.find(id="download-modal")
        if download_modal:
            modal_body = download_modal.find("div", class_="modal-body")
            if modal_body:
                body_text = modal_body.text
                if "asciicast v3 format" in body_text:
                    return "v3"
                elif "asciicast v2 format" in body_text:
                    return "v2"
                elif "asciicast v1 format" in body_text:
                    return "v1"
        return "unknown"

    def _extract_total_views(self, soup: BeautifulSoup) -> str:
        """Extract view count from span[title='Total views']."""
        view_span = soup.find("span", attrs={"title": "Total views"})
        if view_span:
            views_text = view_span.text.strip()
            return views_text.split()[0] if views_text else "unknown"
        return "unknown"

    def _extract_environment_info(self, soup: BeautifulSoup) -> Optional[str]:
        """
        Extract environment info string from the page.
        
        Returns the raw environment string as-is (e.g., "macOS • zsh", "Linux • xterm • bash").
        Supports multiple HTML formats for backwards compatibility.
        """
        # Method 1: New format - look for env-info class (current asciinema.org)
        env_info = soup.find("span", class_="env-info")
        if env_info:
            text = env_info.get_text(separator=" ", strip=True)
            # Clean up extra spaces around bullets
            text = re.sub(r'\s*•\s*', ' • ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            if text:
                return text
        
        # Method 2: Old format - status-line-item spans with bullet separators
        status_items = soup.find_all("span", class_="status-line-item")
        for item in status_items:
            text = item.get_text(strip=True)
            # Skip the views count
            if "views" in text.lower():
                continue
            # Check if contains environment-like info (has bullet or looks like OS/shell)
            if "•" in text or any(x in text.lower() for x in ["linux", "macos", "windows", "bsd", "zsh", "bash", "fish"]):
                return text
        
        # Method 3: Look for any text with bullet pattern in the page
        for span in soup.find_all("span"):
            text = span.get_text(strip=True)
            if "•" in text and "views" not in text.lower() and len(text) < 100:
                return text
        
        return None

    def _extract_terminal_size(self, soup: BeautifulSoup) -> dict:
        """
        Extract terminal size (cols, rows) from the player initialization script
        or from the page content. Supports multiple formats for backwards compatibility.
        """
        result = {"cols": None, "rows": None}
        
        # Method 1: Look in script tags for AsciinemaPlayer.create() or similar
        for script in soup.find_all("script"):
            script_text = script.string or ""
            
            # Try cols/rows format
            cols_match = re.search(r'cols:\s*(\d+)', script_text)
            rows_match = re.search(r'rows:\s*(\d+)', script_text)
            
            if cols_match:
                result["cols"] = int(cols_match.group(1))
            if rows_match:
                result["rows"] = int(rows_match.group(1))
            
            if result["cols"] and result["rows"]:
                return result
            
            # Try width/height format (older versions)
            if not result["cols"]:
                width_match = re.search(r'width:\s*(\d+)', script_text)
                if width_match:
                    result["cols"] = int(width_match.group(1))
            if not result["rows"]:
                height_match = re.search(r'height:\s*(\d+)', script_text)
                if height_match:
                    result["rows"] = int(height_match.group(1))
            
            if result["cols"] and result["rows"]:
                return result
        
        # Method 2: Look in HTML content (may use HTML entities like &lbrace;)
        html_content = str(soup)
        
        # Match { cols: 99, rows: 33 } or &lbrace; cols: 99, rows: 33 &rbrace;
        pattern = r'(?:&lbrace;|\{)\s*cols:\s*(\d+),\s*rows:\s*(\d+)\s*(?:&rbrace;|\})'
        match = re.search(pattern, html_content)
        if match:
            result["cols"] = int(match.group(1))
            result["rows"] = int(match.group(2))
            return result
        
        # Try separate patterns
        if not result["cols"]:
            cols_match = re.search(r'cols:\s*(\d+)', html_content)
            if cols_match:
                result["cols"] = int(cols_match.group(1))
        if not result["rows"]:
            rows_match = re.search(r'rows:\s*(\d+)', html_content)
            if rows_match:
                result["rows"] = int(rows_match.group(1))
        
        return result

    def _extract_download_urls(self, soup: BeautifulSoup) -> dict:
        """
        Extract the plain-text transcript download URL from the download modal.

        Only the .txt transcript is collected; raw cast files (.cast/.json) and
        generated media (.gif) are intentionally excluded to comply with
        asciinema's robots.txt policy and the project's data ethics statement.
        """
        result = {}

        download_modal = soup.find(id="download-modal")
        if download_modal:
            for link in download_modal.find_all("a", href=True):
                href = link["href"]
                if ".txt" in href:
                    result["txt"] = href

        return result
