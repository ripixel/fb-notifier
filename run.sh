#!/bin/bash
set -e

BUCKET="gs://newark-parkrun-fb-notifier-state"

# Download config and state from GCS
echo "Downloading config from GCS..."
gsutil cp ${BUCKET}/config.json /app/config.json

echo "Downloading seen_posts from GCS (if exists)..."
gsutil cp ${BUCKET}/seen_posts.json /app/seen_posts.json 2>/dev/null || echo '{"seen_ids":[]}' > /app/seen_posts.json

# Run notifier
echo "Running notifier..."
python notifier.py

# Upload updated state back to GCS
echo "Uploading seen_posts to GCS..."
gsutil cp /app/seen_posts.json ${BUCKET}/seen_posts.json

echo "Done!"
