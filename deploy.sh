#!/bin/bash
set -e
uv sync --no-dev
sudo cp -f deploy/nginx.conf /etc/nginx/sites-enabled/justralph.it
sudo cp -f deploy/nginx-subdomains.conf /etc/nginx/sites-enabled/jri-subdomains.conf
sudo nginx -t
sudo systemctl reload nginx
sudo cp -f deploy/jri.service /etc/systemd/system/jri.service
sudo cp -f deploy/jri.socket /etc/systemd/system/jri.socket
sudo systemctl daemon-reload
sudo systemctl enable jri.socket jri
sudo systemctl restart jri
echo 'Deployed successfully'
