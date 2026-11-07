# UZEX AI Hackathon — solution description

## What the prototype does

A Telegram bot answers questions about the 500,000-deal UZEX registry in Uzbek
and Russian. Every answer is produced from the provided CSV data; when the data
does not contain the answer, the bot says so instead of guessing. Each answer is
followed by a visible **Manba / Источник** block containing the exact SQL query,
the number of returned rows, the detected period and the status scope.

## Architecture

```
Telegram message
      │
      ▼
language detection (uz / ru)
      │
      ▼
Gemini 3.5 Flash Lite → JSON plan (intent + read-only SQL)
      │
      ▼
entity repair (product / region / status / company / broker / unit)
      │
      ▼
SQL guard (SELECT only, read-only connection, table whitelist, 8 s timeout)
      │
      ▼
SQLite (enriched, indexed, 500k deals)
      │
      ▼
deterministic bilingual renderer → answer text + source block
```

The LLM is used only as a **query planner**. It never writes the final numbers,
company names or codes: those come from the SQL result and are formatted by
Python templates. This is the main anti-hallucination layer.

### Data preparation

`app/build_db.py` loads all five yearly CSV files plus contracts, participants,
brokers and USD rates into one SQLite database and adds:

* `units = quantity × lot` — `Количество` is the number of lots, not physical
  units;
* `amount_uzs` and `amount_usd` — every amount converted at the deal-date USD
  rate, so sums never mix currencies;
* `is_canceled`, `is_completed`, `is_paid`, `is_delivered` flags;
* `year`, `month`, `month_key`, `quarter`, normalized company names;
* indexes for dates, products, regions, companies, brokers, amounts and units;
* a `metadata` table with the real row counts, date range, status distribution
  and yearly breakdown (computed from the data, not hard-coded).

### Grounding and safety

1. **Only the provided database** is in the prompt; the model is told that all
   companies, INNs and amounts are synthetic and that world knowledge is
   forbidden.
2. **Canonical values** for products, regions, statuses, contract types, forms,
   auction types and units are supplied to the model.
3. **Entity repair** resolves Uzbek/Russian/English aliases and fuzzy names to
   real database values before execution. Ambiguous names (for example
   “масло” or “Steel”) produce a clarification question, not a guess.
4. **SQL guard**: SELECT/WITH only, no comments or multiple statements, only
   six whitelisted tables, read-only SQLite connection, authorizer, 8-second
   progress-handler timeout and a 200-row cap. The authorizer uses a function
   allowlist and rejects `randomblob`, `zeroblob`, `printf`, `group_concat`,
   `load_extension` and similar primitives; recursive CTEs and string
   concatenation are blocked to prevent memory-exhaustion queries.
   `scripts/security_check.py` exercises these cases as an adversarial test.
5. **Abstain paths**: `not_found` for a real gap in the data, `unsupported` for
   forecasts, politics, weather, advice, phone numbers, profit and other
   out-of-scope questions. The bot explicitly says the answer is absent.
6. **Source block**: every data answer shows the SQL, row count, period and
   scope, so a reviewer can verify the number. The SQL is also visible in the
   logs.
7. **Output and operational safety**: dynamic database values and user terms
   are HTML-escaped, and a Telegram allowlist sanitizer keeps only `b`, `code`,
   `pre`, `blockquote` and similar formatting tags. The Gemini key is sent in a
   header rather than a URL, `.env` and logs are mode 600, `/stats` is
   admin-only, and a per-user/global question quota limits API abuse.

### Canceled deals

By default the bot counts **all registry rows, including canceled deals**, and
says so in the source scope. If the user explicitly asks for completed or
executed deals, the query filters `is_completed = 1`; if the user asks for
canceled deals, it filters `is_canceled = 1`. This avoids silently dropping
6.7% of the registry while still supporting the completed/canceled wording.

### Languages and UX

* Language is detected before planning; answer templates, labels, number
  formatting and dates are rendered in Uzbek or Russian.
* Uzbek Latin, Uzbek Cyrillic and Russian are supported; Russian decimal commas
  and Uzbek space-separated thousands are used.
* `/start`, `/help`, `/about`, `/examples` and clickable example questions.
* Long answers are split safely; source blocks are expandable and HTML-escaped.
* A semaphore limits concurrent questions and the Gemini client retries 429/5xx
  responses, so one slow request cannot block the whole bot.

### Testing

* `scripts/eval.py --offline` runs 55 scripted cases through the real SQL guard,
  entity repair and renderer without an API key. It covers counts, sums, units,
  currencies, products, regions, statuses, companies, brokers, contracts, USD
  rates, metadata, ambiguity and out-of-scope questions.
* `scripts/eval.py --live` sends the same questions to Gemini and compares the
  answer against independent reference SQL.
* `scripts/security_check.py` covers malicious SQL, function-based memory/CPU
  abuse, HTML injection and the per-user quota.
* `scripts/eval.py --offline` is also a fast regression test on the VM.

## Why Telegram

The task explicitly allows a Telegram bot, it requires no installation or
registration for the evaluator, and it works on any phone. It also keeps the
prototype small enough to run on a free 1 GB VM: no web server, no domain, no
TLS certificate, only outbound long polling.

## Stack

Python 3.13, `python-telegram-bot` 22.8, `httpx`, `python-dotenv`, SQLite,
Gemini 3.5 Flash Lite REST API with automatic fallback to other Flash models. Deployment: systemd on an always-on Ubuntu VM
(Oracle Cloud Free Tier or DigitalOcean), with a Dockerfile as an alternative.
