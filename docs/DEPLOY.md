# Deploying the bot

The prototype is a Telegram long-polling bot. It needs one always-on Linux
server with outbound HTTPS; no inbound ports, domain or TLS certificate are
required. A 1 vCPU / 1 GB RAM VM is enough (the SQLite file is ~550 MB), but
add swap on a 1 GB machine.

## 1. Create the Telegram bot

1. Open [@BotFather](https://t.me/BotFather), send `/newbot`, choose a name and
   a username ending in `bot`.
2. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN=...`.
3. Do **not** commit `.env`. A `.gitignore` entry already protects it.

## 2. Get a Gemini API key

1. Open <https://aistudio.google.com/apikey> with the student account.
2. Create an API key and put it in `.env` as `GEMINI_API_KEY=...`.
3. Optional: set `GEMINI_MODEL=gemini-3.5-flash-lite` (the client automatically
   falls back to another available Flash model, so an exhausted 2.5 quota does
   not stop the bot).

## 3. Create the VM (Oracle Cloud Free Tier or DigitalOcean)

Any Ubuntu 22.04/24.04 VM works. The free Oracle Ampere A1 shape (ARM) is
plenty; the AMD micro shape (1 GB RAM) also works with swap.

On the VM:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 4. Copy the project and database

The fastest path is the helper script. It packs the sources and CSV files
(without `.venv`, the generated database, logs or `.env`), uploads them, copies
`.env` securely, and runs the installer. The database is rebuilt on the VM.

```bash
cd /home/db/uzex-hackathon-saralash
bash scripts/deploy_remote.sh ubuntu@YOUR_VM_IP
# Oracle Linux usually uses opc@ instead of ubuntu@:
# SSH_KEY=~/.ssh/oracle.key bash scripts/deploy_remote.sh opc@YOUR_VM_IP
```

Manual alternative (replace user/host):

```bash
rsync -avz --progress \
  --exclude '.venv' --exclude '__pycache__' --exclude 'logs' \
  ./ ubuntu@YOUR_VM_IP:/opt/uzex/
```

The database is in `data/uzex.sqlite` and is included by that command. If you
prefer not to upload 550 MB, omit `data/uzex.sqlite`; `scripts/install_vm.sh`
will rebuild it from `uzex_spot_data/` on the VM.

Create `.env` on the VM:

```bash
cd /opt/uzex
cp .env.example .env
nano .env
```

## 5. Install and start the service

```bash
cd /opt/uzex
sudo bash scripts/install_vm.sh
```

The installer creates the `uzex` user, a virtual environment, installs
dependencies, builds the DB if needed, and enables `uzex-bot.service` with
`Restart=always`.

Check:

```bash
systemctl status uzex-bot
journalctl -u uzex-bot -f
```

Open the bot in Telegram, send `/start`, then ask a question. The bot also has
`/help`, `/about`, `/examples` and `/id`.

For operator access, send `/id`, add the returned number to `.env` as
`ADMIN_CHAT_ID=...`, then restart the service. `/stats` is deliberately closed
to everyone else. It shows question/error counters, the active Gemini model and
the 429 count.

The installer makes the application root-owned and read-only to the service,
sets `.env` to `root:uzex` mode 640 (readable by the service group, not
writable), and the log directory to mode 700. Keep those permissions if you
edit the files manually. Immediately after creating `.env` by hand, run
`sudo chown root:uzex .env && sudo chmod 640 .env`.

## 6. Smoke test from inside the VM

```bash
cd /opt/uzex
.venv/bin/python scripts/security_check.py
.venv/bin/python scripts/eval.py --offline
sudo .venv/bin/python -m app.cli "2025-yilda nechta bitim tuzilgan?"
```

The CLI test needs `sudo` only because `.env` is intentionally not readable by
ordinary users; the offline tests do not need the secrets.

The first command must print `OK: 0 failure(s)`; the second must print
`55/55 passed`; the third must return a number and a `Manba`/`Источник` block.

## 7. Operate

| Task | Command |
| --- | --- |
| Logs | `journalctl -u uzex-bot -f` |
| Restart | `systemctl restart uzex-bot` |
| Stop | `systemctl stop uzex-bot` |
| Update code | re-run `rsync`, then `systemctl restart uzex-bot` |
| Disk usage | `du -sh /opt/uzex/data/uzex.sqlite` |
| Bot stats | `/stats` in Telegram (admin `ADMIN_CHAT_ID` only) |
| Security check | `.venv/bin/python scripts/security_check.py` |

## 8. Keep it alive during evaluation

* Keep the VM running until the results are announced (6 November).
* Use a paid/always-free instance, not a trial that expires during the day.
* The bot uses long polling, so no webhook or open inbound port is needed.
* If the provider reboots the VM, systemd starts the bot automatically.
* If Gemini returns 429, the client retries; the global semaphore limits the
  bot to 3-4 concurrent questions.

## Alternative: Docker

```bash
docker build -t uzex-bot .
docker run -d --restart unless-stopped \
  --env-file .env \
  -v "$PWD/data:/opt/uzex/data" \
  --name uzex-bot uzex-bot
```

The image builds the database from `uzex_spot_data` during the build, so the
resulting image is large. The systemd path is recommended for the hackathon.
