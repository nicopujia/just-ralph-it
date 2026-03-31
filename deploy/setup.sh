#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# JRI Server Provisioning Script
# Run FROM the old server: ./deploy/setup.sh <new-server-ip>
# Provisions a fresh Ubuntu 24.04 server for justralph.it
# =============================================================================

NEW_IP="${1:?Usage: ./deploy/setup.sh <new-server-ip>}"
REMOTE="root@${NEW_IP}"
LOCAL_HOME="/home/nico"
REMOTE_HOME="/home/nico"

echo "==> Provisioning ${NEW_IP} for JRI..."

# ---------------------------------------------------------------------------
# Phase 1: Remote setup via SSH (as root)
# ---------------------------------------------------------------------------
ssh "${REMOTE}" bash -s <<'REMOTE_SCRIPT'
set -euo pipefail

# --- Create user nico with sudo access ---
echo "==> Creating user nico..."
if ! id nico &>/dev/null; then
    adduser --disabled-password --gecos "" nico
    usermod -aG sudo nico
    # Allow passwordless sudo
    echo "nico ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/nico
    chmod 0440 /etc/sudoers.d/nico
fi

# --- System packages ---
echo "==> Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 nginx git curl jq rsync sqlite3 mosh

# --- uv ---
echo "==> Installing uv..."
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# --- Node.js 22 via nodesource ---
echo "==> Installing Node.js 22..."
if ! command -v node &>/dev/null || ! node --version | grep -q "^v22"; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y nodejs
fi

# --- GitHub CLI ---
echo "==> Installing GitHub CLI..."
if ! command -v gh &>/dev/null; then
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list
    apt-get update -y
    apt-get install -y gh
fi

# --- OpenCode CLI ---
echo "==> Installing OpenCode..."
if ! command -v opencode &>/dev/null; then
    npm install -g @anthropic-ai/opencode
fi

# --- Clone repo ---
echo "==> Cloning JRI repo..."
if [ ! -d /home/nico/jri/.git ]; then
    git clone https://github.com/nicopujia/justralph.it.git /home/nico/jri
fi

# --- Python dependencies ---
echo "==> Installing Python dependencies..."
cd /home/nico/jri && /root/.local/bin/uv sync --no-dev

# --- Create data directory ---
echo "==> Creating data directory..."
mkdir -p /home/nico/jri/data

# --- Systemd units ---
echo "==> Installing systemd units..."
cp /home/nico/jri/deploy/jri.service /etc/systemd/system/jri.service
cp /home/nico/jri/deploy/jri.socket /etc/systemd/system/jri.socket

# --- Nginx configs ---
echo "==> Installing nginx configs..."
cp /home/nico/jri/deploy/nginx.conf /etc/nginx/sites-available/justralph.it
cp /home/nico/jri/deploy/nginx-subdomains.conf /etc/nginx/sites-available/jri-subdomains.conf
ln -sf /etc/nginx/sites-available/justralph.it /etc/nginx/sites-enabled/justralph.it
ln -sf /etc/nginx/sites-available/jri-subdomains.conf /etc/nginx/sites-enabled/jri-subdomains.conf
rm -f /etc/nginx/sites-enabled/default

# --- Create config directories for nico ---
echo "==> Creating config directories..."
mkdir -p /home/nico/.config/gh
mkdir -p /home/nico/.local/share/opencode
mkdir -p /home/nico/.local/bin

# --- Fix ownership (before SCP so nico owns the dirs) ---
echo "==> Fixing ownership..."
chown -R nico:nico /home/nico/

echo "==> Remote provisioning complete."
REMOTE_SCRIPT

# ---------------------------------------------------------------------------
# Phase 2: Copy credentials and data from old server
# ---------------------------------------------------------------------------

echo "==> Copying credentials to new server..."

# GitHub CLI credentials
scp "${LOCAL_HOME}/.config/gh/hosts.yml" "${REMOTE}:${REMOTE_HOME}/.config/gh/hosts.yml"

# OpenCode credentials
scp -r "${LOCAL_HOME}/.local/share/opencode/auth.json" "${REMOTE}:${REMOTE_HOME}/.local/share/opencode/auth.json"

# Environment file
scp "${LOCAL_HOME}/jri/.env" "${REMOTE}:${REMOTE_HOME}/jri/.env"

echo "==> Copying SQLite database..."
scp "${LOCAL_HOME}/jri/data/jri.db" "${REMOTE}:${REMOTE_HOME}/jri/data/jri.db"

echo "==> Copying project data directories..."
rsync -avz --progress "${LOCAL_HOME}/jri/data/" "${REMOTE}:${REMOTE_HOME}/jri/data/"

echo "==> Copying OpenCode session data..."
rsync -avz --progress "${LOCAL_HOME}/.local/share/opencode/" "${REMOTE}:${REMOTE_HOME}/.local/share/opencode/"

# ---------------------------------------------------------------------------
# Phase 3: Fix ownership and start services
# ---------------------------------------------------------------------------

echo "==> Fixing ownership and starting services..."
ssh "${REMOTE}" bash -s <<'FINAL_SCRIPT'
set -euo pipefail

# Fix ownership after all copies
chown -R nico:nico /home/nico/

# Enable and start services
systemctl daemon-reload
systemctl enable jri.socket jri.service
systemctl start jri.socket
systemctl start jri.service

# Reload nginx
nginx -t
systemctl reload nginx

echo "==> Services started successfully."
FINAL_SCRIPT

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "=============================================="
echo "  Server provisioning complete!"
echo "=============================================="
echo ""
echo "  Next step: Update Cloudflare DNS A records"
echo "    justralph.it      -> ${NEW_IP}"
echo "    *.justralph.it    -> ${NEW_IP}"
echo ""
echo "=============================================="
