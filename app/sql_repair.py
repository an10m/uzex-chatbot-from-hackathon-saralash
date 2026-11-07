"""Resolve human names inside LLM-generated SQL to canonical DB values.

The model is told to use canonical values, but this layer makes the system
robust: a product alias, a company short name or an Uzbek region name is
resolved against the actual database before the query runs.  If a name is
ambiguous, the query is not executed — the bot asks a clarification question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .entities import (
    Catalog,
    resolve_broker,
    resolve_company,
    resolve_product,
    resolve_region,
    resolve_simple,
    resolve_status,
    TYPE_ALIASES,
    FORM_ALIASES,
    AUCTION_ALIASES,
    UNIT_ALIASES,
)


@dataclass
class RepairResult:
    sql: str
    notes: list[str] = field(default_factory=list)
    not_found: str | None = None
    ambiguous_type: str | None = None
    ambiguous_term: str | None = None
    candidates: list[str] = field(default_factory=list)


_ENTITY_COLUMNS = {
    "product_family": "product",
    "region": "region",
    "status": "status",
    "contract_type": "contract_type",
    "form": "form",
    "auction_type": "auction_type",
    "unit": "unit",
    "seller_name": "seller",
    "buyer_name": "buyer",
    "seller_broker": "seller_broker",
    "buyer_broker": "buyer_broker",
}


def _unquote(value: str) -> str:
    return value.replace("''", "'")


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _resolve_one(col: str, value: str, catalog: Catalog) -> tuple[list[str], bool, list[str]]:
    """Return (canonical_values, ambiguous, candidate_labels)."""
    kind = _ENTITY_COLUMNS.get(col.lower())
    if kind == "product":
        r = resolve_product(value, catalog.families)
    elif kind == "region":
        r = resolve_region(value, catalog.regions)
    elif kind == "status":
        r = resolve_status(value, catalog.statuses)
    elif kind == "contract_type":
        r = resolve_simple(value, catalog.contract_types, TYPE_ALIASES)
    elif kind == "form":
        r = resolve_simple(value, catalog.forms, FORM_ALIASES)
    elif kind == "auction_type":
        r = resolve_simple(value, catalog.auction_types, AUCTION_ALIASES)
    elif kind == "unit":
        r = resolve_simple(value, catalog.units, UNIT_ALIASES)
    elif kind in ("seller", "buyer"):
        r = resolve_company(value, catalog.companies)
        if r.values:
            return [f"INN:{r.values[0]}"], False, []
        return [], r.ambiguous, [c[1] for c in r.candidates]
    elif kind in ("seller_broker", "buyer_broker"):
        r = resolve_broker(value, catalog.brokers)
        if r.values:
            return [f"CODE:{r.values[0]}"], False, []
        return [], r.ambiguous, [c[1] for c in r.candidates]
    else:
        return [value], False, []
    # Toshkent without a qualifier is intentionally expanded to city + region.
    if kind == "region" and set(r.values) == {"Toshkent shahri", "Toshkent viloyati"}:
        return r.values, False, []
    # A status alias such as "unpaid" deliberately covers several statuses.
    if kind == "status" and r.values:
        return r.values, False, []
    if r.ambiguous:
        return [], True, [c[1] for c in r.candidates]
    if r.values:
        return r.values, False, []
    return [], False, [c[1] for c in r.candidates]


def repair_sql(sql: str, catalog: Catalog) -> RepairResult:
    result = RepairResult(sql=sql)

    def replace_equality(match: re.Match) -> str:
        col, raw = match.group(1), _unquote(match.group(2))
        values, ambiguous, candidates = _resolve_one(col, raw, catalog)
        if ambiguous:
            result.ambiguous_type = _ENTITY_COLUMNS.get(col.lower(), col)
            result.ambiguous_term = raw
            result.candidates = candidates
            return match.group(0)
        if not values:
            result.not_found = f"«{raw}» bo‘yicha mos yozuv topilmadi"
            return match.group(0)
        if _ENTITY_COLUMNS.get(col.lower()) in ("seller", "buyer"):
            inn = values[0].split(":", 1)[1]
            target = "seller_inn" if _ENTITY_COLUMNS[col.lower()] == "seller" else "buyer_inn"
            return f"{target} = {_quote(inn)}"
        if _ENTITY_COLUMNS.get(col.lower()) in ("seller_broker", "buyer_broker"):
            code = values[0].split(":", 1)[1]
            target = "seller_broker" if _ENTITY_COLUMNS[col.lower()] == "seller_broker" else "buyer_broker"
            return f"{target} = {_quote(code)}"
        if len(values) == 1:
            return f"{col} = {_quote(values[0])}"
        return f"{col} IN ({', '.join(_quote(v) for v in values)})"

    def replace_in(match: re.Match) -> str:
        col, raw_list = match.group(1), match.group(2)
        literals = re.findall(r"'((?:''|[^'])*)'", raw_list)
        if not literals:
            return match.group(0)
        all_values: list[str] = []
        for raw in literals:
            value = _unquote(raw)
            values, ambiguous, candidates = _resolve_one(col, value, catalog)
            if ambiguous:
                result.ambiguous_type = _ENTITY_COLUMNS.get(col.lower(), col)
                result.ambiguous_term = value
                result.candidates = candidates
                return match.group(0)
            if not values:
                result.not_found = f"«{value}» bo‘yicha mos yozuv topilmadi"
                return match.group(0)
            all_values.extend(values)
        # De-duplicate while preserving order.
        seen = set()
        unique = []
        for v in all_values:
            if v not in seen:
                seen.add(v)
                unique.append(v)
        if _ENTITY_COLUMNS.get(col.lower()) in ("seller", "buyer"):
            inns = [v.split(":", 1)[1] for v in unique]
            target = "seller_inn" if _ENTITY_COLUMNS[col.lower()] == "seller" else "buyer_inn"
            return f"{target} IN ({', '.join(_quote(v) for v in inns)})"
        if _ENTITY_COLUMNS.get(col.lower()) in ("seller_broker", "buyer_broker"):
            codes = [v.split(":", 1)[1] for v in unique]
            target = "seller_broker" if _ENTITY_COLUMNS[col.lower()] == "seller_broker" else "buyer_broker"
            return f"{target} IN ({', '.join(_quote(v) for v in codes)})"
        return f"{col} IN ({', '.join(_quote(v) for v in unique)})"

    def replace_like(match: re.Match) -> str:
        col, raw = match.group(1), _unquote(match.group(2))
        values, ambiguous, candidates = _resolve_one(col, raw, catalog)
        if ambiguous:
            result.ambiguous_type = _ENTITY_COLUMNS.get(col.lower(), col)
            result.ambiguous_term = raw
            result.candidates = candidates
            return match.group(0)
        if not values:
            result.not_found = f"«{raw}» bo‘yicha mos yozuv topilmadi"
            return match.group(0)
        if _ENTITY_COLUMNS.get(col.lower()) in ("seller", "buyer"):
            inn = values[0].split(":", 1)[1]
            target = "seller_inn" if _ENTITY_COLUMNS[col.lower()] == "seller" else "buyer_inn"
            return f"{target} = {_quote(inn)}"
        if _ENTITY_COLUMNS.get(col.lower()) in ("seller_broker", "buyer_broker"):
            code = values[0].split(":", 1)[1]
            target = "seller_broker" if _ENTITY_COLUMNS[col.lower()] == "seller_broker" else "buyer_broker"
            return f"{target} = {_quote(code)}"
        if len(values) == 1:
            return f"{col} = {_quote(values[0])}"
        return f"{col} IN ({', '.join(_quote(v) for v in values)})"

    # Only rewrite simple comparison forms; the model is instructed to use them.
    sql = re.sub(
        r"\b(product_family|region|status|contract_type|form|auction_type|unit|seller_name|buyer_name|seller_broker|buyer_broker)\s*=\s*'((?:''|[^'])*)'",
        replace_equality,
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\b(product_family|region|status|contract_type|form|auction_type|unit|seller_name|buyer_name|seller_broker|buyer_broker)\s+IN\s*\(\s*('(?:''|[^'])*'(?:\s*,\s*'(?:''|[^'])*')*)\s*\)",
        replace_in,
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\b(product_family|region|status|contract_type|form|auction_type|unit|seller_name|buyer_name|seller_broker|buyer_broker)\s+LIKE\s+'%((?:''|[^'])*)%'",
        replace_like,
        sql,
        flags=re.IGNORECASE,
    )

    # If Toshkent was expanded to both city and region, add a transparent note.
    if "Toshkent shahri" in sql and "Toshkent viloyati" in sql:
        result.notes.append("Toshkent shahri va Toshkent viloyati birgalikda hisobga olingan.")

    result.sql = sql
    return result
