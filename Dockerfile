# Use Playwright's official image which includes Chromium and all dependencies
FROM mcr.microsoft.com/playwright/python:v1.57.0-noble

WORKDIR /app

# Install Google Cloud SDK for gsutil
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] http://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list \
    && curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && apt-get update && apt-get install -y --no-install-recommends google-cloud-cli \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY notifier.py run.sh ./
RUN chmod +x run.sh

# Run the notifier with GCS sync
CMD ["./run.sh"]
