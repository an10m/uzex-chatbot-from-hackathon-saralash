# UZEX AI Hackathon 2026 — Grounded Bilingual Deal Assistant

A Telegram bot that answers questions about **500,000 synthetic UZEX exchange
deals** in **Uzbek and Russian**. Every answer is produced from a read-only SQL
query over the provided CSV data. The language model is used only as a **query
planner**; the numbers, company names and codes in the final answer are rendered
by deterministic Python templates from the SQL result. When the data does not
contain the answer, the bot says so instead of guessing.

**Live prototype:** [@uzex_chatbot](https://t.me/uzex_chatbot)

> Built for the screening stage of the **UZEX AI Hackathon 2026**. All
> companies, INNs, brokers, amounts and prices in the dataset are synthetic.
> The bot is not a source of real market, financial or investment information.

---

## Contents

- [Why this design](#why-this-design)
- [Architecture](#architecture)
- [How an answer stays grounded](#how-an-answer-stays-grounded)
- [Data](#data)
- [Project layout](#project-layout)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Using the bot](#using-the-bot)
- [Testing](#testing)
- [Deployment](#deployment)
- [Security notes](#security-notes)
- [Evaluation results](#evaluation-results)
- [Known limitations](#known-limitations)
- [Hackathon context](#hackathon-context)

---

## Why this design

The screening task rewards correct answers and explicitly disqualifies teams
whose agent invents a number, company or product that is absent from the data.
A free-form LLM answer is therefore the wrong architecture. This project splits
the work:

| Layer | Responsibility |
| --- | --- |
| Gemini Flash | Understand the question and produce a JSON plan with a read-only SQL query |
| Entity repair | Resolve Uzbek/Russian/English product, region, company, broker and status names to real database values |
| SQL guard | Reject anything that is not a bounded, read-only `SELECT` |
| SQLite | Execute the query and return the authoritative rows |
| Deterministic renderer | Format the answer in the question's language and attach a visible source block |

The model never writes the final sentence containing a number. It cannot
"hallucinate" a figure because it never sees the opportunity to type one.

Key properties:

- **Grounded** — every value comes from the SQL result.
- **Auditable** — each answer includes a `Manba` / `Источник` block with the SQL,
  row count, period and status scope.
- **Bilingual** — Uzbek Latin, Uzbek Cyrillic and Russian are detected and
  answered in the same language.
- **Abstains honestly** — out-of-scope questions and missing records return an
  explicit `not_found` or `unsupported` answer.
- **Fast enough for the 90-second limit** — typical answers take a few seconds;
  the bot enforces an 80-second internal deadline.
- **Safe to expose publicly** — SELECT-only SQL, read-only connection, function
  allowlist, HTML sanitizer, per-user/global rate limits and a hardened systemd
  unit.

---

## Architecture

```text
Telegram message
      │
      ▼
language detection (Uzbek Latin / Uzbek Cyrillic / Russian)
      │
      ▼
Gemini 3.5 Flash Lite ──► JSON plan: intent + read-only SQL
      │                   (automatic fallback to other Flash models)
      ▼
entity repair
  product family · region · status · company · broker · unit · contract type
      │
      ▼
SQL guard
  SELECT/WITH only · no comments · no multiple statements · table allowlist
  function allowlist · read-only SQLite · 8 s timeout · 200-row cap
      │
      ▼
SQLite database (500k deals, enriched and indexed)
      │
      ▼
deterministic bilingual renderer
      │
      ▼
HTML sanitizer ──► Telegram answer + Manba / Источник block
```

### Request lifecycle

1. `app/bot.py` receives a Telegram update over long polling.
2. The question language is detected before planning.
3. `app/llm.py` asks Gemini for a structured plan. The client retries 429/5xx
   responses and falls back through a list of available Flash models.
4. `app/agent.py` validates the intent, repairs named entities and dispatches
   the plan (SQL, deal lookup, contract lookup, company/broker card, USD rate,
   metadata, ambiguity or unsupported).
5. `app/sql_guard.py` validates the SQL and executes it on a read-only SQLite
   connection with an authorizer, a progress-handler timeout and a row cap.
6. `app/answers.py` formats the result in Uzbek or Russian and builds the source
   block. No language model is involved in this step.
7. `app/html_safe.py` sanitizes the final HTML against a small Telegram
   allowlist before sending.

---

## How an answer stays grounded

1. **Only the provided database is in scope.**
   The prompt tells the model that all companies, INNs, brokers and amounts are
   synthetic, that the data ends on 30 October 2026, and that world knowledge
   must not be used.

2. **Canonical values are supplied to the planner.**
   Product families, regions, statuses, contract types, forms, auction types and
   units are listed with their real database spellings.

3. **Entity repair happens before execution.**
   Uzbek, Russian and English aliases, apostrophe variants and fuzzy names are
   resolved to actual database values. Ambiguous terms such as “масло” or
   “Steel” produce a clarification question instead of a guess.

4. **SQL is treated as untrusted input.**
   - only `SELECT` / `WITH … SELECT`;
   - no comments, semicolons or multiple statements;
   - only the six application tables;
   - read-only SQLite URI (`mode=ro`);
   - authorizer with a function allowlist;
   - recursive CTEs and `||` concatenation blocked;
   - 8-second timeout and at most 200 returned rows.

5. **The renderer owns the final text.**
   Counts, sums, dates, names and codes are inserted from the query result into
   bilingual templates. Dynamic values are HTML-escaped.

6. **Abstention is a first-class outcome.**
   - `not_found` — the data is in scope but the requested record or value does
     not exist.
   - `unsupported` — forecasts, politics, weather, advice, phone numbers,
     profit, real-world market data and other out-of-scope requests.

7. **The source block makes verification possible.**
   Every data answer shows the executed SQL, the number of rows, the detected
   period, the status scope and the overall data range. The SQL is also written
   to the log.

### A note on canceled deals

By default the bot counts **all registry rows, including canceled deals**, and
says so in the source scope. If the user explicitly asks for completed or
executed deals, the query filters `is_completed = 1`; if the user asks for
canceled deals, it filters `is_canceled = 1`. This avoids silently dropping
6.7% of the registry while still understanding the completed/canceled wording.

---

## Data

The source folder `uzex_spot_data/` contains the synthetic dataset published for
the hackathon:

| File | Contents |
| --- | --- |
| `deals_2022.csv` … `deals_2026.csv` | 500,000 deals from 3 January 2022 to 30 October 2026 |
| `contracts.csv` | 6,616 contract cards: product attributes, unit, lot, currency, base price, payment/delivery terms, seller |
| `participants.csv` | 14,392 seller and buyer companies |
| `brokers.csv` | 120 brokerage offices |
| `usd_rates.csv` | daily USD/UZS rate for every calendar day |

The full column-by-column description in Russian and Uzbek is in
[`uzex_spot_data/README.md`](uzex_spot_data/README.md).

### Database build

`app/build_db.py` is a dependency-free (stdlib only) loader that creates
`data/uzex.sqlite` and adds the derived fields the agent needs:

| Derived field | Meaning |
| --- | --- |
| `units` | `quantity × lot` — the physical volume in the contract's unit |
| `unit_price` | amount per unit |
| `amount_uzs` / `amount_usd` | every amount converted at the deal-date USD rate |
| `usd_rate` | rate applied to the deal |
| `is_canceled`, `is_completed`, `is_paid`, `is_delivered` | status flags |
| `year`, `month`, `month_key`, `quarter` | date dimensions |
| `seller_name_norm`, `buyer_name_norm` | normalized company names for lookup |
| `product_family` | normalized product taxonomy (41 families) |

Indexes cover dates, years, months, contracts, products, regions, companies,
brokers, amounts and units. A `metadata` table stores the real row counts, date
range, status distribution, yearly breakdown and other aggregates computed from
the data rather than hard-coded in the prompt.

The generated database is about **547 MB** and is intentionally **not committed**
to the repository. Rebuild it with the command in
[Quick start](#quick-start).

### Source data note

The CSV files are included in this repository so the database can be rebuilt
end-to-end without an external download. They contain **only synthetic data**
provided by the hackathon organizers. If you prefer a smaller checkout, remove
`uzex_spot_data/*.csv` after building `data/uzex.sqlite`; the bot only needs the
SQLite file at runtime.

---

## Project layout

```text
.
├── app/
│   ├── agent.py          # plan validation, entity retry, intent dispatch
│   ├── answers.py        # deterministic bilingual answer and source rendering
│   ├── bot.py            # Telegram entry point, rate limits, /stats, deadline
│   ├── build_db.py       # CSV → enriched SQLite database
│   ├── cli.py            # terminal client and REPL
│   ├── config.py         # .env / environment configuration
│   ├── entities.py       # product/region/company/broker/unit alias resolution
│   ├── html_safe.py      # Telegram HTML allowlist sanitizer
│   ├── llm.py            # Gemini REST client, retries, model fallback
│   ├── prompts.py        # planner prompt and canonical values
│   ├── sql_guard.py      # SQL validation, authorizer, timeout, row cap
│   └── sql_repair.py     # safe repair/retry of failed planner SQL
├── data/                 # generated uzex.sqlite (gitignored)
├── deploy/
│   └── uzex-bot.service  # hardened systemd unit
├── docs/
│   ├── DEPLOY.md         # step-by-step VM deployment guide
│   ├── SOLUTION.md       # longer solution description
│   ├── FORM_ANSWER_RU.md # ready-to-paste Russian form text
│   └── FORM_ANSWER_UZ.md # ready-to-paste Uzbek form text
├── scripts/
│   ├── deploy_remote.sh  # rsync/ssh deployment helper
│   ├── eval.py           # 55-case offline and live evaluation
│   ├── install_vm.sh     # one-time Ubuntu/Debian VM installer
│   └── security_check.py # adversarial SQL/HTML/rate-limit checks
├── uzex_spot_data/       # synthetic CSV dataset + bilingual schema README
├── .env.example
├── .gitignore
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## Quick start

### Prerequisites

- Linux or macOS (Windows works with WSL); Ubuntu 22.04/24.04 is the deployment
  target.
- **Python 3.9+** (tested with Python 3.13.5).
- About **1.5 GB free disk space**: ~154 MB CSV, ~547 MB SQLite, plus the
  virtual environment.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).
- No inbound ports, domain or TLS certificate are required. The bot uses
  outbound long polling.

### 1. Clone the repository

```bash
git clone https://github.com/an10m/uzex-hackathon-saralash.git
cd uzex-hackathon-saralash
```

### 2. Create the virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip wheel
pip install -r requirements.txt
```

The runtime dependencies are deliberately small: `python-telegram-bot`,
`httpx` and `python-dotenv`. The database builder uses only the standard
library.

### 3. Configure secrets

```bash
cp .env.example .env
chmod 600 .env
${EDITOR:-nano} .env
```

Fill in at least:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:replace-with-your-botfather-token
GEMINI_API_KEY=replace-with-your-gemini-api-key
GEMINI_MODEL=gemini-3.5-flash-lite
```

How to obtain them:

1. **Telegram token** — open [@BotFather](https://t.me/BotFather), send
   `/newbot`, choose a name and a username ending in `bot`, then copy the token.
2. **Gemini key** — open <https://aistudio.google.com/apikey>, create an API key
   and copy it.

> **Never commit `.env`.** It is already listed in `.gitignore`. Use
> `.env.example` as the template. If a key is ever committed or pasted into a
> chat, revoke it and create a new one.

### 4. Build the SQLite database

```bash
python app/build_db.py --data-dir uzex_spot_data --out data/uzex.sqlite
```

Expected final line (size and timing vary):

```text
Done: data/uzex.sqlite  (546.9 MB, 500,000 deals, 25.0s)
```

The loader reads the five yearly deal files, contracts, participants, brokers
and USD rates, computes the derived fields and indexes, and writes the database.
It is safe to re-run; the output file is replaced.

### 5. Run the regression checks

```bash
# SQL guard, HTML sanitizer and per-user rate limit: expect "OK: 0 failure(s)"
python scripts/security_check.py

# 55 scripted cases through the real backend: expect "55/55 passed (offline)"
python scripts/eval.py --offline

# End-to-end terminal test (needs GEMINI_API_KEY)
python -m app.cli "2025-yilda nechta bitim tuzilgan?"
```

The CLI test should print a count followed by a `Manba` / `Источник` block.

### 6. Start the bot

```bash
python -m app.bot
```

Open your bot in Telegram, send `/start`, then ask one of the example
questions. Press `Ctrl+C` to stop. For an always-on deployment, use the
systemd path in [Deployment](#deployment).

### 7. Optional: run without the bot

```bash
# One question
python -m app.cli "Сколько сделок было в 2025 году?"

# Interactive REPL
python -m app.cli --repl

# Full answer object as JSON
python -m app.cli --json "2025-yilda paxta bo‘yicha qancha bitim va qancha kilogramm?"
```

---

## Configuration

All configuration is read from `.env` (or the process environment) by
`app/config.py`.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | — | Bot token from @BotFather |
| `GEMINI_API_KEY` | yes | — | Gemini API key from Google AI Studio |
| `GEMINI_MODEL` | no | `gemini-3.5-flash-lite` | Preferred model; the client falls back through other available Flash models |
| `UZEX_DB_PATH` | no | `data/uzex.sqlite` | Path to the generated SQLite database |
| `LOG_FILE` | no | `logs/bot.log` | Rotating log file used outside systemd |
| `ADMIN_CHAT_ID` | no | empty | Telegram chat ID allowed to use `/stats`; `/stats` is disabled when empty |
| `LOG_LEVEL` | no | `INFO` | Python logging level |

To find your Telegram chat ID, send `/id` to the bot and put the returned
number in `ADMIN_CHAT_ID`, then restart the service.

The Gemini client sends the key in the `x-goog-api-key` header rather than the
URL, retries 429/5xx responses with backoff, and records the active model and
rate-limit counters for `/stats`.

---

## Using the bot

| Command | Purpose |
| --- | --- |
| `/start` | Bilingual welcome message and example questions |
| `/help` | How to ask questions and what the bot cannot do |
| `/about` | Data coverage and technology summary |
| `/examples` | Clickable example questions in Uzbek and Russian |
| `/id` | Show the current Telegram chat ID |
| `/stats` | Admin-only question/error/model/429 counters |

Example questions:

```text
2025-yilda nechta bitim tuzilgan?
2025-yilda paxta bo‘yicha qancha bitim va qancha kilogramm?
Eng ko‘p bitim tuzgan 5 ta sotuvchi kimlar?
504707-sonli bitim haqida ma’lumot bering.
100001-sonli shartnoma shartlari qanday?
01.01.2025 dagi dollar kursi qancha?
Toshkent shahridagi eng yirik xaridor kim?
2024 va 2025 yillarni bitimlar soni bo‘yicha solishtiring.
```

```text
Сколько сделок было в 2025 году?
Сколько хлопка продано в 2025 году в килограммах?
Топ-5 продавцов по количеству сделок.
Информация о сделке 504707.
Условия контракта 100001.
Какой курс доллара был 01.01.2025?
Крупнейший покупатель в городе Ташкент.
Сравните 2024 и 2025 годы по количеству сделок.
```

Each data answer ends with an expandable source block containing:

- the exact SQL that produced the result;
- the number of returned rows;
- the detected period;
- the status scope (all rows / completed / canceled);
- the overall data range.

Operational limits:

- per user: **12 questions per minute** and **80 per hour**;
- globally: **150 questions per minute**;
- internal per-question deadline: **80 seconds** (the evaluation allows 90);
- SQL timeout: **8 seconds**; maximum **200 rows** returned to the renderer.

---

## Testing

### Offline evaluation (no API key)

```bash
python scripts/eval.py --offline
```

The 55 scripted cases run through the real SQL guard, entity repair and
renderer. Coverage includes counts, sums, physical units, currencies, products,
regions, statuses, companies, brokers, contracts, USD rates, metadata,
ambiguity and out-of-scope questions.

### Live evaluation (uses Gemini)

```bash
python scripts/eval.py --live
python scripts/eval.py --live --filter 2025
```

Live mode sends the same questions to Gemini and compares the agent's answer
with independent reference SQL. The reference queries are written separately
from the planner prompt, so a failure points at the agent rather than at a
copied example.

### Security checks

```bash
python scripts/security_check.py
```

The script exercises:

- destructive and multi-statement SQL;
- `PRAGMA`, `ATTACH`, `sqlite_master` and other forbidden constructs;
- memory/CPU abuse through `randomblob`, `zeroblob`, `printf`, `group_concat`,
  recursive CTEs and string concatenation;
- HTML/script injection through ambiguous entity names;
- the per-user rate limit.

Expected result: `OK: 0 failure(s)`.

### Results at submission time

| Check | Result |
| --- | --- |
| Offline evaluation | **55/55 passed** |
| Live evaluation against Gemini 3.5 Flash Lite | **55/55 passed** |
| Security check | **OK — 0 failures** |
| Slowest observed live answer | **~15.5 s** (limit: 90 s) |

Live results depend on the Gemini API and the available free-tier quota. The
client automatically moves to another Flash model when the preferred one is
unavailable or rate-limited.

---

## Deployment

The recommended production setup is a small always-on Ubuntu VM with systemd.
A 1 vCPU / 1 GB RAM instance is enough; add 2 GB of swap on a 1 GB machine. No
inbound port, domain or TLS certificate is needed.

The full step-by-step guide is in [`docs/DEPLOY.md`](docs/DEPLOY.md). The short
version:

### One-command deployment

From the project root, with SSH access to the VM:

```bash
bash scripts/deploy_remote.sh ubuntu@YOUR_VM_IP

# Oracle Linux often uses opc@ instead of ubuntu@:
SSH_KEY=~/.ssh/oracle.key bash scripts/deploy_remote.sh opc@YOUR_VM_IP
```

The script packs the sources and CSV files (excluding `.venv`, the generated
database, logs and `.env`), uploads them over SSH, streams `.env` securely,
and runs `scripts/install_vm.sh` on the VM. The database is rebuilt on the VM.

### Manual deployment

```bash
# 1. Copy the project (and optionally data/uzex.sqlite) to /opt/uzex
rsync -avz --progress \
  --exclude '.venv' --exclude '__pycache__' --exclude 'logs' \
  ./ ubuntu@YOUR_VM_IP:/opt/uzex/

# 2. On the VM
cd /opt/uzex
cp .env.example .env
chmod 600 .env
nano .env

# 3. Install system packages, create the service user, build the DB,
#    install the unit and start the service
sudo bash scripts/install_vm.sh

# 4. Inspect
systemctl status uzex-bot
journalctl -u uzex-bot -f
```

The installer creates the `uzex` system user, a virtual environment, installs
dependencies, builds `data/uzex.sqlite` when missing, and enables
`uzex-bot.service` with `Restart=always`.

### Operate

| Task | Command |
| --- | --- |
| Status | `systemctl status uzex-bot` |
| Logs | `journalctl -u uzex-bot -f` |
| Restart | `systemctl restart uzex-bot` |
| Stop | `systemctl stop uzex-bot` |
| Update code | re-run `rsync`/deploy, then `systemctl restart uzex-bot` |
| Disk usage | `du -sh /opt/uzex/data/uzex.sqlite` |
| Bot stats | `/stats` in Telegram (admin only) |

### Docker alternative

```bash
docker build -t uzex-bot .
docker run -d --restart unless-stopped \
  --env-file .env \
  -v "$PWD/data:/opt/uzex/data" \
  --name uzex-bot uzex-bot
```

The image builds the database from `uzex_spot_data` at build time, so the image
is large. The systemd path is recommended for the hackathon VM.

### Service hardening

`deploy/uzex-bot.service` runs as the unprivileged `uzex` user with:

- `NoNewPrivileges=true`
- `PrivateTmp=true`
- `ProtectSystem=full`
- `ProtectHome=true`
- `RestrictSUIDSGID=true`
- `LockPersonality=true`
- an empty capability bounding set
- `UMask=0077`

The application code and database are root-owned and read-only to the service;
only the log directory is writable by `uzex`.

---

## Security notes

This repository is designed to be published without exposing credentials. The
following are deliberate:

- `.env`, `.venv/`, `logs/`, `data/*.sqlite`, `__pycache__/` and Python bytecode
  are excluded by `.gitignore`.
- `.env.example` contains placeholders only.
- The Gemini key is sent in an HTTP header, not in a URL query string, so it
  does not appear in proxy or access logs.
- Error paths and logs redact Telegram tokens (`123456:…`) and Google API keys
  (`AIza…`).
- The bot logs the executed SQL but never the API key or the bot token.
- `/stats` is disabled unless `ADMIN_CHAT_ID` is set, and is closed to everyone
  else.
- Per-user and global rate limits stop the public bot from being used as a free
  LLM proxy.
- `scripts/security_check.py` is an adversarial regression test for the SQL and
  HTML layers.

### Before you publish a fork

```bash
git status --ignored
git grep -nE 'AIza[0-9A-Za-z_-]{20,}|[0-9]{6,}:[A-Za-z0-9_-]{20,}' || true
```

The first command must not list `.env` as tracked. The second should print
nothing. If a real key appears anywhere in the working tree or history, revoke
it in BotFather / Google AI Studio and create a replacement before pushing.

### Reporting a problem

If you find a security issue, please do not open a public issue containing
secrets. Revoke the affected credential first, then contact the repository
owner privately.

---

## Evaluation results

At the time the prototype was submitted:

- **Offline regression suite:** 55/55 passed.
- **Live suite against Gemini 3.5 Flash Lite:** 55/55 passed.
- **Adversarial security suite:** 0 failures.
- **Slowest observed live answer:** ~15.5 seconds, well inside the 90-second
  per-question limit.
- **Database:** 500,000 deals, 6,616 contracts, 14,392 companies, 120 brokers,
  1,765 daily USD rates, 41 product families.
- **Coverage:** 3 January 2022 – 30 October 2026.

The evaluation harness is included so the numbers can be reproduced:
`scripts/eval.py --offline`, `scripts/eval.py --live` and
`scripts/security_check.py`.

---

## Known limitations

- **Synthetic data only.** The dataset is fictional and ends on 30 October 2026.
  The bot explicitly refuses forecasts, real-market data, politics, weather,
  advice, phone numbers, profit questions and other out-of-scope requests.
- **The long tail of product names is hard.** The database normalizes products
  into 41 families, but unusual free-text attributes can still be ambiguous.
  The bot asks for clarification rather than guessing.
- **SQLite is a single-node store.** This is appropriate for 500k rows and a
  hackathon prototype, not for a multi-tenant production service.
- **Gemini availability matters.** The bot falls back across Flash models and
  retries 429/5xx responses, but a total API outage would prevent new answers.
- **No authentication.** The Telegram bot is intentionally open so evaluators
  can use it without registration, as the task requires. Rate limits are the
  only access control.
- **Long polling, not webhooks.** This keeps deployment simple and avoids
  inbound ports, at the cost of a small latency overhead.

---

## Hackathon context

- **Task:** build an AI agent that answers questions about exchange deals in
  Uzbek and Russian, using only the provided data, and explicitly says when the
  answer is absent.
- **Interface:** Telegram bot or web service; the prototype is a Telegram bot so
  it works without installation or registration.
- **Evaluation:** 10 questions, up to 90 seconds each; 60 points for
  correctness, 20 for prototype quality, 20 for the solution description.
- **Dataset:** 500,000 synthetic deals, January 2022 – October 2026, plus
  contracts, participants, brokers and daily USD rates.
- **Rules:** inventing a number, company or product that is not in the data
  leads to disqualification.

Official pages:

- <https://hackathon.uzex.uz/saralash/>
- [@UZEX_AI_Hackathon](https://t.me/UZEX_AI_Hackathon)

Longer write-ups:

- [`docs/SOLUTION.md`](docs/SOLUTION.md) — architecture and design decisions.
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — full VM deployment guide.
- [`docs/FORM_ANSWER_RU.md`](docs/FORM_ANSWER_RU.md) — Russian solution text.
- [`docs/FORM_ANSWER_UZ.md`](docs/FORM_ANSWER_UZ.md) — Uzbek solution text.

---

## Acknowledgements

- **UZEX** for the hackathon task and the synthetic dataset.
- **Google AI Studio / Gemini** for the free-tier API used by the prototype.
- **python-telegram-bot** and **SQLite** for the runtime foundation.

The dataset is provided by the hackathon organizers and remains subject to their
terms. The source code is published for the UZEX AI Hackathon 2026; no separate
open-source license is granted.
