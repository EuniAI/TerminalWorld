#!/usr/bin/env python3
"""
Detect and analyze external URLs mentioned in terminal recording metadata.

This script analyzes recording metadata (info.json) to extract all HTTP/HTTPS
URLs from the title/description, optionally checks their accessibility, and
stores lightweight metadata for downstream consumers.
"""

import argparse
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    logger = logging.getLogger(__name__)
    logger.warning("requests library not installed. URL accessibility checking will be disabled.")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# =============================================================================
# External URL Platform Hints
# =============================================================================

KNOWN_CODE_HOSTS = {
    'github.com': 'github',
    'gitlab.com': 'gitlab',
    'bitbucket.org': 'bitbucket',
    'gitee.com': 'gitee',
    'codeberg.org': 'codeberg',
    'sr.ht': 'sourcehut',
    'launchpad.net': 'launchpad',
    'sourceforge.net': 'sourceforge',
}

SELF_HOSTED_GIT_TOKENS = {'gitlab', 'gitea', 'gogs', 'forgejo', 'bitbucket'}
PROFILE_PREFIXES = {
    'orgs', 'users', 'organizations', 'explore', 'topics', 'marketplace',
    'features', 'search', 'login', 'settings', 'apps',
}
REPO_SUBPATH_MARKERS = {'tree', 'blob', 'raw', 'src', 'browse'}

# URL pattern to match HTTP/HTTPS links
URL_PATTERN = re.compile(
    r'https?://[^\s<>"\'\]\)]+',
    re.IGNORECASE
)

# Request timeout and retry settings
REQUEST_TIMEOUT = 60  # seconds
MAX_RETRIES = 2
USER_AGENT = 'Mozilla/5.0 (compatible; Terminal-Link-Detector/1.0; +https://github.com/)'


def extract_urls(text: str) -> list[str]:
    """
    Extract all HTTP/HTTPS URLs from text.
    
    Args:
        text: Text to search for URLs
        
    Returns:
        List of URLs found in the text
    """
    if not text:
        return []
    
    urls = URL_PATTERN.findall(text)
    
    # Clean up URLs (remove trailing punctuation)
    cleaned_urls = []
    for url in urls:
        # Remove common trailing punctuation
        url = url.rstrip('.,;:!?')
        # Remove trailing parentheses if not balanced
        if url.endswith(')') and '(' not in url:
            url = url.rstrip(')')
        cleaned_urls.append(url)
    
    return cleaned_urls


def _normalize_host(netloc: str) -> str:
    """Normalize a parsed URL host for platform matching."""
    host = netloc.lower()
    if '@' in host:
        host = host.split('@', 1)[1]
    if ':' in host:
        host = host.split(':', 1)[0]
    if host.startswith('www.'):
        host = host[4:]
    return host


def _host_has_token(host: str, token: str) -> bool:
    """Return True when token appears as a full host label."""
    return token in re.split(r'[.-]', host)


def _classify_platform_from_parts(host: str, path: str) -> Optional[str]:
    """Classify the URL host into a known platform when evidence is strong."""
    for known_host, platform in KNOWN_CODE_HOSTS.items():
        if host == known_host or host.endswith('.' + known_host):
            return platform

    segments = [segment for segment in path.split('/') if segment]
    if len(segments) >= 2 and any(_host_has_token(host, token) for token in SELF_HOSTED_GIT_TOKENS):
        return 'self_hosted_git'

    return None


