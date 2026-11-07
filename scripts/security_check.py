"""Adversarial checks for the SQL guard and Telegram HTML sanitizer.

Run from the project root:

    .venv/bin/python scripts/security_check.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.answers import render_ambiguous  # noqa: E402
from app.bot import _is_rate_limited  # noqa: E402
from app.config import DB_PATH  # noqa: E402
from app.html_safe import sanitize_telegram_html  # noqa: E402
from app.sql_guard import SQLRejected, SQLTimeout, run_query, validate_sql  # noqa: E402

failures = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global failures
    if condition:
        print(f"ok   {name}")
    else:
        failures += 1
        print(f"FAIL {name}{(': ' + detail) if detail else ''}")


def expect_rejected(sql: str) -> None:
    try:
        validate_sql(sql)
    except SQLRejected:
        return
    raise AssertionError(f"SQL was not rejected: {sql}")


def main() -> int:
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        return 2

    # Static SQL validation.
    for sql in (
        "DROP TABLE deals",
        "SELECT * FROM sqlite_master",
        "PRAGMA table_info(deals)",
        "SELECT load_extension('x')",
        "SELECT 1; DROP TABLE deals",
        "SELECT 1 -- comment",
        "ATTACH DATABASE '/tmp/x' AS x",
    ):
        try:
            expect_rejected(sql)
            check(f"reject {sql[:40]}", True)
        except AssertionError as exc:
            check(f"reject {sql[:40]}", False, str(exc))

    # The authorizer must also reject dangerous functions that pass the text
    # filter.
    for sql in (
        "SELECT randomblob(1000000000)",
        "SELECT zeroblob(1000000000)",
        "SELECT printf('%1000000000s', 'x')",
        "SELECT group_concat(deal_no) FROM deals",
        "SELECT * FROM pragma_table_info('deals')",
        "SELECT replace(product_family, 'а', 'аа') FROM deals",
        "WITH RECURSIVE c(x) AS (VALUES(1) UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c",
        "SELECT product_family || product_family FROM deals",
    ):
        try:
            run_query(DB_PATH, sql)
        except (SQLRejected, sqlite3.DatabaseError, SQLTimeout):
            check(f"authorizer {sql[:40]}", True)
        else:
            check(f"authorizer {sql[:40]}", False, "query executed")

    # Legitimate queries must still work after the allowlist was tightened.
    for sql in (
        "SELECT COUNT(*) FROM deals",
        "SELECT ROUND(SUM(amount_uzs), 2) FROM deals WHERE year = 2025",
        "SELECT COUNT(*) FROM deals WHERE seller_name LIKE '%Steel%'",
        "SELECT date, rate FROM rates WHERE date = '2025-01-01'",
        "SELECT lower(product_family), length(product_family) FROM deals LIMIT 1",
    ):
        try:
            run_query(DB_PATH, sql, max_rows=3)
            check(f"allow {sql[:45]}", True)
        except Exception as exc:  # noqa: BLE001
            check(f"allow {sql[:45]}", False, str(exc))

    # HTML injection through an ambiguous entity term/candidate.
    answer = render_ambiguous(
        "uz",
        "company",
        '<b onclick="x">Steel</b><script>alert(1)</script>',
        ["<img src=x onerror=alert(1)>", "ACME & CO"],
    )
    safe = sanitize_telegram_html(answer.text)
    check("HTML script escaped", "<script>" not in safe and "&lt;script&gt;" in safe)
    check("HTML img escaped", "<img" not in safe)
    check("formatting tag preserved", sanitize_telegram_html("<b>ok</b>") == "<b>ok</b>")
    check("unknown tag escaped", sanitize_telegram_html("<iframe>x</iframe>") == "&lt;iframe&gt;x&lt;/iframe&gt;")

    # Per-user quota: 12 questions/minute, then the 13th is rejected.
    context = SimpleNamespace(application=SimpleNamespace(bot_data={}))
    allowed = sum(1 for _ in range(12) if not _is_rate_limited(context, 123456789))
    blocked = _is_rate_limited(context, 123456789)
    check("rate limit per-user", allowed == 12 and blocked is True, f"allowed={allowed} blocked={blocked}")

    print(f"\n{'OK' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
