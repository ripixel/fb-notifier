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
    """Extract the post ID from a Facebook permalink URL.

    Only accepts legitimate pfbid-prefixed IDs. Returns None if no valid ID found.
    This prevents capturing photo IDs, story IDs, or generating unreliable text hashes.
    """
    # Only match pfbid-prefixed post IDs (the modern Facebook standard)
    match = re.search(r'/posts/(pfbid\w+)', post_url)
    if match:
        post_id = match.group(1)
        logger.info(f"Found post ID: {post_id[:20]}...")
        return post_id

    # No valid post ID found - this is not a legitimate post permalink
    logger.debug(f"No pfbid found in URL: {post_url[:60]}...")
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
                # Click "Allow all cookies" button if present
                allow_cookies = page.get_by_role("button", name="Allow all cookies")
                if await allow_cookies.count() > 0:
                    await allow_cookies.click()
                    logger.info("Clicked 'Allow all cookies' button")
                    await page.wait_for_timeout(2000)

                # Also close any other modals
                close_buttons = await page.query_selector_all('[aria-label="Close"]')
                for btn in close_buttons:
                    await btn.click()
                    await page.wait_for_timeout(500)
            except Exception as e:
                logger.debug(f"No popup to close: {e}")

            # Scroll down multiple times to load more posts
            for scroll_num in range(3):
                await page.evaluate(f"window.scrollTo(0, {(scroll_num + 1) * 500})")
                await page.wait_for_timeout(1500)

            # Step 1: Find all unique post permalinks on the page
            # Look for <a> tags whose href starts with the posts URL
            posts_url_prefix = f"https://www.facebook.com/{page_name}/posts"
            all_links = await page.query_selector_all('a[href]')
            unique_hrefs = set()

            for link in all_links:
                href = await link.get_attribute('href')
                if href and href.startswith(posts_url_prefix):
                    # Clean the URL (remove query params)
                    clean_href = href.split('?')[0]
                    unique_hrefs.add(clean_href)

            logger.info(f"Found {len(unique_hrefs)} unique post links")

            if not unique_hrefs:
                logger.warning("No post links found - Facebook may have changed their HTML")
                return posts

            # Step 2: For each unique post link, find its article and extract content
            for post_url in list(unique_hrefs)[:10]:  # Limit to 10 posts
                try:
                    # Find the link element for this URL
                    link_elem = await page.query_selector(f'a[href*="{post_url.split("/posts/")[1][:30]}"]')
                    if not link_elem:
                        continue

                    # Navigate up to find the article container
                    article = await link_elem.evaluate_handle('el => el.closest("[role=\\"article\\"]")')
                    if not article:
                        continue

                    # Get post text - look for the main message content
                    text = ""
                    text_elem = await article.query_selector('[data-ad-preview="message"]')
                    if not text_elem:
                        # Try getting text from dir="auto" elements that aren't just timestamps
                        text_elems = await article.query_selector_all('[dir="auto"]')
                        for te in text_elems:
                            candidate = await te.inner_text()
                            if len(candidate) > 20:  # Skip short timestamp texts
                                text = candidate
                                break
                    else:
                        text = await text_elem.inner_text()

                    if len(text) < 10:
                        continue

                    # Get first image from the post
                    image_url = None
                    img_elem = await article.query_selector('img[src*="scontent"]')
                    if img_elem:
                        image_url = await img_elem.get_attribute('src')

                    # Ensure full URL
                    if not post_url.startswith('http'):
                        post_url = f"https://www.facebook.com{post_url}"

                    logger.info(f"Extracted post: {post_url[-40:]} - {text[:30]}...")

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

        # Skip posts without valid pfbid (e.g., photos, shared content)
        if post_id is None:
            logger.debug(f"Skipping post without valid pfbid: {post['post_url'][:50]}...")
            continue

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
