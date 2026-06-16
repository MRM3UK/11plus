#!/usr/bin/env python3
"""
Fetch stream links from http://ott.11plus.live/ and generate M3U playlist.
"""

import requests
import re
import json
import os
import time
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs

# Configuration
BASE_URL = "http://ott.11plus.live/"
OUTPUT_M3U = "playlist.m3u"
OUTPUT_JSON = "channels.json"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 5

# Common headers to mimic a browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Referer": BASE_URL,
}

# Stream URL patterns to match
STREAM_PATTERNS = [
    # Pattern like: http://30.30.30.138:8088/302/index.m3u8?token=...
    re.compile(
        r'https?://[\d\.]+:\d+/\d+/index\.m3u8\?token=[a-f0-9\-]+',
        re.IGNORECASE
    ),
    # Generic m3u8 pattern
    re.compile(
        r'https?://[^\s\'"<>]+\.m3u8(?:\?[^\s\'"<>]*)?',
        re.IGNORECASE
    ),
    # TS stream pattern
    re.compile(
        r'https?://[^\s\'"<>]+\.ts(?:\?[^\s\'"<>]*)?',
        re.IGNORECASE
    ),
    # MP4 stream pattern
    re.compile(
        r'https?://[^\s\'"<>]+\.mp4(?:\?[^\s\'"<>]*)?',
        re.IGNORECASE
    ),
]


def make_request(url, session, retries=MAX_RETRIES):
    """Make HTTP request with retry logic."""
    for attempt in range(retries):
        try:
            print(f"  Requesting: {url} (attempt {attempt + 1}/{retries})")
            response = session.get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
                verify=False
            )
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            print(f"  Error on attempt {attempt + 1}: {e}")
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY)
    return None


def extract_stream_urls(text):
    """Extract stream URLs from text content."""
    found_urls = set()
    for pattern in STREAM_PATTERNS:
        matches = pattern.findall(text)
        for match in matches:
            # Clean up the URL
            clean_url = match.strip().rstrip("'\">,;)")
            found_urls.add(clean_url)
    return found_urls


def extract_channel_info(element):
    """Try to extract channel name from surrounding HTML elements."""
    # Try various methods to get channel name
    name = None

    # Check for title attribute
    if hasattr(element, 'get'):
        name = element.get('title') or element.get('alt') or element.get('data-name')

    # Check parent elements for text
    if not name and hasattr(element, 'parent'):
        parent = element.parent
        if parent:
            # Look for text in parent
            text = parent.get_text(strip=True)
            if text and len(text) < 100:
                name = text

    return name


def fetch_main_page(session):
    """Fetch the main page and extract channel links and stream URLs."""
    print(f"Fetching main page: {BASE_URL}")
    response = make_request(BASE_URL, session)
    if not response:
        print("Failed to fetch main page")
        return [], set()

    soup = BeautifulSoup(response.text, 'lxml')
    content = response.text

    # Extract direct stream URLs from main page
    stream_urls = extract_stream_urls(content)
    print(f"Found {len(stream_urls)} stream URLs on main page")

    # Find all links that might lead to channel pages
    channel_links = []
    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        full_url = urljoin(BASE_URL, href)

        # Skip external links, anchors, javascript
        if href.startswith('#') or href.startswith('javascript:'):
            continue

        # Get channel name from link text or attributes
        channel_name = (
            link.get_text(strip=True) or
            link.get('title', '') or
            link.get('data-name', '') or
            ''
        )

        if full_url.startswith(BASE_URL) or urlparse(full_url).netloc == urlparse(BASE_URL).netloc:
            channel_links.append({
                'url': full_url,
                'name': channel_name,
            })

    print(f"Found {len(channel_links)} internal links")
    return channel_links, stream_urls


def fetch_channel_page(url, session):
    """Fetch a channel page and extract stream URLs."""
    response = make_request(url, session)
    if not response:
        return set()

    content = response.text
    stream_urls = extract_stream_urls(content)

    # Also check for JavaScript-embedded URLs
    # Look for patterns like: source: "http://...", src="http://...", url: "http://..."
    js_patterns = [
        re.compile(r'(?:source|src|url|file|stream)["\s]*[:=]\s*["\']('
                   r'https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)["\']', re.IGNORECASE),
        re.compile(r'(?:source|src|url|file|stream)["\s]*[:=]\s*["\']('
                   r'https?://[\d\.]+:\d+/[^\s\'"<>]+)["\']', re.IGNORECASE),
    ]

    for pattern in js_patterns:
        matches = pattern.findall(content)
        for match in matches:
            clean_url = match.strip().rstrip("'\">,;)")
            stream_urls.add(clean_url)

    # Check for iframe sources
    soup = BeautifulSoup(content, 'lxml')
    for iframe in soup.find_all('iframe', src=True):
        iframe_src = urljoin(url, iframe['src'])
        print(f"  Found iframe: {iframe_src}")
        iframe_response = make_request(iframe_src, session)
        if iframe_response:
            iframe_streams = extract_stream_urls(iframe_response.text)
            stream_urls.update(iframe_streams)
            # Check JS patterns in iframe too
            for pattern in js_patterns:
                matches = pattern.findall(iframe_response.text)
                for match in matches:
                    clean_url = match.strip().rstrip("'\">,;)")
                    stream_urls.add(clean_url)

    # Check for video/source elements
    for source in soup.find_all(['source', 'video'], src=True):
        src = source.get('src', '')
        if src and ('m3u8' in src or 'm3u' in src or '.ts' in src):
            stream_urls.add(urljoin(url, src))

    return stream_urls