def _is_repo_url_from_parts(platform: Optional[str], host: str, path: str) -> bool:
    """Return True only when the URL clearly points to repository content."""
    segments = [segment for segment in path.split('/') if segment]

    if not segments:
        return False

    lowered_segments = [segment.lower() for segment in segments]
    if lowered_segments[0] in PROFILE_PREFIXES:
        return False

    if platform in {'github', 'gitee', 'codeberg'}:
        third = lowered_segments[2] if len(lowered_segments) >= 3 else ''
        return len(segments) == 2 or third in REPO_SUBPATH_MARKERS

    if platform in {'gitlab', 'self_hosted_git'}:
        if len(lowered_segments) >= 4 and lowered_segments[2] == '-':
            return lowered_segments[3] in REPO_SUBPATH_MARKERS
        third = lowered_segments[2] if len(lowered_segments) >= 3 else ''
        return len(segments) == 2 or third in REPO_SUBPATH_MARKERS

    if platform == 'bitbucket':
        third = lowered_segments[2] if len(lowered_segments) >= 3 else ''
        return len(segments) == 2 or third in {'src', 'branch', 'branches'}

    if platform == 'sourcehut':
        if len(segments) < 2 or not segments[0].startswith('~'):
            return False
        third = lowered_segments[2] if len(lowered_segments) >= 3 else ''
        return len(segments) == 2 or third in REPO_SUBPATH_MARKERS

    return False


def detect_platform(url: str) -> Optional[str]:
    """
    Detect the likely hosting platform for a URL.
    
    Args:
        url: URL to check
        
    Returns:
        Platform name if recognized, else None.
    """
    try:
        parsed = urlparse(url)
        host = _normalize_host(parsed.netloc)
        return _classify_platform_from_parts(host, parsed.path)

    except Exception as e:
        logger.debug(f"Failed to parse URL {url}: {e}")
        return None


def check_url_accessibility(url: str, timeout: int = REQUEST_TIMEOUT) -> dict:
    """
    Check if a URL is accessible.

    Returns a dict with:
    - accessible: True if status 200-399, False otherwise, None if unchecked
    - status_code: HTTP status code (or None if error)
    - error: Error message if not accessible
    - redirect_url: Final URL after redirects (if different)
    - access_status: Semantic label (accessible/auth_required/forbidden/not_found/
                     server_error/timeout/network_error/inaccessible/unknown)
    - privacy: public / private_or_auth_required / unknown
    """
    if not HAS_REQUESTS:
        return {
            'accessible': None, 'status_code': None,
            'error': 'requests library not installed', 'redirect_url': None,
            'access_status': 'unknown', 'privacy': 'unknown',
        }

    result = {
        'accessible': False, 'status_code': None,
        'error': None, 'redirect_url': None,
    }

    headers = {
        'User-Agent': USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)

            if response.status_code == 405 or response.status_code == 501:
                response = requests.get(url, headers=headers, timeout=timeout,
                                        allow_redirects=True, stream=True)
                response.close()

            result['status_code'] = response.status_code
            if response.url != url:
                result['redirect_url'] = response.url

            if 200 <= response.status_code < 400:
                result['accessible'] = True
            elif response.status_code == 404:
                result['error'] = '404 Not Found'
            elif response.status_code == 403:
                result['error'] = '403 Forbidden'
            elif response.status_code == 401:
                result['error'] = '401 Unauthorized'
            elif response.status_code >= 500:
                result['error'] = f'{response.status_code} Server Error'
                if attempt < MAX_RETRIES - 1:
                    time.sleep(1)
                    continue
            else:
                result['error'] = f'HTTP {response.status_code}'

        except requests.exceptions.Timeout:
            result['error'] = 'Request timeout'
            if attempt < MAX_RETRIES - 1:
                continue
        except requests.exceptions.ConnectionError as e:
            result['error'] = f'Connection error: {str(e)[:100]}'
        except requests.exceptions.TooManyRedirects:
            result['error'] = 'Too many redirects'
        except requests.exceptions.SSLError:
            result['error'] = 'SSL certificate error'
        except requests.exceptions.RequestException as e:
            result['error'] = f'Request error: {str(e)[:100]}'
        except Exception as e:
            result['error'] = f'Unexpected error: {str(e)[:100]}'

        break

    # Derive semantic labels from raw result
    status_code = result['status_code']
    error = str(result.get('error') or '').lower()
    accessible = result['accessible']

    if accessible is True:
        access_status = 'accessible'
    elif isinstance(status_code, int):
        if 200 <= status_code < 400:
            access_status = 'accessible'
        elif status_code == 401:
            access_status = 'auth_required'
        elif status_code == 403:
            access_status = 'forbidden'
        elif status_code == 404:
            access_status = 'not_found'
        elif status_code >= 500:
            access_status = 'server_error'
        else:
            access_status = 'unknown'
    elif accessible is False:
        if 'timeout' in error:
            access_status = 'timeout'
        elif 'connection' in error or 'ssl' in error or 'redirect' in error or 'network' in error:
            access_status = 'network_error'
        elif 'unauthorized' in error or 'authentication' in error or 'auth required' in error:
            access_status = 'auth_required'
        elif 'forbidden' in error:
            access_status = 'forbidden'
        elif '404' in error or 'not found' in error:
            access_status = 'not_found'
        elif error:
            access_status = 'inaccessible'
        else:
            access_status = 'unknown'
    else:
        access_status = 'unknown'

    if access_status == 'accessible':
        privacy = 'public'
    elif access_status == 'auth_required':
        privacy = 'private_or_auth_required'
    elif any(t in error for t in ('private', 'login required', 'authentication required', 'unauthorized')):
        privacy = 'private_or_auth_required'
    else:
        privacy = 'unknown'

    result['access_status'] = access_status
    result['privacy'] = privacy
    return result


