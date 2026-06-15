#!/bin/bash
set -e

BUCKET="gs://newark-parkrun-fb-notifier-state"

echo "Downloading config from GCS..."
gsutil cp ${BUCKET}/config.json /app/config.json

echo "Downloading seen_posts from GCS (if exists)..."
gsutil cp ${BUCKET}/seen_posts.json /app/seen_posts.json 2>/dev/null || echo '{"seen_ids":[]}' > /app/seen_posts.json

echo "Downloading cookies from GCS (if exists)..."
gsutil cp ${BUCKET}/cookies.json /app/cookies.json 2>/dev/null || true
gsutil cp ${BUCKET}/instagram_cookies.json /app/instagram_cookies.json 2>/dev/null || true

echo "Running notifier..."
python notifier.py --once

echo "Uploading seen_posts to GCS..."
gsutil cp /app/seen_posts.json ${BUCKET}/seen_posts.json

echo "Done!"