def check_for_api_endpoints(session):
    """Check common API endpoints that might return stream data."""
    api_paths = [
        '/api/channels',
        '/api/streams',
        '/api/playlist',
        '/api/live',
        '/channels.json',
        '/playlist.json',
        '/streams.json',
        '/live.json',
        '/api/v1/channels',
        '/api/v1/streams',
        '/channels',
        '/live',
        '/playlist',
        '/playlist.m3u',
        '/playlist.m3u8',
        '/channels.m3u',
        '/channels.m3u8',
        '/iptv/playlist.m3u',
        '/get_streams',
        '/stream_list',
    ]

    all_streams = {}
    for path in api_paths:
        url = urljoin(BASE_URL, path)
        try:
            response = session.get(
                url,
                headers=HEADERS,
                timeout=15,
                allow_redirects=True,
                verify=False
            )
            if response.status_code == 200:
                content = response.text
                streams = extract_stream_urls(content)
                if streams:
                    print(f"  Found {len(streams)} streams at {path}")
                    for stream in streams:
                        all_streams[stream] = f"Channel from {path}"

                # Try parsing as JSON
                try:
                    data = response.json()
                    json_str = json.dumps(data)
                    json_streams = extract_stream_urls(json_str)
                    if json_streams:
                        print(f"  Found {len(json_streams)} streams in JSON at {path}")
                        # Try to extract names from JSON
                        if isinstance(data, list):
                            for item in data:
                                if isinstance(item, dict):
                                    name = (item.get('name') or item.get('title') or
                                            item.get('channel_name') or item.get('channel') or '')
                                    url_val = (item.get('url') or item.get('stream_url') or
                                               item.get('link') or item.get('src') or '')
                                    if url_val:
                                        all_streams[url_val] = name or f"Channel from {path}"
                        elif isinstance(data, dict):
                            for key, value in data.items():
                                if isinstance(value, str) and ('m3u8' in value or '.ts' in value):
                                    all_streams[value] = key
                                elif isinstance(value, dict):
                                    url_val = (value.get('url') or value.get('stream_url') or
                                               value.get('link') or '')
                                    name = (value.get('name') or value.get('title') or key)
                                    if url_val:
                                        all_streams[url_val] = name
                except (json.JSONDecodeError, ValueError):
                    pass

                # Check if it's already an M3U playlist
                if '#EXTM3U' in content:
                    print(f"  Found M3U playlist at {path}")
                    lines = content.strip().split('\n')
                    current_name = ''
                    for line in lines:
                        line = line.strip()
                        if line.startswith('#EXTINF:'):
                            # Extract name from EXTINF line
                            parts = line.split(',', 1)
                            if len(parts) > 1:
                                current_name = parts[1].strip()
                        elif line and not line.startswith('#'):
                            all_streams[line] = current_name or "Unknown Channel"
                            current_name = ''

        except requests.exceptions.RequestException:
            continue

    return all_streams


def follow_redirect_for_stream(url, session):
    """Follow redirects to find the actual stream URL."""
    try:
        response = session.head(
            url,
            headers=HEADERS,
            timeout=15,
            allow_redirects=True,
            verify=False
        )
        final_url = response.url
        if final_url != url and ('m3u8' in final_url or '.ts' in final_url):
            return final_url
    except requests.exceptions.RequestException:
        pass
    return url


def generate_m3u_playlist(channels, timestamp):
    """Generate M3U playlist content."""
    lines = [
        '#EXTM3U',
        f'#PLAYLIST: 11Plus Live Streams',
        f'# Updated: {timestamp}',
        f'# Source: {BASE_URL}',
        f'# Auto-generated by GitHub Actions',
        f'# Total channels: {len(channels)}',
        '',
    ]

    for i, channel in enumerate(channels, 1):
        name = channel.get('name', f'Channel {i}')
        url = channel.get('url', '')
        group = channel.get('group', '11Plus')
        logo = channel.get('logo', '')

        if not url:
            continue

        # Build EXTINF line
        extinf = f'#EXTINF:-1'
        extinf += f' group-title="{group}"'
        if logo:
            extinf += f' tvg-logo="{logo}"'
        extinf += f' tvg-name="{name}"'
        extinf += f',{name}'

        lines.append(extinf)
        lines.append(url)
        lines.append('')

    return '\n'.join(lines)


