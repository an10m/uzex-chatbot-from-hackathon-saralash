"""The grounded UZEX question-answering agent."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .answers import (
    Answer,
    render_ambiguous,
    render_broker,
    render_company,
    render_contract,
    render_deal,
    render_error,
    render_metadata,
    render_need_date,
    render_not_found,
    render_query_answer,
    render_rate,
    render_unsupported,
)
from .entities import (
    Catalog,
    detect_language,
    resolve_broker,
    resolve_company,
    resolve_product,
    resolve_region,
    resolve_status,
)
from .llm import GeminiClient, LLMError
from .prompts import build_repair_prompt, build_system_prompt, build_user_prompt
from .sql_guard import SQLRejected, SQLTimeout, run_query
from .sql_repair import repair_sql

log = logging.getLogger(__name__)

_ALLOWED_INTENTS = {
    "sql", "lookup_deal", "lookup_contract", "lookup_company", "lookup_broker",
    "usd_rate", "metadata", "ambiguous", "not_found", "unsupported",
}
_METADATA_TOPICS = {"period", "files", "fields", "statuses", "calculation", "currency", "counts", "limitations"}


@dataclass
class AgentConfig:
    db_path: Path


def _parse_iso_date(value: Any) -> str | None:
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _extract_six_digit(question: str) -> list[int]:
    return [int(x) for x in re.findall(r"\b\d{6}\b", question)]


def _extract_date(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"\b(\d{1,2}[./]\d{1,2}[./]\d{4}|\d{4}-\d{2}-\d{2})\b", text)
    if m:
        return _parse_iso_date(m.group(1))
    return None


def _infer_metric(plan: dict, result_columns: list[str] | None = None) -> str:
    metric = (plan.get("metric") or "").strip()
    if metric in {"count", "sum_uzs", "sum_usd", "units", "avg_price", "avg_amount", "amount"}:
        return metric
    sql = (plan.get("sql") or "").lower()
    if "avg(amount" in sql or "avg( amount" in sql:
        return "avg_amount"
    cols = {c.lower() for c in (result_columns or [])}
    if any(c in cols for c in ("deals_count", "count", "cnt", "n")):
        return "count"
    if any(c in cols for c in ("total_uzs", "sum_uzs", "amount_uzs")):
        return "sum_uzs"
    if any(c in cols for c in ("total_usd", "sum_usd", "amount_usd")):
        return "sum_usd"
    if any(c in cols for c in ("units", "total_units", "volume")):
        return "units"
    if any(c in cols for c in ("avg_price", "avg_unit_price")):
        return "avg_price"
    if "count(" in sql:
        return "count"
    if "sum(amount_uzs" in sql:
        return "sum_uzs"
    if "sum(amount_usd" in sql:
        return "sum_usd"
    if "sum(units" in sql:
        return "units"
    return "other"


def _infer_answer_type(plan: dict, result: Any) -> str:
    at = (plan.get("answer_type") or "").strip()
    if at in {"scalar", "table", "record"}:
        return at
    if result is not None:
        if result.row_count == 1 and len(result.columns) == 1:
            return "scalar"
        if result.row_count == 1:
            return "record"
    return "table"


class Agent:
    def __init__(self, db_path: str | Path, llm: GeminiClient):
        self.db_path = Path(db_path)
        self.llm = llm
        # The bot only reads the registry.  Opening the catalog/metadata
        # connection read-only prevents an accidental write path entirely.
        self.con = sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row
        self.catalog = Catalog.from_db(self.con)
        self.meta = self._load_meta()
        self.system_prompt = build_system_prompt(self.catalog, self.meta)
        self.con.close()
        log.info(
            "Agent ready: %s deals, %s families, %s companies",
            self.meta.get("total_deals"), len(self.catalog.families), len(self.catalog.companies),
        )

    def _load_meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        for row in self.con.execute("SELECT key, value FROM metadata"):
            try:
                meta[row["key"]] = json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                meta[row["key"]] = row["value"]
        return meta

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def ask(self, question: str, history: list[dict] | None = None) -> Answer:
        started = time.monotonic()
        question = (question or "").strip()
        lang = detect_language(question)
        if not question:
            return render_error(lang)
        if len(question) > 2000:
            question = question[:2000]
        try:
            llm_result = await self.llm.generate_json(
                self.system_prompt,
                build_user_prompt(question, lang, history),
            )
            plan = self._normalize_plan(llm_result.data, lang)
            answer = await self._handle_plan(plan, question, lang, allow_retry=True)
        except LLMError as exc:
            log.error("LLM error: %s", exc)
            answer = render_error(lang)
        except Exception:
            log.exception("Unexpected agent error")
            answer = render_error(lang)
        answer.elapsed_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "Q=%r lang=%s kind=%s intent=%s rows=%s ms=%s",
            question[:180], lang, answer.kind, answer.plan.get("intent"),
            answer.row_count, answer.elapsed_ms,
        )
        return answer

    # ------------------------------------------------------------------
    # Plan handling
    # ------------------------------------------------------------------

    def _normalize_plan(self, plan: Any, lang: str) -> dict:
        if not isinstance(plan, dict):
            return {"language": lang, "intent": "unsupported", "reason": "invalid plan"}
        plan = dict(plan)
        plan["language"] = lang
        intent = str(plan.get("intent") or "").strip()
        if intent not in _ALLOWED_INTENTS:
            # Try a small deterministic fallback before giving up.
            fallback = self._fallback_intent(plan, lang)
            if fallback:
                return fallback
            return {"language": lang, "intent": "unsupported", "reason": "unknown intent"}
        if intent == "sql" and not str(plan.get("sql") or "").strip():
            return {"language": lang, "intent": "unsupported", "reason": "empty SQL"}
        return plan

    def _fallback_intent(self, plan: dict, lang: str) -> dict | None:
        q = str(plan.get("question") or "")
        numbers = _extract_six_digit(q)
        if numbers and ("shartnoma" in q.lower() or "контракт" in q.lower()):
            return {"language": lang, "intent": "lookup_contract", "contract_no": numbers[0]}
        if numbers and ("bitim" in q.lower() or "сделк" in q.lower()):
            return {"language": lang, "intent": "lookup_deal", "deal_no": numbers[0]}
        return None

    async def _handle_plan(self, plan: dict, question: str, lang: str, allow_retry: bool = True) -> Answer:
        intent = plan.get("intent")
        if intent == "sql":
            return await self._handle_sql(plan, question, lang, allow_retry)
        if intent == "lookup_deal":
            return await self._handle_lookup_deal(plan, question, lang)
        if intent == "lookup_contract":
            return await self._handle_lookup_contract(plan, question, lang)
        if intent == "lookup_company":
            return await self._handle_lookup_company(plan, lang)
        if intent == "lookup_broker":
            return await self._handle_lookup_broker(plan, lang)
        if intent == "usd_rate":
            return await self._handle_usd_rate(plan, question, lang)
        if intent == "metadata":
            topic = str(plan.get("topic") or "period").strip().lower()
            if topic not in _METADATA_TOPICS:
                topic = "period"
            return render_metadata(topic, self.meta, self.catalog, lang)
        if intent == "not_found":
            return render_not_found(lang, "", self.meta)
        if intent == "unsupported":
            return render_unsupported(lang, "", self.meta)
        if intent == "ambiguous":
            return await self._handle_ambiguous(plan, question, lang)
        return render_error(lang)

    async def _handle_sql(self, plan: dict, question: str, lang: str, allow_retry: bool) -> Answer:
        sql = str(plan.get("sql") or "").strip()
        repair = repair_sql(sql, self.catalog)
        if repair.ambiguous_type:
            return render_ambiguous(lang, repair.ambiguous_type, repair.ambiguous_term or "", repair.candidates, self.meta)
        if repair.not_found:
            return render_not_found(lang, "", self.meta)
        plan = dict(plan)
        plan["sql"] = repair.sql
        try:
            result = await asyncio.to_thread(run_query, self.db_path, repair.sql)
        except (SQLRejected, sqlite3.OperationalError, SQLTimeout) as exc:
            log.warning("SQL failed: %s | %s", exc, repair.sql[:500])
            if allow_retry:
                try:
                    fixed = await self.llm.generate_json(
                        self.system_prompt,
                        build_repair_prompt(question, lang, plan, str(exc)),
                    )
                    fixed_plan = self._normalize_plan(fixed.data, lang)
                    return await self._handle_plan(fixed_plan, question, lang, allow_retry=False)
                except LLMError:
                    log.error("SQL repair LLM call failed")
            return render_error(lang)
        if result.row_count == 0:
            return render_not_found(lang, "", self.meta)

        plan["metric"] = _infer_metric(plan, result.columns)
        if "avg(amount" in repair.sql.lower() and plan["metric"] in ("sum_uzs", "sum_usd", "amount"):
            plan["metric"] = "avg_amount"
        plan["answer_type"] = _infer_answer_type(plan, result)
        answer = render_query_answer(question, plan, result, lang, self.meta)
        if repair.notes:
            answer.text += "\n\n" + " ".join(repair.notes)
        return answer

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    async def _handle_lookup_deal(self, plan: dict, question: str, lang: str) -> Answer:
        deal_no = plan.get("deal_no")
        try:
            deal_no = int(deal_no) if deal_no is not None else None
        except (TypeError, ValueError):
            deal_no = None
        if deal_no is None:
            numbers = _extract_six_digit(question)
            if len(numbers) == 1:
                deal_no = numbers[0]
        if deal_no is None:
            return render_not_found(lang, "", self.meta)
        sql = f"SELECT * FROM deals WHERE deal_no = {deal_no}"
        result = await asyncio.to_thread(run_query, self.db_path, sql)
        if result.row_count == 0:
            return render_not_found(lang, "", self.meta)
        row = result.as_dicts(1)[0]
        return render_deal(row, lang, self.meta)

    async def _handle_lookup_contract(self, plan: dict, question: str, lang: str) -> Answer:
        contract_no = plan.get("contract_no")
        try:
            contract_no = int(contract_no) if contract_no is not None else None
        except (TypeError, ValueError):
            contract_no = None
        if contract_no is None:
            numbers = _extract_six_digit(question)
            if len(numbers) == 1:
                contract_no = numbers[0]
        if contract_no is None:
            return render_not_found(lang, "", self.meta)
        sql = f"SELECT * FROM contracts WHERE contract_no = {contract_no}"
        result = await asyncio.to_thread(run_query, self.db_path, sql)
        if result.row_count == 0:
            return render_not_found(lang, "", self.meta)
        row = result.as_dicts(1)[0]
        return render_contract(row, lang, self.meta)

    async def _handle_lookup_company(self, plan: dict, lang: str) -> Answer:
        term = str(plan.get("company") or "").strip()
        if not term:
            return render_not_found(lang, "", self.meta)
        res = resolve_company(term, self.catalog.companies)
        if res.ambiguous:
            return render_ambiguous(lang, "company", term, [c[1] for c in res.candidates], self.meta)
        if not res.values:
            return render_not_found(lang, "", self.meta)
        inn = res.values[0]
        sql = f"SELECT * FROM participants WHERE inn = '{inn}'"
        result = await asyncio.to_thread(run_query, self.db_path, sql)
        if result.row_count == 0:
            return render_not_found(lang, "", self.meta)
        company = result.as_dicts(1)[0]
        stats_sql = (
            "SELECT "
            f"(SELECT COUNT(*) FROM deals WHERE seller_inn = '{inn}') AS seller_deals, "
            f"(SELECT COALESCE(SUM(amount_uzs), 0) FROM deals WHERE seller_inn = '{inn}') AS seller_total_uzs, "
            f"(SELECT COUNT(*) FROM deals WHERE buyer_inn = '{inn}') AS buyer_deals, "
            f"(SELECT COALESCE(SUM(amount_uzs), 0) FROM deals WHERE buyer_inn = '{inn}') AS buyer_total_uzs"
        )
        stats_result = await asyncio.to_thread(run_query, self.db_path, stats_sql)
        stats = stats_result.as_dicts(1)[0] if stats_result.row_count else {}
        return render_company(company, stats, lang, self.meta)

    async def _handle_lookup_broker(self, plan: dict, lang: str) -> Answer:
        term = str(plan.get("broker") or "").strip()
        if not term:
            return render_not_found(lang, "", self.meta)
        res = resolve_broker(term, self.catalog.brokers)
        if res.ambiguous:
            return render_ambiguous(lang, "broker", term, [c[1] for c in res.candidates], self.meta)
        if not res.values:
            return render_not_found(lang, "", self.meta)
        code = res.values[0]
        sql = f"SELECT * FROM brokers WHERE code = '{code}'"
        result = await asyncio.to_thread(run_query, self.db_path, sql)
        if result.row_count == 0:
            return render_not_found(lang, "", self.meta)
        broker = result.as_dicts(1)[0]
        stats_sql = (
            "SELECT "
            f"(SELECT COUNT(*) FROM deals WHERE seller_broker = '{code}') AS seller_deals, "
            f"(SELECT COUNT(*) FROM deals WHERE buyer_broker = '{code}') AS buyer_deals"
        )
        stats_result = await asyncio.to_thread(run_query, self.db_path, stats_sql)
        stats = stats_result.as_dicts(1)[0] if stats_result.row_count else {}
        return render_broker(broker, stats, lang, self.meta)

    async def _handle_usd_rate(self, plan: dict, question: str, lang: str) -> Answer:
        date = _parse_iso_date(plan.get("date"))
        if not date:
            # The model may have left the date out; try to find one in the question.
            date = _extract_date(question)
        if not date:
            return render_need_date(lang)
        sql = f"SELECT date, rate FROM rates WHERE date = '{date}'"
        result = await asyncio.to_thread(run_query, self.db_path, sql)
        if result.row_count == 0:
            return render_not_found(lang, "", self.meta)
        row = result.as_dicts(1)[0]
        amount = plan.get("amount")
        try:
            amount = float(amount) if amount is not None and str(amount).strip() != "" else None
            if amount is not None and (not math.isfinite(amount) or amount < 0):
                amount = None
        except (TypeError, ValueError):
            amount = None
        currency = str(plan.get("currency") or "USD").upper()
        if currency not in ("USD", "UZS"):
            currency = "USD"
        return render_rate(row["date"], float(row["rate"]), lang, self.meta, amount=amount, currency=currency)

    # ------------------------------------------------------------------
    # Ambiguity / clarification
    # ------------------------------------------------------------------

    async def _handle_ambiguous(self, plan: dict, question: str, lang: str) -> Answer:
        entity_type = str(plan.get("entity_type") or "").strip().lower()
        term = str(plan.get("entity_term") or "").strip()
        if not term:
            numbers = _extract_six_digit(question)
            if len(numbers) == 1:
                return await self._handle_lookup_deal({"deal_no": numbers[0]}, question, lang)
            return render_not_found(lang, "", self.meta)

        if entity_type == "product":
            res = resolve_product(term, self.catalog.families)
            if res.ambiguous:
                return render_ambiguous(lang, "product", term, [c[1] for c in res.candidates], self.meta)
            if res.values:
                # Resolved uniquely: let the model retry with the canonical value.
                return await self._retry_with_entity(question, lang, f"product_family = '{res.values[0]}'")
            return render_ambiguous(lang, "product", term, [c[1] for c in res.candidates], self.meta)

        if entity_type == "region":
            res = resolve_region(term, self.catalog.regions)
            if set(res.values) == {"Toshkent shahri", "Toshkent viloyati"}:
                return await self._retry_with_entity(
                    question,
                    lang,
                    "region IN ('Toshkent shahri', 'Toshkent viloyati')",
                )
            if res.ambiguous:
                return render_ambiguous(lang, "region", term, [c[1] for c in res.candidates], self.meta)
            if res.values:
                return await self._retry_with_entity(question, lang, f"region = '{res.values[0]}'")
            return render_ambiguous(lang, "region", term, [c[1] for c in res.candidates], self.meta)

        if entity_type == "status":
            res = resolve_status(term, self.catalog.statuses)
            if res.ambiguous:
                return render_ambiguous(lang, "status", term, [c[1] for c in res.candidates], self.meta)
            if res.values:
                return await self._retry_with_entity(question, lang, f"status = '{res.values[0]}'")
            return render_ambiguous(lang, "status", term, [c[1] for c in res.candidates], self.meta)

        if entity_type == "company":
            res = resolve_company(term, self.catalog.companies)
            if res.ambiguous:
                return render_ambiguous(lang, "company", term, [c[1] for c in res.candidates], self.meta)
            if res.values:
                return await self._handle_lookup_company({"company": term}, lang)
            return render_ambiguous(lang, "company", term, [c[1] for c in res.candidates], self.meta)

        if entity_type == "broker":
            res = resolve_broker(term, self.catalog.brokers)
            if res.ambiguous:
                return render_ambiguous(lang, "broker", term, [c[1] for c in res.candidates], self.meta)
            if res.values:
                return await self._handle_lookup_broker({"broker": term}, lang)
            return render_ambiguous(lang, "broker", term, [c[1] for c in res.candidates], self.meta)

        return render_ambiguous(lang, entity_type or "entity", term, [c[1] for c in res.candidates] if 'res' in locals() else [], self.meta)

    async def _retry_with_entity(self, question: str, lang: str, resolved_filter: str) -> Answer:
        prompt = build_user_prompt(question, lang) + (
            f"\n\nBackend entity resolution: {resolved_filter}. "
            "Use this canonical value and generate the SQL plan. Do not ask for clarification."
        )
        try:
            fixed = await self.llm.generate_json(self.system_prompt, prompt)
            plan = self._normalize_plan(fixed.data, lang)
            return await self._handle_plan(plan, question, lang, allow_retry=False)
        except LLMError:
            return render_error(lang)
