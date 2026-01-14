#!/usr/bin/env python3
"""
Facebook Page Notifier for Newark Parkrun
Monitors an RSS feed (generated from Facebook) and sends push notifications via ntfy.
"""

import json
import os
import sys
import hashlib
import logging
from pathlib import Path
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

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

        self.rss_url = data["rss_url"]
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


def generate_post_id(entry) -> str:
    """Generate a unique ID for a post based on its content."""
    if hasattr(entry, 'id') and entry.id:
        return entry.id

    content = f"{getattr(entry, 'link', '')}{getattr(entry, 'title', '')}"
    return hashlib.md5(content.encode()).hexdigest()


def extract_images(html_content: str) -> list[str]:
    """Extract image URLs from HTML content."""
    soup = BeautifulSoup(html_content, 'html.parser')
    images = []

    for img in soup.find_all('img'):
        src = img.get('src')
        if src and not src.startswith('data:'):
            images.append(src)

    return images


def extract_text(html_content: str) -> str:
    """Extract clean text from HTML content."""
    soup = BeautifulSoup(html_content, 'html.parser')
    return soup.get_text(separator='\n', strip=True)


def send_ntfy_notification(config: Config, title: str, message: str, url: Optional[str] = None, image_url: Optional[str] = None):
    """Send a push notification via ntfy."""
    ntfy_url = f"{config.ntfy_server}/{config.ntfy_topic}"

    headers = {
        "Title": title[:256],  # ntfy title limit
        "Tags": "running,facebook,parkrun",
    }

    if url:
        headers["Click"] = url

    if image_url:
        headers["Attach"] = image_url

    try:
        response = requests.post(
            ntfy_url,
            data=message[:4096].encode('utf-8'),  # ntfy message limit
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        logger.info(f"Notification sent: {title}")

    except Exception as e:
        logger.error(f"Failed to send notification: {e}")
        raise


def process_feed(config: Config, seen_posts: SeenPosts):
    """Fetch and process the RSS feed."""
    logger.info(f"Fetching feed: {config.rss_url}")

    feed = feedparser.parse(config.rss_url)

    if feed.bozo:
        logger.warning(f"Feed parsing warning: {feed.bozo_exception}")

    if not feed.entries:
        logger.info("No entries in feed")
        return

    new_posts = 0

    for entry in feed.entries:
        post_id = generate_post_id(entry)

        if seen_posts.is_seen(post_id):
            continue

        logger.info(f"New post found: {getattr(entry, 'title', 'Untitled')[:50]}")

        # Extract content
        content_html = ""
        if hasattr(entry, 'content') and entry.content:
            content_html = entry.content[0].get('value', '')
        elif hasattr(entry, 'summary'):
            content_html = entry.summary
        elif hasattr(entry, 'description'):
            content_html = entry.description

        text = extract_text(content_html)
        images = extract_images(content_html)

        # Send notification
        title = f"🏃 Newark Parkrun: {getattr(entry, 'title', 'New Post')[:50]}"
        message = text[:500] + ("..." if len(text) > 500 else "")

        send_ntfy_notification(
            config,
            title=title,
            message=message,
            url=getattr(entry, 'link', None),
            image_url=images[0] if images else None
        )

        # Mark as seen
        seen_posts.mark_seen(post_id)
        new_posts += 1

    # Save seen posts
    seen_posts.save()

    logger.info(f"Processed {new_posts} new posts")


def main():
    """Main entry point."""
    config_path = os.environ.get("FB_NOTIFIER_CONFIG", "config.json")

    try:
        config = Config(config_path)
        seen_posts = SeenPosts(config.seen_posts_file)
        process_feed(config, seen_posts)

    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