def main():
    """Main function to fetch streams and generate playlist."""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    print(f"=" * 60)
    print(f"11Plus Stream Fetcher")
    print(f"Started at: {timestamp}")
    print(f"Source: {BASE_URL}")
    print(f"=" * 60)

    session = requests.Session()
    session.headers.update(HEADERS)

    all_channels = {}  # url -> channel info

    # Step 1: Check API endpoints
    print("\n[Step 1] Checking API endpoints...")
    api_streams = check_for_api_endpoints(session)
    for url, name in api_streams.items():
        all_channels[url] = {'name': name, 'url': url, 'group': '11Plus', 'source': 'api'}
    print(f"Total from API: {len(api_streams)}")

    # Step 2: Fetch main page
    print("\n[Step 2] Fetching main page...")
    channel_links, main_page_streams = fetch_main_page(session)

    for stream_url in main_page_streams:
        if stream_url not in all_channels:
            all_channels[stream_url] = {
                'name': f'Stream',
                'url': stream_url,
                'group': '11Plus',
                'source': 'main_page'
            }

    # Step 3: Visit channel/sub pages
    print(f"\n[Step 3] Visiting {len(channel_links)} sub-pages...")
    visited = set()
    for i, link_info in enumerate(channel_links):
        link_url = link_info['url']
        link_name = link_info['name']

        if link_url in visited or link_url == BASE_URL:
            continue
        visited.add(link_url)

        print(f"\n  [{i+1}/{len(channel_links)}] {link_name or link_url}")
        page_streams = fetch_channel_page(link_url, session)

        for stream_url in page_streams:
            if stream_url not in all_channels:
                channel_name = link_name if link_name else f'Channel {len(all_channels) + 1}'
                all_channels[stream_url] = {
                    'name': channel_name,
                    'url': stream_url,
                    'group': '11Plus',
                    'source': link_url
                }
            elif link_name and all_channels[stream_url]['name'].startswith('Stream'):
                all_channels[stream_url]['name'] = link_name

        # Small delay to be respectful
        time.sleep(1)

    # Step 4: If no streams found, try harder
    if not all_channels:
        print("\n[Step 4] No streams found, trying alternative methods...")

        # Try fetching page with different headers
        alt_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
            "Accept": "*/*",
        }
        try:
            response = requests.get(BASE_URL, headers=alt_headers, timeout=30, verify=False)
            if response.status_code == 200:
                streams = extract_stream_urls(response.text)
                for stream_url in streams:
                    all_channels[stream_url] = {
                        'name': 'Channel',
                        'url': stream_url,
                        'group': '11Plus',
                        'source': 'alt_fetch'
                    }
        except Exception as e:
            print(f"  Alternative fetch failed: {e}")

    # Step 5: Clean up and assign proper names
    print(f"\n[Step 5] Processing {len(all_channels)} streams...")
    channels_list = []
    for i, (url, info) in enumerate(all_channels.items(), 1):
        name = info.get('name', '').strip()
        if not name or name == 'Stream' or name.startswith('Channel from'):
            name = f'11Plus Channel {i}'

        channels_list.append({
            'name': name,
            'url': url,
            'group': info.get('group', '11Plus'),
            'logo': info.get('logo', ''),
            'source': info.get('source', ''),
        })

    # Sort channels by name
    channels_list.sort(key=lambda x: x['name'])

    # Step 6: Generate M3U playlist
    print(f"\n[Step 6] Generating M3U playlist with {len(channels_list)} channels...")
    m3u_content = generate_m3u_playlist(channels_list, timestamp)

    # Write M3U file
    with open(OUTPUT_M3U, 'w', encoding='utf-8') as f:
        f.write(m3u_content)
    print(f"  Written to {OUTPUT_M3U}")

    # Write JSON file (for debugging/reference)
    json_data = {
        'updated': timestamp,
        'source': BASE_URL,
        'total_channels': len(channels_list),
        'channels': channels_list
    }
    with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)
    print(f"  Written to {OUTPUT_JSON}")

    # Summary
    print(f"\n{'=' * 60}")
    print(f"SUMMARY")
    print(f"{'=' * 60}")
    print(f"Total channels found: {len(channels_list)}")
    print(f"Playlist file: {OUTPUT_M3U}")
    print(f"JSON file: {OUTPUT_JSON}")
    print(f"Completed at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    if channels_list:
        print(f"\nChannels:")
        for ch in channels_list:
            print(f"  - {ch['name']}: {ch['url'][:80]}...")
    else:
        print("\n⚠️  No streams were found. The site structure may have changed.")
        # Still write empty playlist so the file exists
        with open(OUTPUT_M3U, 'w', encoding='utf-8') as f:
            f.write(f'#EXTM3U\n# No streams found at {timestamp}\n# Source: {BASE_URL}\n')

    return len(channels_list)


if __name__ == '__main__':
    main()
