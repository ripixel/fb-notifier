#!/usr/bin/env python3
"""
Facebook Page Notifier for Newark Parkrun
Uses Playwright headless browser to scrape public Facebook page posts.
Sends push notifications via ntfy for new posts.
"""

import json
import os
import sys
import logging
import re
import asyncio
import time
from pathlib import Path
from typing import Optional

import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class Config:
    """Configuration management."""

    def __init__(self, config_path: str = "config.json"):
        self.config_path = Path(config_path)
        self._load()

    def _load(self):
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {self.config_path}\n"
                "Copy config.example.json to config.json and fill in your values."
            )

        with open(self.config_path) as f:
            data = json.load(f)

        self.facebook_page = data.get("facebook_page", "newarkparkrun")
        self.instagram_page = data.get("instagram_page")
        self.ntfy_topic = data["ntfy_topic"]
        self.ntfy_server = data.get("ntfy_server", "https://ntfy.sh")
        self.seen_posts_file = Path(data.get("seen_posts_file", "./seen_posts.json"))
        self.cookies_file = Path(data.get("cookies_file", "./cookies.json"))
        self.instagram_cookies_file = Path(data.get("instagram_cookies_file", "./instagram_cookies.json"))


class SeenPosts:
    """Track posts we've already processed to avoid duplicate notifications."""

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.seen: set[str] = set()
        self._load()

    def _load(self):
        if self.filepath.exists():
            with open(self.filepath) as f:
                data = json.load(f)
                self.seen = set(data.get("seen_ids", []))

    def save(self):
        with open(self.filepath, 'w') as f:
            json.dump({"seen_ids": list(self.seen)}, f, indent=2)

    def is_seen(self, post_id: str) -> bool:
        return post_id in self.seen

    def mark_seen(self, post_id: str):
        self.seen.add(post_id)


def generate_post_id(post_url: str, text: str) -> Optional[str]:
    """Extract a stable post ID from a Facebook or Instagram URL."""
    # Instagram shortcode: /p/<shortcode>/
    match = re.search(r'/p/([A-Za-z0-9_-]{9,})/', post_url)
    if match:
        return f"ig_{match.group(1)}"

    # Facebook pfbid format
    match = re.search(r'/posts/(pfbid\w+)', post_url)
    if match:
        return match.group(1)

    # Facebook numeric post ID
    match = re.search(r'/posts/(\d+)', post_url)
    if match:
        return match.group(1)

    # Facebook story.php
    match = re.search(r'story_fbid=(\d+)', post_url)
    if match:
        return f"story_{match.group(1)}"

    logger.debug(f"No valid post ID found in URL: {post_url[:60]}...")
    return None


def sanitize_for_header(text: str) -> str:
    """Sanitize text for use in HTTP headers (ASCII only)."""
    replacements = {
        '\u2019': "'",
        '\u2018': "'",
        '\u201c': '"',
        '\u201d': '"',
        '\u2013': '-',
        '\u2014': '--',
        '\u2026': '...',
    }
    for unicode_char, ascii_char in replacements.items():
        text = text.replace(unicode_char, ascii_char)

    return text.encode('ascii', 'ignore').decode('ascii')


def send_ntfy_notification(config: Config, title: str, message: str, url: Optional[str] = None, image_url: Optional[str] = None):
    """Send a push notification via ntfy."""
    ntfy_url = f"{config.ntfy_server}/{config.ntfy_topic}"

    safe_title = sanitize_for_header(title[:256])

    headers = {
        "Title": safe_title,
        "Tags": "running,facebook,parkrun",
    }

    if url:
        headers["Click"] = url

    if image_url:
        headers["Attach"] = image_url

    try:
        response = requests.post(
            ntfy_url,
            data=message[:4096].encode('utf-8'),
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        logger.info(f"Notification sent: {safe_title}")

    except Exception as e:
        logger.error(f"Failed to send notification: {e}")
        raise


def load_cookies(cookies_path: Path) -> list[dict]:
    """Load cookies from a JSON file (standard browser-export format).

    Expects a list of cookie objects with at minimum 'name', 'value', 'domain'.
    Returns an empty list if the file doesn't exist.
    """
    if not cookies_path.exists():
        logger.info(f"No cookies file at {cookies_path} — proceeding unauthenticated")
        return []
    with open(cookies_path) as f:
        cookies = json.load(f)

    # Playwright requires sameSite to be "Strict", "Lax", or "None" (capitalised).
    # Browser export tools (e.g. Cookie-Editor) use "no_restriction" for None.
    _same_site_map = {"no_restriction": "None", "unspecified": "None"}
    for c in cookies:
        raw = c.get("sameSite")
        if raw is None:
            c["sameSite"] = "None"
        elif raw in _same_site_map:
            c["sameSite"] = _same_site_map[raw]

    logger.info(f"Loaded {len(cookies)} cookies from {cookies_path}")
    return cookies


_MOBILE_UA = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 '
    'Mobile/15E148 Safari/604.1'
)


