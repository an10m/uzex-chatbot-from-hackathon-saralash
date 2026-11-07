"""Read-only SQL execution with hard limits.

The LLM generates SQL, so this module treats it as untrusted input:
* only SELECT / WITH ... SELECT is allowed;
* SQLite runs on a read-only connection;
* an authorizer permits only SELECT/READ/FUNCTION;
* only the whitelisted tables can be read;
* a progress handler stops long queries;
* at most MAX_ROWS rows are returned.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MAX_ROWS = 200
QUERY_TIMEOUT_SECONDS = 8.0
MAX_SQL_LENGTH = 6000

_ALLOWED_TABLES = {
    "deals", "contracts", "participants", "brokers", "rates", "metadata",
}

# SQLite has a large built-in function surface.  The model only needs a small
# set of aggregate, string and math helpers.  An allowlist is safer than a
# denylist: functions such as randomblob(), zeroblob(), printf() and
# group_concat() can allocate huge amounts of memory or CPU even on a
# read-only database.
_ALLOWED_FUNCTIONS = {
    # aggregates
    "avg", "count", "max", "min", "sum", "total",
    # null / conditional helpers
    "coalesce", "ifnull", "iif", "nullif",
    # numbers
    "abs", "ceil", "ceiling", "exp", "floor", "ln", "log", "log10",
    "mod", "pi", "pow", "power", "round", "sign", "sqrt", "trunc",
    # strings (only shrinking/bounded helpers: replace(), concat(), concat_ws(),
    # hex() and || are deliberately excluded because nested calls can amplify
    # a tiny input into a huge allocation)
    "instr", "length", "lower", "ltrim", "rtrim", "substr", "substring",
    "trim", "unicode", "upper",
    # type inspection and date helpers
    "date", "datetime", "julianday", "strftime", "time", "timediff",
    "typeof", "unixepoch", "current_date", "current_time", "current_timestamp",
    # query planner hints and LIKE/GLOB operators
    "glob", "like", "likelihood", "likely", "unlikely",
}

_FORBIDDEN = re.compile(
    r"\b(PRAGMA|ATTACH|DETACH|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|"
    r"VACUUM|ANALYZE|REINDEX|LOAD_EXTENSION|READFILE|WRITEFILE|TRIGGER|"
    r"VIEW|INDEX|TABLE|RECURSIVE|sqlite_master|sqlite_schema|sqlite_temp_master)\b",
    re.IGNORECASE,
)


class SQLRejected(ValueError):
    pass


class SQLTimeout(RuntimeError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    elapsed_ms: int

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def as_dicts(self, limit: int | None = None) -> list[dict]:
        rows = self.rows[:limit] if limit else self.rows
        return [dict(zip(self.columns, r)) for r in rows]


def validate_sql(sql: str) -> str:
    if not sql or not sql.strip():
        raise SQLRejected("empty SQL")
    s = sql.strip()
    if len(s) > MAX_SQL_LENGTH:
        raise SQLRejected("SQL too long")
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if ";" in s:
        raise SQLRejected("multiple statements are not allowed")
    if "--" in s or "/*" in s or "*/" in s:
        raise SQLRejected("SQL comments are not allowed")
    if "||" in s:
        raise SQLRejected("string concatenation is not allowed")
    if not re.match(r"^(SELECT|WITH)\b", s, re.IGNORECASE):
        raise SQLRejected("only SELECT queries are allowed")
    if _FORBIDDEN.search(s):
        raise SQLRejected("forbidden SQL keyword or table")
    if not re.search(r"\bSELECT\b", s, re.IGNORECASE):
        raise SQLRejected("no SELECT found")
    return s


def _authorizer(action: int, arg1: str | None, arg2: str | None, dbname: str | None, trigger: str | None) -> int:
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_RECURSIVE:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        if arg1 in _ALLOWED_TABLES:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        name = (arg2 or "").lower()
        if name in _ALLOWED_FUNCTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    # Deny PRAGMA, transactions, all writes, DDL, etc.
    return sqlite3.SQLITE_DENY


def run_query(db_path: str | Path, sql: str, max_rows: int = MAX_ROWS) -> QueryResult:
    sql = validate_sql(sql)
    try:
        max_rows = max(1, min(int(max_rows), MAX_ROWS))
    except (TypeError, ValueError):
        max_rows = MAX_ROWS
    started = time.monotonic()
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA temp_store=FILE")
        con.execute("PRAGMA cache_size=-32000")
        # The database is an application-owned artifact, not a user schema.
        try:
            con.execute("PRAGMA trusted_schema=OFF")
        except sqlite3.DatabaseError:
            pass
        con.set_authorizer(_authorizer)

        def progress() -> int:
            return 1 if time.monotonic() - started > QUERY_TIMEOUT_SECONDS else 0

        con.set_progress_handler(progress, 10000)
        cur = con.execute(sql)
        if cur.description is None:
            raise SQLRejected("query returned no result set")
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        if truncated:
            rows = rows[:max_rows]
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return QueryResult(columns=columns, rows=rows, truncated=truncated, elapsed_ms=elapsed_ms)
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise SQLTimeout("query timed out") from exc
        raise
    finally:
        con.set_progress_handler(None, 0)
        con.close()
