#!/usr/bin/env python3
"""
Facebook Page Notifier for Newark Parkrun
Uses Playwright headless browser to scrape public Facebook page posts.
Sends push notifications via ntfy for new posts.
"""

import json
import os
import sys
import hashlib
import logging
import re
import asyncio
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


def generate_post_id(post_url: str, text: str) -> str:
    """Generate a unique ID for a post based on its content.

    Uses text content hash as primary identifier since Facebook post URLs
    can vary between scrapes (different query params, redirect URLs, etc).
    """
    # Primary: Use first 200 chars of text content as the stable identifier
    # This is more reliable than URLs which can change
    if text and len(text) >= 20:
        text_id = hashlib.md5(text[:200].encode()).hexdigest()
        logger.debug(f"Generated text-based post ID: {text_id[:12]}... from: {text[:50]}")
        return text_id

    # Fallback: Try to extract post ID from URL
    for pattern in [
        r'/posts/pfbid(\w+)',
        r'/posts/(\d+)',
        r'story_fbid=(\d+)',
        r'fbid=(\d+)',
        r'/permalink/(\d+)',
    ]:
        match = re.search(pattern, post_url)
        if match:
            url_id = match.group(1)
            logger.debug(f"Generated URL-based post ID: {url_id}")
            return url_id

    # Last resort: hash URL + text
    content = f"{post_url}{text[:100]}"
    fallback_id = hashlib.md5(content.encode()).hexdigest()
    logger.debug(f"Generated fallback post ID: {fallback_id[:12]}...")
    return fallback_id


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
    Returns list of dicts with: post_url, text, image_url
    """
    from playwright.async_api import async_playwright

    url = f"https://www.facebook.com/{page_name}"
    posts = []

    logger.info(f"Opening Facebook page with headless browser: {url}")

    async with async_playwright() as p:
        # Use Chromium headless
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 800},
            locale='en-US',
        )
        page = await context.new_page()

        try:
            # Navigate and wait for content
            await page.goto(url, wait_until='networkidle', timeout=30000)

            # Wait a bit for dynamic content to load
            await page.wait_for_timeout(3000)

            # Close any popups (login prompts, cookie banners)
            try:
                close_buttons = await page.query_selector_all('[aria-label="Close"], [data-testid="cookie-policy-manage-dialog-accept-button"]')
                for btn in close_buttons:
                    await btn.click()
                    await page.wait_for_timeout(500)
            except:
                pass

            # Scroll down to load more posts
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await page.wait_for_timeout(2000)

            # Extract posts - Facebook uses role="article" for posts
            post_elements = await page.query_selector_all('[role="article"]')
            logger.info(f"Found {len(post_elements)} post elements")

            for post_elem in post_elements[:10]:  # Limit to recent posts
                try:
                    # Get post text
                    text_elem = await post_elem.query_selector('[data-ad-preview="message"]')
                    if not text_elem:
                        text_elem = await post_elem.query_selector('[dir="auto"]')

                    text = ""
                    if text_elem:
                        text = await text_elem.inner_text()

                    if len(text) < 10:
                        continue

                    # Get post link
                    post_url = f"https://www.facebook.com/{page_name}"
                    link_elems = await post_elem.query_selector_all('a[href*="/posts/"], a[href*="permalink"]')
                    for link in link_elems:
                        href = await link.get_attribute('href')
                        if href:
                            if href.startswith('/'):
                                post_url = f"https://www.facebook.com{href}"
                            else:
                                post_url = href
                            break

                    # Get first image
                    image_url = None
                    img_elem = await post_elem.query_selector('img[src*="scontent"]')
                    if img_elem:
                        image_url = await img_elem.get_attribute('src')

                    posts.append({
                        'post_url': post_url.split('?')[0],  # Clean URL
                        'text': text[:1000],
                        'image_url': image_url
                    })

                except Exception as e:
                    logger.debug(f"Failed to parse post element: {e}")
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

        if seen_posts.is_seen(post_id):
            logger.debug(f"Already seen post: {post_id}")
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

    try:
        config = Config(config_path)
        seen_posts = SeenPosts(config.seen_posts_file)
        process_facebook_page(config, seen_posts)

    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
