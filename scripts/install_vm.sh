#!/usr/bin/env bash
# One-time installer for an always-on Linux VM (Ubuntu/Debian).
#
# Run as root on the VM after the repository (and optionally data/uzex.sqlite)
# has been copied to /opt/uzex:
#
#   sudo bash scripts/install_vm.sh
#
# The script installs Python, creates a service user, installs dependencies,
# builds the SQLite database if it is missing, and enables the systemd unit.
set -euo pipefail
umask 077

APP_DIR="${APP_DIR:-/opt/uzex}"
SERVICE_NAME="${SERVICE_NAME:-uzex-bot}"
PYTHON="${PYTHON:-python3}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/install_vm.sh" >&2
  exit 1
fi

if [[ ! -f "$APP_DIR/app/bot.py" ]]; then
  echo "Repository not found at $APP_DIR. Copy it there first." >&2
  exit 1
fi

SWAP_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
if [[ ! -f /swapfile ]] && (( SWAP_MB < 1800 )); then
  echo "==> Adding a 2 GB swap file (VM has ${SWAP_MB} MB RAM)"
  if fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none; then
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  else
    echo "WARNING: could not create swap; continuing without it." >&2
  fi
fi

echo "==> Installing system packages"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip sqlite3 ca-certificates
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip sqlite ca-certificates
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 python3-pip sqlite ca-certificates
else
  echo "Unsupported package manager: install python3, python3-venv and sqlite3 manually." >&2
  exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "ERROR: Python 3.9+ is required; found $("$PYTHON" -V 2>&1)." >&2
  echo "Use Ubuntu 22.04/24.04 or install a newer python3." >&2
  exit 1
fi

if ! id -u uzex >/dev/null 2>&1; then
  echo "==> Creating uzex service user"
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin uzex
fi

echo "==> Installing Python dependencies"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  "$PYTHON" -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --upgrade pip wheel
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/data/uzex.sqlite" ]]; then
  echo "==> Building SQLite database (this can take a few minutes)"
  "$APP_DIR/.venv/bin/python" "$APP_DIR/app/build_db.py" \
    --data-dir "$APP_DIR/uzex_spot_data" \
    --out "$APP_DIR/data/uzex.sqlite"
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "==> WARNING: $APP_DIR/.env is missing. Copy .env.example and fill it in." >&2
fi

echo "==> Installing systemd unit"
install -m 0644 "$APP_DIR/deploy/uzex-bot.service" "/etc/systemd/system/${SERVICE_NAME}.service"
mkdir -p "$APP_DIR/logs"
# Code and the registry stay root-owned and read-only to the service.  Only
# the log directory is writable by the uzex user.  .env is readable by the
# service group (systemd loads it before dropping privileges) but not writable.
chown -R root:root "$APP_DIR"
chown -R uzex:uzex "$APP_DIR/logs"
chmod 700 "$APP_DIR/logs"
# The service runs as uzex.  The virtualenv must be traversable/readable and
# the database readable by the service group; application code stays root-owned.
chmod a+rX "$APP_DIR"
chmod -R a+rX "$APP_DIR/app" "$APP_DIR/.venv"
chmod 755 "$APP_DIR/data"
if [[ -f "$APP_DIR/data/uzex.sqlite" ]]; then
  chown root:uzex "$APP_DIR/data/uzex.sqlite"
  chmod 640 "$APP_DIR/data/uzex.sqlite"
fi
if [[ -f "$APP_DIR/.env" ]]; then
  chown root:uzex "$APP_DIR/.env"
  chmod 640 "$APP_DIR/.env"
fi
find "$APP_DIR/logs" -maxdepth 1 -type f -name '*.log*' -exec chown uzex:uzex {} + 2>/dev/null || true
find "$APP_DIR/logs" -maxdepth 1 -type f -name '*.log*' -exec chmod 600 {} + 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo "==> Status"
systemctl --no-pager --full status "$SERVICE_NAME" || true
echo
echo "Follow logs: journalctl -u ${SERVICE_NAME} -f"
echo "Restart:     systemctl restart ${SERVICE_NAME}"
