# Facebook Page Notifier for Newark Parkrun

Monitor a Facebook page directly and receive push notifications when new posts appear. Designed for situations where you need to track Facebook updates without having a Facebook account.

## Features

- 📰 **Direct scraping** - No RSS middleman, polls as frequently as you want (every 10-15 min recommended)
- 🖥️ **Headless browser** - Uses Playwright to render the full Facebook page, bypassing anti-scraping measures
- 📱 Push notifications via [ntfy](https://ntfy.sh)
- 🔗 Tap notification to view original Facebook post
- 🖼️ First image from post shown in notification
- 🐳 Docker support for easy deployment
- ☁️ GCP Cloud Run ready

## Quick Start

### 1. Install ntfy App

1. Download [ntfy](https://ntfy.sh) on your phone (iOS/Android)
2. Subscribe to topic: `newark-parkrun-fb` (or your custom topic)

### 2. Install Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure

```bash
cp config.example.json config.json
```

Edit `config.json`:
```json
{
  "facebook_page": "newarkparkrun",
  "ntfy_topic": "newark-parkrun-fb"
}
```

| Setting | Description |
|---------|-------------|
| `facebook_page` | The Facebook page name (from the URL) |
| `ntfy_topic` | Your ntfy topic name |
| `ntfy_server` | Optional, defaults to `https://ntfy.sh` |
| `seen_posts_file` | Optional, defaults to `./seen_posts.json` |

### 4. Run

**Option A: Python directly**
```bash
python notifier.py
```

**Option B: Docker**
```bash
docker compose up -d
```

## Polling Frequency

Unlike FetchRSS (which updates every 24 hours 💀), this scrapes directly so you can poll as often as you like. Recommended: **every 10-15 minutes** to balance freshness vs. not hammering Facebook.

Set up a cron job:
```bash
# Every 10 minutes
*/10 * * * * cd /path/to/fb-notifier && /path/to/python notifier.py >> /var/log/fb-notifier.log 2>&1
```

## GCP Deployment

See [DEPLOY_GCP.md](./DEPLOY_GCP.md) for full instructions on deploying to Google Cloud.

### Quick GCP Setup (Cloud Run Jobs)

```bash
# Build and push
gcloud builds submit --tag gcr.io/YOUR_PROJECT/fb-notifier

# Create job (runs every 10 min)
gcloud run jobs create fb-notifier \
  --image gcr.io/YOUR_PROJECT/fb-notifier \
  --region us-central1 \
  --memory 1Gi

# Schedule (every 10 minutes)
gcloud scheduler jobs create http fb-notifier-scheduler \
  --schedule="*/10 * * * *" \
  --uri="https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/YOUR_PROJECT/jobs/fb-notifier:run" \
  --http-method=POST \
  --oauth-service-account-email=YOUR_SERVICE_ACCOUNT
```

**Note:** Playwright requires more memory than simple HTTP requests. Use at least 1Gi for Cloud Run.

## How It Works

1. **Playwright** launches a headless Chromium browser
2. Navigates to the public Facebook page and waits for content to load
3. Extracts posts using DOM selectors
4. New posts detected by comparing against previously seen posts
5. For each new post:
   - Push notification sent via ntfy with post text
   - First image attached to notification
   - Tap notification to open original Facebook post

## Troubleshooting

**"Failed to scrape Facebook page"**
- Facebook may have temporarily blocked requests. Wait a few minutes and try again.
- Ensure the page name is correct (use the URL slug, e.g., `newarkparkrun` not the full URL)
- Check that Playwright browsers are installed: `playwright install chromium`

**No notifications received**
- Check you're subscribed to the correct ntfy topic
- Run manually to see logs: `python notifier.py`

**"browserType.launch: Executable doesn't exist"**
- Run `playwright install chromium` to download the browser

**Memory issues on Cloud Run**
- Increase memory allocation to at least 1Gi

## License

MIT
