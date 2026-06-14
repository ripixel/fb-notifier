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
        self.ntfy_topic = data["ntfy_topic"]
        self.ntfy_server = data.get("ntfy_server", "https://ntfy.sh")
        self.seen_posts_file = Path(data.get("seen_posts_file", "./seen_posts.json"))


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
    """Extract a stable post ID from a Facebook permalink URL."""
    # Modern pfbid format
    match = re.search(r'/posts/(pfbid\w+)', post_url)
    if match:
        post_id = match.group(1)
        logger.info(f"Found pfbid post ID: {post_id[:20]}...")
        return post_id

    # Numeric post ID in path (mbasic sometimes uses these)
    match = re.search(r'/posts/(\d+)', post_url)
    if match:
        post_id = match.group(1)
        logger.info(f"Found numeric post ID: {post_id[:20]}...")
        return post_id

    # story_fbid format (mbasic story.php links)
    match = re.search(r'story_fbid=(\d+)', post_url)
    if match:
        post_id = f"story_{match.group(1)}"
        logger.info(f"Found story_fbid post ID: {post_id[:20]}...")
        return post_id

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


async def scrape_facebook_with_playwright(page_name: str) -> list[dict]:
    """
    Scrape posts from Facebook page using Playwright headless browser.
    Uses mbasic.facebook.com which is server-rendered and avoids JS-based bot detection.
    Returns list of dicts with: post_url, text, image_url
    """
    from playwright.async_api import async_playwright

    url = f"https://mbasic.facebook.com/{page_name}"
    posts = []

    logger.info(f"Opening Facebook page: {url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled'],
        )
        context = await browser.new_context(
            viewport={'width': 390, 'height': 844},
            locale='en-US',
            user_agent=(
                'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) '
                'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 '
                'Mobile/15E148 Safari/604.1'
            ),
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(2000)

            logger.info(f"Page loaded. URL: {page.url}")

            if 'login' in page.url or 'checkpoint' in page.url:
                logger.warning(f"Redirected to login/checkpoint: {page.url}")
                all_links = await page.query_selector_all('a[href]')
                sample = [await l.get_attribute('href') or '' for l in all_links[:20]]
                logger.warning(f"Sample hrefs: {sample}")
                return posts

            # Collect all post links — mbasic uses /posts/pfbid..., /posts/12345,
            # and story.php?story_fbid=... formats
            all_links = await page.query_selector_all('a[href]')
            unique_posts: dict[str, str] = {}  # post_id -> canonical www URL

            for link in all_links:
                href = await link.get_attribute('href') or ''

                # pfbid format: /newarkparkrun/posts/pfbid...
                m = re.search(r'/posts/(pfbid\w+)', href)
                if m:
                    pid = m.group(1)
                    unique_posts[pid] = f"https://www.facebook.com/{page_name}/posts/{pid}"
                    continue

                # Numeric post path: /newarkparkrun/posts/12345
                m = re.search(rf'/{re.escape(page_name)}/posts/(\d+)', href)
                if m:
                    pid = m.group(1)
                    unique_posts[pid] = f"https://www.facebook.com/{page_name}/posts/{pid}"
                    continue

                # story.php format
                m = re.search(r'story_fbid=(\d+)', href)
                if m:
                    pid = f"story_{m.group(1)}"
                    page_id_m = re.search(r'[?&]id=(\d+)', href)
                    if page_id_m:
                        unique_posts[pid] = (
                            f"https://www.facebook.com/story.php"
                            f"?story_fbid={m.group(1)}&id={page_id_m.group(1)}"
                        )
                    else:
                        unique_posts[pid] = f"https://www.facebook.com/{page_name}/posts/{m.group(1)}"
                    continue

            logger.info(f"Found {len(unique_posts)} unique post links")

            if not unique_posts:
                page_title = await page.title()
                logger.warning(f"No post links found. Title: '{page_title}', URL: {page.url}")
                sample = [await l.get_attribute('href') or '' for l in all_links[:20]]
                logger.warning(f"Sample hrefs: {sample}")
                return posts

            # Extract text and image for each post by finding the surrounding block
            for pid, post_url in list(unique_posts.items())[:10]:
                try:
                    # Find the anchor for this post
                    fragment = pid if pid.startswith('pfbid') else pid.replace('story_', '')
                    link_elem = await page.query_selector(f'a[href*="{fragment[:30]}"]')

                    text = ""
                    image_url = None

                    if link_elem:
                        # Walk up to the nearest block that contains the post body.
                        # mbasic wraps each post in a <div> with an id like "MCompositePost..."
                        container = await link_elem.evaluate_handle(
                            'el => el.closest("div[id]") || el.parentElement'
                        )
                        if container:
                            text = (await container.inner_text() or "").strip()
                            img = await container.query_selector('img')
                            if img:
                                image_url = await img.get_attribute('src')

                    logger.info(f"Extracted post: {pid[:20]} - {text[:50]}...")

                    posts.append({
                        'post_url': post_url,
                        'text': text[:1000],
                        'image_url': image_url
                    })

                except Exception as e:
                    logger.debug(f"Failed to parse post: {e}")
                    continue

        except Exception as e:
            logger.error(f"Playwright scraping error: {e}")
        finally:
            await browser.close()

    return posts


def process_facebook_page(config: Config, seen_posts: SeenPosts):
    """Scrape Facebook page and process new posts."""
    logger.info(f"Checking Facebook page: {config.facebook_page}")

    try:
        posts = asyncio.run(scrape_facebook_with_playwright(config.facebook_page))
    except Exception as e:
        logger.error(f"Failed to scrape Facebook page: {e}")
        raise

    if not posts:
        logger.info("No posts found on page")
        return

    logger.info(f"Found {len(posts)} posts to check")
    new_posts = 0

    # Process in reverse (oldest first) for chronological notifications
    for post in reversed(posts):
        post_id = generate_post_id(post['post_url'], post['text'])

        if post_id is None:
            logger.debug(f"Skipping post without valid ID: {post['post_url'][:50]}...")
            continue

        if seen_posts.is_seen(post_id):
            logger.debug(f"Already seen post: {post_id}")
            continue

        if len(post['text']) < 10:
            logger.debug(f"Marking post seen without notification (no text): {post_id[:20]}...")
            seen_posts.mark_seen(post_id)
            continue

        # Create notification
        first_line = post['text'].split('\n')[0][:50] if post['text'] else 'New Post'
        title = f"Newark Parkrun: {first_line}"
        message = post['text'][:500] + ("..." if len(post['text']) > 500 else "")

        logger.info(f"New post found: {first_line}")

        send_ntfy_notification(
            config,
            title=title,
            message=message,
            url=post['post_url'],
            image_url=post.get('image_url')
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
            process_facebook_page(config, seen_posts)
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
        return

    while True:
        try:
            process_facebook_page(config, seen_posts)
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")

        logger.info("Sleeping 30 minutes until next check...")
        time.sleep(30 * 60)


if __name__ == "__main__":
    main()