def analyze_recording(info_file: Path, check_accessibility: bool = False, timeout: int = REQUEST_TIMEOUT) -> dict:
    """
    Analyze a single recording's info.json for external URLs.
    
    Args:
        info_file: Path to info.json file
        check_accessibility: Whether to check URL accessibility
        timeout: Request timeout in seconds
        
    Returns:
        Dictionary containing analysis results
    """
    recording_id = info_file.parent.name
    
    try:
        with open(info_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Extract title and description
        title = data.get('title', '')
        description = data.get('description', '')
        
        # Combine title and description for URL extraction
        combined_text = f"{title}\n{description}"
        
        # Extract all URLs
        urls = extract_urls(combined_text)
        
        # Analyze each extracted URL without filtering any out
        external_urls = []
        platforms = set()
        
        for url in urls:
            parsed = urlparse(url)
            host = _normalize_host(parsed.netloc)
            platform = _classify_platform_from_parts(host, parsed.path)
            url_info = {
                'url': url,
                'host': host,
                'platform': platform,
                'is_repo': _is_repo_url_from_parts(platform, host, parsed.path),
            }

            # Check accessibility if requested
            if check_accessibility:
                accessibility = check_url_accessibility(url, timeout=timeout)
                url_info.update(accessibility)

            external_urls.append(url_info)
            if platform:
                platforms.add(platform)
        
        has_external_urls = len(external_urls) > 0
        
        # Count accessible/inaccessible URLs if accessibility check was performed
        accessible_count = None
        inaccessible_count = None
        if check_accessibility and has_external_urls:
            accessible_count = sum(1 for u in external_urls if u.get('accessible') is True)
            inaccessible_count = sum(1 for u in external_urls if u.get('accessible') is False)
        
        result = {
            'recording_id': recording_id,
            'has_external_urls': has_external_urls,
            'external_urls': external_urls,
            'platforms': sorted(list(platforms)),
            'url_count': len(external_urls),
            'all_urls': urls,
        }
        
        if check_accessibility and has_external_urls:
            result['accessible_count'] = accessible_count
            result['inaccessible_count'] = inaccessible_count
        
        if has_external_urls:
            if check_accessibility:
                logger.info(f"{recording_id}: Found {len(external_urls)} URL(s) - "
                          f"{accessible_count} accessible, {inaccessible_count} inaccessible")
            else:
                logger.info(f"{recording_id}: Found {len(external_urls)} external URL(s)")
        
        return result
        
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to analyze {recording_id}: {e}")
        return {
            'recording_id': recording_id,
            'has_external_urls': False,
            'external_urls': [],
            'platforms': [],
            'url_count': 0,
            'all_urls': [],
            'error': str(e)
        }


def process_recordings(
    recordings_dir: Path,
    output_file: Path,
    check_accessibility: bool = False,
    max_workers: int = 10,
    timeout: int = REQUEST_TIMEOUT
) -> dict:
    """
    Process all recordings in the directory.
    
    Args:
        recordings_dir: Directory containing recording folders
        output_file: Path to output JSON file
        check_accessibility: Whether to check URL accessibility
        max_workers: Maximum number of parallel workers for accessibility checks
        timeout: Request timeout in seconds
        
    Returns:
        Dictionary mapping recording_id to detection results
    """
    # Find all info.json files
    info_files = list(recordings_dir.glob("*/info.json"))
    
    if not info_files:
        logger.warning(f"No info.json files found in {recordings_dir}")
        return {}
    
    logger.info(f"Found {len(info_files):,} recordings to analyze")
    
    if check_accessibility:
        if not HAS_REQUESTS:
            logger.error("Cannot check URL accessibility: requests library not installed")
            logger.error("Install it with: pip install requests")
            check_accessibility = False
        else:
            logger.info(f"URL accessibility checking enabled (timeout: {timeout}s, max {max_workers} parallel workers)")
    
    # Analyze each recording
    results = {}
    with_external_urls = 0
    without_external_urls = 0
    total_accessible = 0
    total_inaccessible = 0
    
    for idx, info_file in enumerate(info_files, 1):
        if idx % 100 == 0:
            logger.info(f"Progress: {idx:,} / {len(info_files):,} recordings processed")
        
        result = analyze_recording(info_file, check_accessibility=check_accessibility, timeout=timeout)
        recording_id = result['recording_id']
        results[recording_id] = result
        
        if result['has_external_urls']:
            with_external_urls += 1
            if check_accessibility:
                total_accessible += result.get('accessible_count', 0)
                total_inaccessible += result.get('inaccessible_count', 0)
        else:
            without_external_urls += 1
    
    logger.info(f"Analysis complete:")
    logger.info(f"  With external URLs: {with_external_urls:,} ({with_external_urls/len(info_files)*100:.1f}%)")
    logger.info(f"  Without external URLs: {without_external_urls:,} ({without_external_urls/len(info_files)*100:.1f}%)")
    
    if check_accessibility and (total_accessible + total_inaccessible) > 0:
        total_checked = total_accessible + total_inaccessible
        logger.info(f"\nURL Accessibility:")
        logger.info(f"  Accessible: {total_accessible:,} ({total_accessible/total_checked*100:.1f}%)")
        logger.info(f"  Inaccessible: {total_inaccessible:,} ({total_inaccessible/total_checked*100:.1f}%)")
    
    # Calculate platform statistics
    platform_counts = {}
    for result in results.values():
        for platform in result['platforms']:
            platform_counts[platform] = platform_counts.get(platform, 0) + 1
    
    if platform_counts:
        logger.info(f"\nPlatform Distribution:")
        for platform, count in sorted(platform_counts.items(), key=lambda x: x[1], reverse=True):
            pct = count / with_external_urls * 100
            logger.info(f"  {platform:<30} {count:>6,} ({pct:>5.1f}%)")
    
    # Save results
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Create simplified output: {recording_id: [url_info_list]}
    simplified_output = {}
    for recording_id, result in results.items():
        simplified_output[recording_id] = result.get('external_urls', [])
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(simplified_output, f, ensure_ascii=False, indent=2)
    
    logger.info(f"\nResults saved to: {output_file}")
    
    # Write per-recording analysis.json (merge with existing data)
    logger.info("Writing per-recording analysis.json files...")
    per_recording_written = 0
    for recording_id, result in results.items():
        analysis_path = recordings_dir / recording_id / "analysis.json"
        existing = {}
        if analysis_path.exists():
            try:
                with open(analysis_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError):
                existing = {}
        existing["external_urls"] = result.get('external_urls', [])
        with open(analysis_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        per_recording_written += 1
    logger.info(f"Per-recording analysis.json written for {per_recording_written:,} recordings")
    
    return results


def generate_report(results: dict, check_accessibility: bool = False) -> str:
    """
    Generate a human-readable report of the analysis.
    
    Args:
        results: Dictionary of analysis results
        check_accessibility: Whether accessibility checking was performed
        
    Returns:
        Formatted report string
    """
    total = len(results)
    with_external = sum(1 for r in results.values() if r['has_external_urls'])
    without_external = total - with_external
    
    if total == 0:
        return "No recordings analyzed."
    
    # Platform statistics
    platform_counts = {}
    for result in results.values():
        for platform in result['platforms']:
            platform_counts[platform] = platform_counts.get(platform, 0) + 1
    
    report = f"""
============================================================
External URL Detection Report
============================================================
Total Recordings:      {total:,}

With External URLs:    {with_external:,} ({with_external/total*100:.1f}%)
Without External URLs: {without_external:,} ({without_external/total*100:.1f}%)

"""
    
    # Add accessibility statistics if available
    if check_accessibility:
        total_accessible = sum(r.get('accessible_count', 0) for r in results.values())
        total_inaccessible = sum(r.get('inaccessible_count', 0) for r in results.values())
        total_checked = total_accessible + total_inaccessible
        
        if total_checked > 0:
            report += f"""URL Accessibility:
  Accessible:          {total_accessible:,} ({total_accessible/total_checked*100:.1f}%)
  Inaccessible:        {total_inaccessible:,} ({total_inaccessible/total_checked*100:.1f}%)
  Total Checked:       {total_checked:,}

"""
    
    if platform_counts:
        report += "Platform Distribution:\n"
        for platform, count in sorted(platform_counts.items(), key=lambda x: x[1], reverse=True):
            pct = count / with_external * 100
            bar_length = int(pct / 2)
            bar = "█" * bar_length
            report += f"  {platform:<30} {bar:<50} {count:>6,} ({pct:>5.1f}%)\n"
    
    report += "============================================================"
    
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Detect external URLs in terminal recording metadata",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
This script analyzes info.json files to extract external HTTP/HTTPS URLs
from recording metadata and annotate them with lightweight platform and
accessibility hints for downstream consumers.

Examples:
    python detect_external_urls.py
    python detect_external_urls.py --recordings-dir ../data/recordings
    python detect_external_urls.py --output-file output/external_url_detection.json
    python detect_external_urls.py --max-workers 20  # Use 20 parallel workers
    python detect_external_urls.py --timeout 15  # Set 15 seconds timeout
        """
    )
    
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=Path(__file__).parent.parent / "data" / "recordings",
        help="Path to recordings directory"
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=Path("output/external_url_detection.json"),
        help="Output JSON file (default: output/external_url_detection.json)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging (debug level)"
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=10,
        help="Maximum number of parallel workers for URL accessibility checks (default: 10)"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=REQUEST_TIMEOUT,
        help=f"Request timeout in seconds for accessibility checks (default: {REQUEST_TIMEOUT})"
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    # Check if recordings directory exists
    if not args.recordings_dir.exists():
        logger.error(f"Recordings directory not found: {args.recordings_dir}")
        return 1
    
    # Warn if requests library is not installed
    if not HAS_REQUESTS:
        logger.error("URL accessibility checking requires the 'requests' library")
        logger.error("Install it with: pip install requests")
        return 1
    
    # Process recordings (always check accessibility)
    results = process_recordings(
        args.recordings_dir,
        args.output_file,
        check_accessibility=True,
        max_workers=args.max_workers,
        timeout=args.timeout
    )
    
    if not results:
        logger.error("No recordings were processed")
        return 1
    
    # Generate and print report
    report = generate_report(results, check_accessibility=True)
    print(report)
    
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
