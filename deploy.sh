#!/bin/bash
set -e
uv sync --no-dev
sudo cp -f deploy/nginx.conf /etc/nginx/sites-enabled/justralph.it
sudo nginx -t
sudo systemctl reload nginx
sudo cp -f deploy/jri.service /etc/systemd/system/jri.service
sudo systemctl daemon-reload
sudo systemctl enable jri
sudo systemctl restart jri
echo 'Deployed successfully'
