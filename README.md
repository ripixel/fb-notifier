# Facebook Page Notifier for Newark Parkrun

Monitor a Facebook page via RSS and receive push notifications when new posts appear. Designed for situations where you need to track Facebook updates without having a Facebook account.

## Features

- 📰 Converts Facebook page to RSS via FetchRSS
- 📱 Push notifications via [ntfy](https://ntfy.sh)
- 🔗 Tap notification to view original Facebook post
- 🖼️ First image from post shown in notification
- 🐳 Docker support for easy deployment
- ☁️ GCP Cloud Run ready

## Quick Start

### 1. Create FetchRSS Feed

1. Go to [FetchRSS](https://fetchrss.com)
2. Enter the Facebook page URL: `https://www.facebook.com/newarkparkrun`
3. Click "Create RSS"
4. Copy the generated RSS URL

### 2. Install ntfy App

1. Download [ntfy](https://ntfy.sh) on your phone (iOS/Android)
2. Subscribe to topic: `newark-parkrun-fb` (or your custom topic)

### 3. Configure

```bash
cp config.example.json config.json
```

Edit `config.json`:
```json
{
  "rss_url": "YOUR_FETCHRSS_URL_HERE",
  "ntfy_topic": "newark-parkrun-fb"
}
```

### 4. Run

**Option A: Python directly**
```bash
pip install -r requirements.txt
python notifier.py
```

**Option B: Docker**
```bash
docker compose up -d
```

## GCP Deployment

See [DEPLOY_GCP.md](./DEPLOY_GCP.md) for full instructions on deploying to Google Cloud.

### Quick GCP Setup (Cloud Run Jobs)

```bash
# Build and push
gcloud builds submit --tag gcr.io/YOUR_PROJECT/fb-notifier

# Create job (runs every 30 min)
gcloud run jobs create fb-notifier \
  --image gcr.io/YOUR_PROJECT/fb-notifier \
  --region us-central1

# Schedule
gcloud scheduler jobs create http fb-notifier-scheduler \
  --schedule="*/30 * * * *" \
  --uri="https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/YOUR_PROJECT/jobs/fb-notifier:run" \
  --http-method=POST \
  --oauth-service-account-email=YOUR_SERVICE_ACCOUNT
```

## How It Works

1. **FetchRSS** scrapes the public Facebook page and generates an RSS feed
2. **notifier.py** polls the RSS feed periodically
3. New posts are detected by comparing against previously seen posts
4. For each new post:
   - Push notification sent via ntfy with post text
   - First image attached to notification
   - Tap notification to open original Facebook post

## License

MIT
