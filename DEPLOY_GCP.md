# Deploying to Google Cloud Platform

This guide covers deploying the Facebook notifier to GCP using Cloud Run Jobs with Cloud Scheduler.

## Prerequisites

- Google Cloud account with billing enabled
- `gcloud` CLI installed and authenticated
- A GCP project created

## Architecture

```
Cloud Scheduler (every 30 min)
    ↓ triggers
Cloud Run Job (runs notifier.py)
    ↓ reads
Cloud Storage (config + seen posts)
    ↓ sends
ntfy.sh (push notification)
```

## Step 1: Create Storage Bucket

Store config and state files in Cloud Storage for persistence:

```bash
export PROJECT_ID=your-project-id
export BUCKET_NAME=${PROJECT_ID}-fb-notifier

# Create bucket
gcloud storage buckets create gs://${BUCKET_NAME} --location=europe-west2

# Upload config
gcloud storage cp config.json gs://${BUCKET_NAME}/config.json
```

## Step 2: Build Container Image

```bash
# Enable required APIs
gcloud services enable cloudbuild.googleapis.com run.googleapis.com

# Build and push to Artifact Registry
gcloud builds submit --tag gcr.io/${PROJECT_ID}/fb-notifier
```

## Step 3: Create Cloud Run Job

```bash
gcloud run jobs create fb-notifier \
  --image gcr.io/${PROJECT_ID}/fb-notifier \
  --region europe-west2 \
  --memory 256Mi \
  --task-timeout 5m \
  --set-env-vars FB_NOTIFIER_CONFIG=/app/config/config.json
```

## Step 4: Setup Cloud Scheduler

```bash
# Enable Cloud Scheduler API
gcloud services enable cloudscheduler.googleapis.com

# Create scheduler job (runs every 30 minutes)
gcloud scheduler jobs create http fb-notifier-scheduler \
  --location europe-west2 \
  --schedule "*/30 * * * *" \
  --uri "https://europe-west2-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/fb-notifier:run" \
  --http-method POST \
  --oauth-service-account-email ${PROJECT_ID}@appspot.gserviceaccount.com
```

## Step 5: Grant Permissions

```bash
# Allow scheduler to invoke Cloud Run jobs
gcloud run jobs add-iam-policy-binding fb-notifier \
  --region europe-west2 \
  --member serviceAccount:${PROJECT_ID}@appspot.gserviceaccount.com \
  --role roles/run.invoker
```

## Alternative: Cloud Storage Integration

For persistent state (seen_posts.json), modify the container to sync with GCS:

### Updated Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install gcloud CLI
RUN apt-get update && apt-get install -y curl gnupg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] http://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | apt-key --keyring /usr/share/keyrings/cloud.google.gpg add - && \
    apt-get update && apt-get install -y google-cloud-cli && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY notifier.py run.sh ./
RUN chmod +x run.sh

CMD ["./run.sh"]
```

### run.sh

```bash
#!/bin/bash
set -e

# Download state from GCS
gsutil cp gs://${BUCKET_NAME}/config.json /app/config/config.json || true
gsutil cp gs://${BUCKET_NAME}/seen_posts.json /app/seen_posts.json || true

# Run notifier
python notifier.py

# Upload updated state
gsutil cp /app/seen_posts.json gs://${BUCKET_NAME}/seen_posts.json
```

## Cost Estimate

| Resource | Usage | Monthly Cost |
|----------|-------|--------------|
| Cloud Run Jobs | ~1440 invocations | ~$0.00 (free tier) |
| Cloud Scheduler | 1 job | ~$0.10 |
| Cloud Storage | <1MB | ~$0.00 |
| **Total** | | **~$0.10/month** |

## Monitoring

View job execution logs:
```bash
gcloud run jobs executions list --job fb-notifier --region europe-west2
gcloud logging read "resource.type=cloud_run_job" --limit 50
```

## Manual Test

Trigger the job manually:
```bash
gcloud run jobs execute fb-notifier --region europe-west2
```
