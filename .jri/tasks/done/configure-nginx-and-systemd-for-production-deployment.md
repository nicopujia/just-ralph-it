---
title: Configure nginx and systemd for production deployment
priority: 0
assignee: ralph
depends_on:
- implement-main-project-page-with-two-panel-layout
created: '2026-03-21'
acceptance_criteria:
- The file deploy/nginx.conf exists with the correct nginx server block configuration
  including SSE support headers.
- The file deploy/jri.service exists with the correct systemd service configuration.
- The deploy.sh script exists and is executable.
- Running deploy.sh installs dependencies, updates nginx config, and starts the service.
- After deployment, https://justralph.it serves the FastAPI app (landing page).
- SSE connections work through nginx (proxy_buffering off is set).
- The /prd redirect still works.
- Other nginx server blocks (bot.nicolaspujia.com, podcast) are NOT modified.
- The service auto-restarts on crash (Restart=always).
- The service auto-starts on server boot (WantedBy=multi-user.target).
---

Set up nginx to proxy justralph.it to the uvicorn app, and create a systemd service for auto-start.

## Nginx configuration
Update /etc/nginx/sites-enabled/default (or the file containing the justralph.it server block) to proxy to the app:

```nginx
server {
    listen 80;
    server_name justralph.it www.justralph.it;

    # Proxy all requests to the FastAPI app
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # SSE support: disable buffering
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;  # 24 hours for long SSE connections
        proxy_send_timeout 86400s;
    }

    # Keep the /prd redirect
    location /prd {
        return 301 https://nicolaspujia.com/ralph;
    }
}
```

Important: Do NOT modify other server blocks in the nginx config (bot.nicolaspujia.com, podcast.nicolaspujia.com). Only change the justralph.it block. The static fallback is removed since the FastAPI app now serves the landing page directly.

## Systemd service
Create /etc/systemd/system/jri.service:

```ini
[Unit]
Description=Just Ralph It
After=network.target

[Service]
Type=simple
User=nico
WorkingDirectory=/home/nico/jri
ExecStart=/usr/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
Environment=PATH=/home/nico/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
```

## Deployment steps (to be run as root or with sudo)
1. Copy the nginx config.
2. Test nginx config: nginx -t.
3. Reload nginx: systemctl reload nginx.
4. Copy the systemd service file.
5. systemctl daemon-reload.
6. systemctl enable jri.
7. systemctl start jri.

## Deployment script
Create deploy.sh in the project root that does all of the above:
```bash
#!/bin/bash
set -e
pip install -r requirements.txt
sudo cp deploy/nginx.conf /etc/nginx/sites-enabled/justralph.it
sudo nginx -t
sudo systemctl reload nginx
sudo cp deploy/jri.service /etc/systemd/system/jri.service
sudo systemctl daemon-reload
sudo systemctl enable jri
sudo systemctl restart jri
echo 'Deployed successfully'
```

Store the nginx and systemd config files in a deploy/ directory in the repo.
