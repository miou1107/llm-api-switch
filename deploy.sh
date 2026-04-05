#!/bin/bash
set -e

HOST="root@kkvin.com"
REMOTE_DIR="/VinService/llm-api-switch"

echo "==> Syncing files to kkvin.com..."
rsync -avz --delete \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude 'data/*.db' \
  --exclude '.claude' \
  --exclude '.env' \
  --exclude '.pytest_cache' \
  --exclude 'config/api_keys.yaml' \
  --exclude 'config/providers.yaml' \
  ./ "$HOST:$REMOTE_DIR/"

echo "==> Building and restarting on kkvin.com..."
ssh "$HOST" "cd $REMOTE_DIR && docker compose build --no-cache app && docker compose up -d app"

echo "==> Done! Dashboard: https://kkvin.com/llm-switch/dashboard/"
