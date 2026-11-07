#!/usr/bin/env bash
# Deploy the project to an always-on Linux VM over SSH.
#
# Usage:
#   bash scripts/deploy_remote.sh ubuntu@203.0.113.10
#   SSH_KEY=~/.ssh/oracle.key bash scripts/deploy_remote.sh opc@203.0.113.10
#
# The archive excludes the virtualenv, the generated database, logs and secrets.
# The database is rebuilt on the VM from the CSV files; .env is copied over the
# encrypted SSH connection and installed with mode 600.
set -euo pipefail

REMOTE="${1:?usage: bash scripts/deploy_remote.sh user@host}"
APP_DIR="${APP_DIR:-/opt/uzex}"
SSH_KEY="${SSH_KEY:-}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="${TMPDIR:-/tmp}/uzex-deploy-$$.tgz"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
if [[ -n "$SSH_KEY" ]]; then
  SSH_OPTS+=(-i "$SSH_KEY")
fi

cleanup() {
  rm -f "$ARCHIVE"
}
trap cleanup EXIT

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "Missing $PROJECT_DIR/.env; create it from .env.example first." >&2
  exit 1
fi

echo "==> Packing project (without .venv, database, logs and .env)"
tar -C "$PROJECT_DIR" \
  --exclude='.venv' \
  --exclude='data/uzex.sqlite' \
  --exclude='logs' \
  --exclude='__pycache__' \
  --exclude='.env' \
  --exclude='*.pyc' \
  -czf "$ARCHIVE" .

echo "==> Uploading to $REMOTE"
scp "${SSH_OPTS[@]}" "$ARCHIVE" "$REMOTE:/tmp/uzex-deploy.tgz"
# Stream the secret over SSH and create it with mode 600 immediately; a
# plain `scp .env` would briefly leave it world-readable in /tmp.
ssh "${SSH_OPTS[@]}" "$REMOTE" "umask 077; rm -f /tmp/uzex.env; cat > /tmp/uzex.env" < "$PROJECT_DIR/.env"

echo "==> Installing on the VM (this builds the SQLite database)"
ssh "${SSH_OPTS[@]}" "$REMOTE" \
  "sudo mkdir -p '$APP_DIR' && \
   sudo tar -xzf /tmp/uzex-deploy.tgz -C '$APP_DIR' && \
   sudo install -m 600 -o root -g root /tmp/uzex.env '$APP_DIR/.env' && \
   rm -f /tmp/uzex.env /tmp/uzex-deploy.tgz && \
   cd '$APP_DIR' && sudo bash scripts/install_vm.sh"

echo
echo "==> Done. Check: ssh $REMOTE 'systemctl status uzex-bot --no-pager'"
echo "    Logs:  ssh $REMOTE 'journalctl -u uzex-bot -f'"