def _extract_posts_from_html(html: str, page_name: str) -> list[dict]:
    """Parse mbasic Facebook HTML and return post dicts."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, 'html.parser')
    unique_posts: dict[str, dict] = {}

    for a in soup.find_all('a', href=True):
        href = a['href']

        # pfbid format
        m = re.search(r'/posts/(pfbid\w+)', href)
        if m:
            pid = m.group(1)
            if pid not in unique_posts:
                unique_posts[pid] = {
                    'post_url': f"https://www.facebook.com/{page_name}/posts/{pid}",
                    'anchor': a,
                }
            continue

        # Numeric post path
        m = re.search(rf'/{re.escape(page_name)}/posts/(\d+)', href)
        if m:
            pid = m.group(1)
            if pid not in unique_posts:
                unique_posts[pid] = {
                    'post_url': f"https://www.facebook.com/{page_name}/posts/{pid}",
                    'anchor': a,
                }
            continue

        # story.php format
        m = re.search(r'story_fbid=(\d+)', href)
        if m:
            pid = f"story_{m.group(1)}"
            page_id_m = re.search(r'[?&]id=(\d+)', href)
            if pid not in unique_posts:
                if page_id_m:
                    post_url = (
                        f"https://www.facebook.com/story.php"
                        f"?story_fbid={m.group(1)}&id={page_id_m.group(1)}"
                    )
                else:
                    post_url = f"https://www.facebook.com/{page_name}/posts/{m.group(1)}"
                unique_posts[pid] = {'post_url': post_url, 'anchor': a}

    logger.info(f"Found {len(unique_posts)} unique post links in HTML")

    posts = []
    for pid, info in list(unique_posts.items())[:10]:
        anchor = info['anchor']
        # Walk up to the nearest block-level container with some substance
        container = anchor
        for _ in range(5):
            parent = container.parent
            if parent is None:
                break
            container = parent
            text = container.get_text(separator=' ', strip=True)
            if len(text) > 50:
                break

        text = container.get_text(separator='\n', strip=True)
        img = container.find('img')
        image_url = img['src'] if img and img.get('src') else None

        logger.info(f"Post {pid[:20]}: {text[:60]}...")
        posts.append({
            'post_url': info['post_url'],
            'text': text[:1000],
            'image_url': image_url,
        })

    return posts


def scrape_with_requests(page_name: str, cookies: list[dict]) -> list[dict]:
    """Try fetching mbasic.facebook.com with a plain HTTP request (no browser)."""
    url = f"https://mbasic.facebook.com/{page_name}"
    logger.info(f"Attempting plain HTTP fetch: {url}")

    session = requests.Session()
    session.headers.update({
        'User-Agent': _MOBILE_UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
    })
    for c in cookies:
        session.cookies.set(
            c['name'], c['value'],
            domain=c.get('domain', '.facebook.com'),
            path=c.get('path', '/'),
        )

    resp = session.get(url, timeout=15, allow_redirects=True)
    logger.info(f"HTTP response: {resp.status_code}, final URL: {resp.url}")

    if 'login' in str(resp.url) or 'checkpoint' in str(resp.url):
        logger.warning("Plain HTTP hit login wall")
        return []

    return _extract_posts_from_html(resp.text, page_name)


async def scrape_with_playwright(page_name: str, cookies: list[dict]) -> list[dict]:
    """Fallback: use Playwright headless browser against mbasic.facebook.com."""
    from playwright.async_api import async_playwright

    url = f"https://mbasic.facebook.com/{page_name}"
    posts = []
    logger.info(f"Attempting Playwright fetch: {url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled'],
        )
        context = await browser.new_context(
            viewport={'width': 390, 'height': 844},
            locale='en-US',
            user_agent=_MOBILE_UA,
        )
        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()

        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(2000)
            logger.info(f"Playwright page URL: {page.url}")

            if 'login' in page.url or 'checkpoint' in page.url:
                logger.warning(f"Playwright hit login wall: {page.url}")
                return posts

            html = await page.content()
            posts = _extract_posts_from_html(html, page_name)

        except Exception as e:
            logger.error(f"Playwright scraping error: {e}")
        finally:
            await browser.close()

    return posts


def scrape_facebook_page(page_name: str, cookies: list[dict]) -> list[dict]:
    """Try plain HTTP first; fall back to Playwright if blocked."""
    posts = scrape_with_requests(page_name, cookies)
    if posts:
        return posts

    logger.info("Plain HTTP returned no posts, falling back to Playwright")
    return asyncio.run(scrape_with_playwright(page_name, cookies))


def _build_instagram_session(cookies: list[dict]) -> requests.Session:
    """Build a requests Session pre-loaded with Instagram cookies and headers."""
    session = requests.Session()
    csrf_token = ""
    for c in cookies:
        session.cookies.set(
            c['name'], c['value'],
            domain=c.get('domain', '.instagram.com'),
            path=c.get('path', '/'),
        )
        if c['name'] == 'csrftoken':
            csrf_token = c['value']

    session.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/125.0.0.0 Safari/537.36'
        ),
        'Accept': '*/*',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'X-IG-App-ID': '936619743392459',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://www.instagram.com',
        'Referer': 'https://www.instagram.com/',
    })
    if csrf_token:
        session.headers['X-CSRFToken'] = csrf_token
    return session


def _parse_instagram_api_response(data: dict) -> list[dict]:
    """Extract post list from web_profile_info API JSON."""
    edges = (
        data.get("data", {})
            .get("user", {})
            .get("edge_owner_to_timeline_media", {})
            .get("edges", [])
    )
    posts = []
    for edge in edges[:10]:
        node = edge.get("node", {})
        shortcode = node.get("shortcode")
        if not shortcode:
            continue
        caption = ""
        cap_edges = node.get("edge_media_to_caption", {}).get("edges", [])
        if cap_edges:
            caption = cap_edges[0].get("node", {}).get("text", "")
        posts.append({
            'post_url': f"https://www.instagram.com/p/{shortcode}/",
            'text': caption[:1000],
            'image_url': node.get("thumbnail_src") or node.get("display_url"),
        })
        logger.info(f"Post {shortcode}: {caption[:60]}...")
    return posts


async def _scrape_instagram_playwright(username: str, cookies: list[dict]) -> list[dict]:
    """Playwright fallback: render instagram.com in a real browser and extract post links."""
    from playwright.async_api import async_playwright

    url = f"https://www.instagram.com/{username}/"
    posts = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled'],
        )
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 800},
            locale='en-US',
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/125.0.0.0 Safari/537.36'
            ),
        )

        if cookies:
            pw_cookies = []
            for c in cookies:
                pw_cookie = {
                    'name': c['name'],
                    'value': c['value'],
                    'domain': c.get('domain', '.instagram.com'),
                    'path': c.get('path', '/'),
                    'secure': c.get('secure', True),
                    'httpOnly': c.get('httpOnly', False),
                    'sameSite': c.get('sameSite', 'None'),
                }
                pw_cookies.append(pw_cookie)
            await context.add_cookies(pw_cookies)

        page = await context.new_page()
        try:
            logger.info(f"Playwright Instagram: navigating to {url}")
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(3000)

            current_url = page.url
            logger.info(f"Playwright Instagram: final URL: {current_url}")

            if 'accounts/login' in current_url or 'challenge' in current_url:
                logger.warning(f"Playwright Instagram: hit login wall: {current_url}")
                return posts

            # Wait for post links to appear in the React-rendered DOM
            try:
                await page.wait_for_selector('a[href*="/p/"]', timeout=10000)
            except Exception:
                logger.warning("Playwright Instagram: no post links appeared within timeout")

            links = await page.evaluate('''() => {
                const anchors = document.querySelectorAll('a[href*="/p/"]');
                const seen = new Set();
                const results = [];
                for (const a of anchors) {
                    const href = a.getAttribute('href');
                    const match = href.match(/\\/p\\/([A-Za-z0-9_-]+)\\//);
                    if (match && !seen.has(match[1])) {
                        seen.add(match[1]);
                        results.push(match[1]);
                    }
                    if (results.length >= 10) break;
                }
                return results;
            }''')

            logger.info(f"Playwright Instagram: found {len(links)} post shortcodes")
            for sc in links:
                posts.append({
                    'post_url': f"https://www.instagram.com/p/{sc}/",
                    'text': '',
                    'image_url': None,
                })

        except Exception as e:
            logger.error(f"Playwright Instagram error: {e}")
        finally:
            await browser.close()

    return posts


def scrape_instagram(username: str, cookies: list[dict]) -> list[dict]:
    """Fetch recent posts from an Instagram profile.

    Tries the internal web_profile_info JSON API first (fast, has captions).
    Falls back to Playwright rendering www.instagram.com if the API is blocked.
    """
    if cookies:
        logger.info(f"Using {len(cookies)} Instagram cookies")

    session = _build_instagram_session(cookies)

    # Primary: i.instagram.com internal API
    api_url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    logger.info(f"Trying Instagram API: {api_url}")
    try:
        resp = session.get(api_url, timeout=15)
        logger.info(f"Instagram API response: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            posts = _parse_instagram_api_response(data)
            logger.info(f"API returned {len(posts)} posts")
            return posts
        logger.warning(f"Instagram API returned {resp.status_code} — falling back to Playwright")
    except Exception as e:
        logger.warning(f"Instagram API request failed: {e} — falling back to Playwright")

    # Fallback: Playwright rendering of www.instagram.com
    return asyncio.run(_scrape_instagram_playwright(username, cookies))


def process_page(config: Config, seen_posts: SeenPosts):
    """Scrape the configured source (Instagram or Facebook) and send notifications."""
    if config.instagram_page:
        logger.info(f"Checking Instagram page: {config.instagram_page}")
        ig_cookies = load_cookies(config.instagram_cookies_file)
        try:
            posts = scrape_instagram(config.instagram_page, ig_cookies)
        except Exception as e:
            logger.error(f"Failed to scrape Instagram page: {e}")
            raise
    else:
        logger.info(f"Checking Facebook page: {config.facebook_page}")
        cookies = load_cookies(config.cookies_file)
        try:
            posts = scrape_facebook_page(config.facebook_page, cookies)
        except Exception as e:
            logger.error(f"Failed to scrape Facebook page: {e}")
            raise

    if not posts:
        logger.info("No posts found on page")
        return

    logger.info(f"Found {len(posts)} posts to check")
    new_posts = 0

    for post in reversed(posts):
        post_id = generate_post_id(post['post_url'], post['text'])

        if post_id is None:
            logger.debug(f"Skipping post without valid ID: {post['post_url'][:50]}...")
            continue

        if seen_posts.is_seen(post_id):
            logger.debug(f"Already seen post: {post_id}")
            continue

        # For Instagram we may have no caption — still notify, just with the URL
        first_line = post['text'].split('\n')[0][:50] if post['text'] else 'New post'
        title = f"Newark Parkrun: {first_line}"
        message = post['text'][:500] + ("..." if len(post['text']) > 500 else "") if post['text'] else post['post_url']

        logger.info(f"New post found: {post['post_url']}")

        send_ntfy_notification(
            config,
            title=title,
            message=message,
            url=post['post_url'],
            image_url=post.get('image_url'),
        )

        seen_posts.mark_seen(post_id)
        new_posts += 1

    seen_posts.save()
    logger.info(f"Processed {new_posts} new posts")


def main():
    """Main entry point."""
    config_path = os.environ.get("FB_NOTIFIER_CONFIG", "config.json")
    run_once = "--once" in sys.argv

    try:
        config = Config(config_path)
        seen_posts = SeenPosts(config.seen_posts_file)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    if run_once:
        try:
            process_page(config, seen_posts)
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
        return

    while True:
        try:
            process_page(config, seen_posts)
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")

        logger.info("Sleeping 30 minutes until next check...")
        time.sleep(30 * 60)


if __name__ == "__main__":
    main()
