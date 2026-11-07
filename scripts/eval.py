"""Offline and live evaluation for the UZEX agent.

Offline mode exercises the deterministic backend (SQL guard, entity repair,
rendering) with a scripted plan for every question.  It needs no API key.

Live mode sends the same questions to Gemini and checks the answers against
independent reference SQL.  Run it after putting GEMINI_API_KEY in .env:

    .venv/bin/python scripts/eval.py --offline
    .venv/bin/python scripts/eval.py --live
    .venv/bin/python scripts/eval.py --live --filter 2025

The reference SQL is deliberately written separately from the planner prompt,
so a failure points at the agent rather than at a copied example.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent import Agent  # noqa: E402
from app.answers import fmt_date, fmt_money, fmt_number  # noqa: E402
from app.config import DB_PATH, GEMINI_API_KEY, GEMINI_MODEL  # noqa: E402
from app.llm import GeminiClient, LLMResult  # noqa: E402


@dataclass
class Check:
    sql: str
    fmt: str = "int"
    index: int = 0
    label: str = ""


@dataclass
class Case:
    id: str
    lang: str
    question: str
    plan: dict[str, Any] | None = None
    checks: list[Check] = field(default_factory=list)
    contains: list[str] = field(default_factory=list)
    not_contains: list[str] = field(default_factory=list)
    expected_kind: str = "answer"
    source: bool = True


def _sql_case(
    cid: str,
    lang: str,
    question: str,
    checks: list[Check],
    *,
    plan_sql: str | None = None,
    metric: str = "count",
    answer_type: str = "scalar",
    contains: list[str] | None = None,
    source: bool = True,
) -> Case:
    sql = plan_sql or (checks[0].sql if checks else "")
    return Case(
        id=cid,
        lang=lang,
        question=question,
        plan={
            "intent": "sql",
            "sql": sql,
            "metric": metric,
            "answer_type": answer_type,
            "includes_canceled": True,
        },
        checks=checks,
        contains=contains or [],
        source=source,
    )


CASES: list[Case] = [
    # --- counts / sums ------------------------------------------------------
    _sql_case(
        "uz_2025_count", "uz", "2025-yilda nechta bitim tuzilgan?",
        [Check("SELECT COUNT(*) FROM deals WHERE year = 2025")],
    ),
    _sql_case(
        "ru_2025_count", "ru", "Сколько сделок было в 2025 году?",
        [Check("SELECT COUNT(*) FROM deals WHERE year = 2025")],
    ),
    _sql_case(
        "uz_2025_sum_uzs", "uz", "2025-yilda bitimlarning umumiy summasi qancha?",
        [Check("SELECT SUM(amount_uzs) FROM deals WHERE year = 2025", "money")],
        metric="sum_uzs",
    ),
    _sql_case(
        "ru_2025_sum_usd", "ru", "Какая общая сумма всех сделок в долларах США?",
        [Check("SELECT SUM(amount_usd) FROM deals", "money")],
        metric="sum_usd",
    ),
    _sql_case(
        "uz_cotton_2025", "uz", "2025-yilda paxta bo‘yicha qancha bitim va qancha kilogramm?",
        [
            Check("SELECT COUNT(*) FROM deals WHERE year = 2025 AND product_family = 'Хлопок-сырец' AND unit = 'килограмм'"),
            Check("SELECT SUM(units) FROM deals WHERE year = 2025 AND product_family = 'Хлопок-сырец' AND unit = 'килограмм'", "units"),
        ],
        plan_sql=(
            "SELECT COUNT(*) AS deals_count, ROUND(SUM(units), 2) AS units, unit "
            "FROM deals WHERE year = 2025 AND product_family = 'хлопок' AND unit = 'kg' GROUP BY unit"
        ),
        metric="units",
        answer_type="table",
    ),
    _sql_case(
        "ru_cement_2025", "ru", "Сколько сделок с цементом было в 2025 году и на какую сумму?",
        [
            Check("SELECT COUNT(*) FROM deals WHERE year = 2025 AND product_family = 'Портландцемент'"),
            Check("SELECT SUM(amount_uzs) FROM deals WHERE year = 2025 AND product_family = 'Портландцемент'", "money"),
        ],
        plan_sql=(
            "SELECT COUNT(*) AS deals_count, ROUND(SUM(amount_uzs), 2) AS total_uzs "
            "FROM deals WHERE year = 2025 AND product_family = 'Портландцемент'"
        ),
        metric="sum_uzs",
        answer_type="table",
    ),
    _sql_case(
        "uz_export_2025", "uz", "2025-yilda eksport bitimlari soni va summasi qancha dollar?",
        [
            Check("SELECT COUNT(*) FROM deals WHERE year = 2025 AND contract_type = 'Экспортный'"),
            Check("SELECT SUM(amount_usd) FROM deals WHERE year = 2025 AND contract_type = 'Экспортный'", "money"),
        ],
        plan_sql=(
            "SELECT COUNT(*) AS deals_count, ROUND(SUM(amount_usd), 2) AS total_usd "
            "FROM deals WHERE year = 2025 AND contract_type = 'export'"
        ),
        metric="sum_usd",
        answer_type="table",
    ),
    _sql_case(
        "ru_canceled_2025", "ru", "Сколько аннулированных сделок было в 2025 году и на какую сумму?",
        [
            Check("SELECT COUNT(*) FROM deals WHERE year = 2025 AND is_canceled = 1"),
            Check("SELECT SUM(amount_uzs) FROM deals WHERE year = 2025 AND is_canceled = 1", "money"),
        ],
        plan_sql=(
            "SELECT COUNT(*) AS deals_count, ROUND(SUM(amount_uzs), 2) AS total_uzs "
            "FROM deals WHERE year = 2025 AND is_canceled = 1"
        ),
        metric="sum_uzs",
        answer_type="table",
    ),
    _sql_case(
        "uz_completed_2025", "uz", "2025-yilda bajarilgan bitimlar soni qancha?",
        [Check("SELECT COUNT(*) FROM deals WHERE year = 2025 AND is_completed = 1")],
        plan_sql="SELECT COUNT(*) AS deals_count FROM deals WHERE year = 2025 AND status = 'completed'",
    ),
    _sql_case(
        "uz_oct_2026", "uz", "2026-yil oktabr oyida nechta bitim tuzilgan?",
        [Check("SELECT COUNT(*) FROM deals WHERE month_key = '2026-10'")],
    ),
    _sql_case(
        "ru_years_compare", "ru", "Сравните 2024 и 2025 годы по количеству сделок.",
        [
            Check("SELECT COUNT(*) FROM deals WHERE year = 2024"),
            Check("SELECT COUNT(*) FROM deals WHERE year = 2025"),
        ],
        plan_sql="SELECT year, COUNT(*) AS deals_count FROM deals WHERE year IN (2024, 2025) GROUP BY year ORDER BY year",
        metric="count",
        answer_type="table",
    ),
    _sql_case(
        "uz_jan_2025", "uz", "2025-yil yanvar oyida nechta bitim tuzilgan?",
        [Check("SELECT COUNT(*) FROM deals WHERE month_key = '2025-01'")],
    ),
    _sql_case(
        "ru_q1_2025", "ru", "Сколько сделок было в первом квартале 2025 года?",
        [Check("SELECT COUNT(*) FROM deals WHERE year = 2025 AND quarter = 1")],
    ),
    _sql_case(
        "uz_big_deals", "uz", "Nechta bitim summasi 1 milliard so‘mdan oshgan?",
        [Check("SELECT COUNT(*) FROM deals WHERE amount_uzs > 1000000000")],
    ),
    _sql_case(
        "ru_avg_deal_2025", "ru", "Какой средний размер сделки в 2025 году?",
        [Check("SELECT AVG(amount_uzs) FROM deals WHERE year = 2025", "money")],
        metric="avg_amount",
    ),

    # --- filters and repairs ------------------------------------------------
    _sql_case(
        "ru_wheat_2025", "ru", "Сколько пшеницы продано в 2025 году и на какую сумму?",
        [
            Check("SELECT COUNT(*) FROM deals WHERE year = 2025 AND product_family = 'Пшеница продовольственная'"),
            Check("SELECT SUM(amount_uzs) FROM deals WHERE year = 2025 AND product_family = 'Пшеница продовольственная'", "money"),
        ],
        plan_sql=(
            "SELECT COUNT(*) AS deals_count, ROUND(SUM(amount_uzs), 2) AS total_uzs "
            "FROM deals WHERE year = 2025 AND product_family = 'wheat'"
        ),
        metric="sum_uzs",
        answer_type="table",
    ),
    _sql_case(
        "uz_samarkand_2025", "uz", "2025-yilda Samarqand viloyatida nechta bitim tuzilgan?",
        [Check("SELECT COUNT(*) FROM deals WHERE year = 2025 AND region = 'Samarqand viloyati'")],
        plan_sql="SELECT COUNT(*) AS deals_count FROM deals WHERE year = 2025 AND region = 'Samarkand'",
    ),
    _sql_case(
        "uz_forward_2025", "uz", "2025-yilda forvard bitimlari nechta?",
        [Check("SELECT COUNT(*) FROM deals WHERE year = 2025 AND form = 'Форвард'")],
        plan_sql="SELECT COUNT(*) AS deals_count FROM deals WHERE year = 2025 AND form = 'forward'",
    ),
    _sql_case(
        "ru_reverse_2025", "ru", "Сколько обратных аукционов было в 2025 году?",
        [Check("SELECT COUNT(*) FROM deals WHERE year = 2025 AND auction_type = 'Обратный аукцион'")],
        plan_sql="SELECT COUNT(*) AS deals_count FROM deals WHERE year = 2025 AND auction_type = 'reverse'",
    ),
    _sql_case(
        "uz_status_new_2025", "uz", "2025-yilda «Новый» holatidagi bitimlar nechta?",
        [Check("SELECT COUNT(*) FROM deals WHERE year = 2025 AND status = 'Новый'")],
        plan_sql="SELECT COUNT(*) AS deals_count FROM deals WHERE year = 2025 AND status = 'new'",
    ),
    _sql_case(
        "ru_ton_products", "ru", "Какой товар больше всего продан в тоннах?",
        [Check("SELECT product_family FROM deals WHERE unit = 'тонна' GROUP BY product_family ORDER BY SUM(units) DESC LIMIT 1", "name")],
        plan_sql=(
            "SELECT product_family, ROUND(SUM(units), 2) AS units FROM deals "
            "WHERE unit = 'tonna' GROUP BY product_family ORDER BY units DESC LIMIT 5"
        ),
        metric="units",
        answer_type="table",
    ),
    _sql_case(
        "uz_tashkent_both", "uz", "Toshkentdagi eng yirik xaridor kim?",
        [Check(
            "SELECT buyer_name FROM deals WHERE region IN ('Toshkent shahri', 'Toshkent viloyati') "
            "GROUP BY buyer_inn, buyer_name ORDER BY SUM(amount_uzs) DESC LIMIT 1",
            "name",
        )],
        plan_sql=(
            "SELECT buyer_name, ROUND(SUM(amount_uzs), 2) AS total_uzs FROM deals "
            "WHERE region = 'Ташкент' GROUP BY buyer_inn, buyer_name ORDER BY total_uzs DESC LIMIT 1"
        ),
        metric="sum_uzs",
        answer_type="table",
    ),

    # --- rankings -----------------------------------------------------------
    _sql_case(
        "uz_top_sellers", "uz", "Eng ko‘p bitim tuzgan 5 ta sotuvchi kimlar?",
        [
            Check("SELECT seller_name FROM deals GROUP BY seller_inn, seller_name ORDER BY COUNT(*) DESC LIMIT 1", "name"),
            Check("SELECT seller_name FROM deals GROUP BY seller_inn, seller_name ORDER BY COUNT(*) DESC LIMIT 1 OFFSET 1", "name"),
        ],
        plan_sql="SELECT seller_name, COUNT(*) AS deals_count FROM deals GROUP BY seller_inn, seller_name ORDER BY deals_count DESC LIMIT 5",
        metric="count",
        answer_type="table",
    ),
    _sql_case(
        "ru_top_buyers", "ru", "Топ-5 покупателей по сумме сделок.",
        [
            Check("SELECT buyer_name FROM deals GROUP BY buyer_inn, buyer_name ORDER BY SUM(amount_uzs) DESC LIMIT 1", "name"),
            Check("SELECT buyer_name FROM deals GROUP BY buyer_inn, buyer_name ORDER BY SUM(amount_uzs) DESC LIMIT 1 OFFSET 1", "name"),
        ],
        plan_sql=(
            "SELECT buyer_name, ROUND(SUM(amount_uzs), 2) AS total_uzs FROM deals "
            "GROUP BY buyer_inn, buyer_name ORDER BY total_uzs DESC LIMIT 5"
        ),
        metric="sum_uzs",
        answer_type="table",
    ),
    _sql_case(
        "ru_top_regions", "ru", "Топ-5 регионов по количеству сделок.",
        [
            Check("SELECT region FROM deals GROUP BY region ORDER BY COUNT(*) DESC LIMIT 1", "name"),
            Check("SELECT region FROM deals GROUP BY region ORDER BY COUNT(*) DESC LIMIT 1 OFFSET 1", "name"),
        ],
        plan_sql="SELECT region, COUNT(*) AS deals_count FROM deals GROUP BY region ORDER BY deals_count DESC LIMIT 5",
        metric="count",
        answer_type="table",
    ),
    _sql_case(
        "uz_top_products", "uz", "Eng ko‘p bitim tuzilgan 5 ta mahsulot.",
        [Check("SELECT product_family FROM deals GROUP BY product_family ORDER BY COUNT(*) DESC LIMIT 1", "name")],
        plan_sql="SELECT product_family, COUNT(*) AS deals_count FROM deals GROUP BY product_family ORDER BY deals_count DESC LIMIT 5",
        metric="count",
        answer_type="table",
    ),
    _sql_case(
        "ru_top_brokers", "ru", "Топ-3 брокера по количеству участий в сделках.",
        [
            Check(
                "SELECT b.name FROM (SELECT seller_broker AS code FROM deals UNION ALL SELECT buyer_broker FROM deals) x "
                "JOIN brokers b ON b.code = x.code GROUP BY b.code, b.name ORDER BY COUNT(*) DESC LIMIT 1",
                "name",
            ),
        ],
        plan_sql=(
            "SELECT b.name, COUNT(*) AS deals_count FROM ("
            "SELECT seller_broker AS code FROM deals UNION ALL SELECT buyer_broker FROM deals"
            ") x JOIN brokers b ON b.code = x.code GROUP BY b.code, b.name ORDER BY deals_count DESC LIMIT 3"
        ),
        metric="count",
        answer_type="table",
    ),
    _sql_case(
        "uz_broker_bk001", "uz", "Broker BK001 qancha bitimda qatnashgan?",
        [
            Check("SELECT COUNT(*) FROM deals WHERE seller_broker = 'BK001'"),
            Check("SELECT COUNT(*) FROM deals WHERE buyer_broker = 'BK001'"),
        ],
        plan_sql=(
            "SELECT SUM(CASE WHEN seller_broker = 'BK001' THEN 1 ELSE 0 END) AS as_seller, "
            "SUM(CASE WHEN buyer_broker = 'BK001' THEN 1 ELSE 0 END) AS as_buyer "
            "FROM deals WHERE seller_broker = 'BK001' OR buyer_broker = 'BK001'"
        ),
        metric="count",
        answer_type="table",
    ),
    _sql_case(
        "ru_export_products", "ru", "Какой товар дал больше всего экспортной выручки в долларах?",
        [Check(
            "SELECT product_family FROM deals WHERE contract_type = 'Экспортный' "
            "GROUP BY product_family ORDER BY SUM(amount_usd) DESC LIMIT 1",
            "name",
        )],
        plan_sql=(
            "SELECT product_family, ROUND(SUM(amount_usd), 2) AS total_usd FROM deals "
            "WHERE contract_type = 'Экспортный' GROUP BY product_family ORDER BY total_usd DESC LIMIT 5"
        ),
        metric="sum_usd",
        answer_type="table",
    ),

    # --- records / lookups --------------------------------------------------
    _sql_case(
        "uz_expensive_2025", "uz", "2025-yildagi eng qimmat bitim qaysi?",
        [
            Check("SELECT deal_no FROM deals WHERE year = 2025 ORDER BY amount_uzs DESC LIMIT 1"),
            Check("SELECT seller_name FROM deals WHERE year = 2025 ORDER BY amount_uzs DESC LIMIT 1", "name"),
        ],
        plan_sql=(
            "SELECT deal_no, deal_date, seller_name, buyer_name, product_family, ROUND(amount_uzs, 2) AS amount_uzs "
            "FROM deals WHERE year = 2025 ORDER BY amount_uzs DESC LIMIT 1"
        ),
        metric="amount",
        answer_type="table",
    ),
    _sql_case(
        "ru_max_usd", "ru", "У какой сделки максимальная сумма в долларах?",
        [Check("SELECT deal_no FROM deals ORDER BY amount_usd DESC LIMIT 1")],
        plan_sql=(
            "SELECT deal_no, deal_date, seller_name, buyer_name, ROUND(amount_usd, 2) AS amount_usd "
            "FROM deals ORDER BY amount_usd DESC LIMIT 1"
        ),
        metric="sum_usd",
        answer_type="table",
    ),
    Case(
        id="uz_deal_504707", lang="uz", question="504707-sonli bitim haqida ma’lumot bering.",
        plan={"intent": "lookup_deal", "deal_no": 504707},
        contains=["504707", "QO‘BU STEEL MCHJ", "78 356 027.52"], source=True,
    ),
    Case(
        id="ru_deal_432930", lang="ru", question="Информация о сделке 432930.",
        plan={"intent": "lookup_deal", "deal_no": 432930},
        contains=["432930", "СОТЎТО МЕТАЛЛ", "PIJIL TRADE MCHJ", "28 243 305 264,00"], source=True,
    ),
    Case(
        id="uz_contract_100001", lang="uz", question="100001-sonli shartnoma shartlari qanday?",
        plan={"intent": "lookup_contract", "contract_no": 100001},
        contains=["100001", "Профиль алюминиевый", "953758401"], source=True,
    ),
    Case(
        id="ru_contract_104846", lang="ru", question="Условия контракта 104846.",
        plan={"intent": "lookup_contract", "contract_no": 104846},
        contains=["104846"], source=True,
    ),
    Case(
        id="uz_company_sito", lang="uz", question="SITO‘TOM TEKSTIL MCHJ haqida ma’lumot bering.",
        plan={"intent": "lookup_company", "company": "SITO‘TOM TEKSTIL MCHJ"},
        contains=["SITO‘TOM TEKSTIL MCHJ", "925910430"], source=True,
    ),
    Case(
        id="ru_company_lookup", lang="ru", question="Карточка компании TUMIZO NEFT MCHJ.",
        plan={"intent": "lookup_company", "company": "TUMIZO NEFT MCHJ"},
        contains=["TUMIZO NEFT MCHJ"], source=True,
    ),
    Case(
        id="uz_broker_lookup", lang="uz", question="BK001 brokeri haqida ma’lumot bering.",
        plan={"intent": "lookup_broker", "broker": "BK001"},
        contains=["BK001"], source=True,
    ),

    # --- USD rates ----------------------------------------------------------
    Case(
        id="uz_rate_2025", lang="uz", question="01.01.2025 dagi dollar kursi qancha?",
        plan={"intent": "usd_rate", "date": "2025-01-01"},
        contains=["01.01.2025", "12 415.45"], source=True,
    ),
    Case(
        id="ru_rate_2026", lang="ru", question="Какой курс доллара был 31.10.2026?",
        plan={"intent": "usd_rate", "date": "2026-10-31"},
        contains=["31.10.2026", "13 357,84"], source=True,
    ),
    Case(
        id="uz_usd_conversion", lang="uz", question="1000 dollar 01.01.2025 da qancha so‘m bo‘ladi?",
        plan={"intent": "usd_rate", "date": "2025-01-01", "amount": 1000, "currency": "USD"},
        contains=["12 415 450.00", "12 415.45"], source=True,
    ),
    Case(
        id="ru_uzs_conversion", lang="ru", question="1 000 000 сум по курсу 01.01.2025 — сколько долларов?",
        plan={"intent": "usd_rate", "date": "2025-01-01", "amount": 1000000, "currency": "UZS"},
        contains=["80,54", "12 415,45"], source=True,
    ),

    # --- metadata -----------------------------------------------------------
    Case(
        id="uz_metadata_period", lang="uz", question="Ma’lumotlar qaysi davrni qamraydi?",
        plan={"intent": "metadata", "topic": "period"},
        contains=["03.01.2022", "30.10.2026", "500 000"], source=False,
    ),
    Case(
        id="ru_metadata_statuses", lang="ru", question="Какие статусы сделок есть в данных?",
        plan={"intent": "metadata", "topic": "statuses"},
        contains=["Исполнен", "Аннулирован", "33 719"], source=False,
    ),
    Case(
        id="ru_metadata_calculation", lang="ru", question="Как считается объём и цена за единицу?",
        plan={"intent": "metadata", "topic": "calculation"},
        contains=["лотов", "объём"], source=False,
    ),
    Case(
        id="uz_metadata_counts", lang="uz", question="Ma’lumotlarda jami qancha bitim, shartnoma, kompaniya va broker bor?",
        plan={"intent": "metadata", "topic": "counts"},
        contains=["500 000", "6 616", "14 392", "120"], source=False,
    ),
    Case(
        id="ru_metadata_files", lang="ru", question="Из каких файлов состоят данные?",
        plan={"intent": "metadata", "topic": "files"},
        contains=["deals_2022.csv", "contracts.csv", "participants.csv", "usd_rates.csv"], source=False,
    ),

    # --- abstain: unsupported / not found / ambiguous -----------------------
    Case(
        id="uz_weather", lang="uz", question="Toshkentda ertaga ob-havo qanday bo‘ladi?",
        plan={"intent": "unsupported", "reason": "ob-havo ma’lumotlarda yo‘q"},
        contains=["doirasidan tashqarida"], expected_kind="abstain", source=False,
    ),
    Case(
        id="ru_forecast", lang="ru", question="Что будет с ценой хлопка в 2027 году?",
        plan={"intent": "unsupported", "reason": "прогноз вне данных"},
        contains=["за рамки"], expected_kind="abstain", source=False,
    ),
    Case(
        id="uz_2021", lang="uz", question="2021-yilda nechta bitim bo‘lgan?",
        plan={"intent": "not_found", "reason": "2021 год вне периода"},
        contains=["topilmadi"], expected_kind="abstain", source=False,
    ),
    Case(
        id="ru_company_missing", lang="ru", question="Сколько сделок у компании ABSOLUTE FICTIONAL COMPANY?",
        plan={"intent": "not_found", "reason": "компания не найдена"},
        contains=["не найден"], expected_kind="abstain", source=False,
    ),
    Case(
        id="ru_ambiguous_product", lang="ru", question="Сколько масла продано в 2025 году?",
        plan={"intent": "ambiguous", "entity_type": "product", "entity_term": "масло"},
        contains=["несколько вариантов", "масло"], expected_kind="ambiguous", source=False,
    ),
    Case(
        id="uz_ambiguous_company", lang="uz", question="Steel kompaniyasi haqida ma’lumot bering.",
        plan={"intent": "ambiguous", "entity_type": "company", "entity_term": "Steel"},
        contains=["bir nechta mos variant"], expected_kind="ambiguous", source=False,
    ),
    Case(
        id="uz_out_of_scope_profit", lang="uz", question="Kompaniyaning foydasi qancha?",
        plan={"intent": "unsupported", "reason": "foyda ma’lumotlari yo‘q"},
        contains=["doirasidan tashqarida"], expected_kind="abstain", source=False,
    ),
    Case(
        id="ru_out_of_scope_phone", lang="ru", question="Какой телефон у компании SITO‘TOM TEKSTIL MCHJ?",
        plan={"intent": "unsupported", "reason": "телефонов в данных нет"},
        # The planner may classify this as unsupported (phone not stored) or
        # not_found (phone field absent); both must abstain without inventing.
        contains=[], expected_kind="abstain", source=False,
    ),
]


def _reference_value(con: sqlite3.Connection, check: Check, lang: str) -> str:
    cur = con.execute(check.sql)
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"reference SQL returned no rows: {check.sql}")
    value = row[check.index]
    if value is None:
        return "—"
    if check.fmt == "int":
        return fmt_number(int(value), lang)
    if check.fmt == "money":
        return fmt_money(float(value), lang)
    if check.fmt == "units":
        return fmt_number(float(value), lang, 2)
    if check.fmt == "date":
        return fmt_date(value, lang)
    return str(value)


def _check_answer(con: sqlite3.Connection, case: Case, answer: Any) -> list[str]:
    problems: list[str] = []
    if answer.kind != case.expected_kind:
        problems.append(f"kind={answer.kind}, expected {case.expected_kind}")
    if answer.language != case.lang:
        problems.append(f"language={answer.language}, expected {case.lang}")
    if case.source and not answer.source:
        problems.append("missing source block")
    for check in case.checks:
        try:
            expected = _reference_value(con, check, case.lang)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"bad reference check: {exc}")
            continue
        if expected not in answer.text:
            problems.append(f"missing {expected!r} ({check.label or check.sql[:60]})")
    for needle in case.contains:
        if needle.lower() not in answer.text.lower():
            problems.append(f"missing text {needle!r}")
    for needle in case.not_contains:
        if needle.lower() in answer.text.lower():
            problems.append(f"unexpected text {needle!r}")
    return problems


class FakeLLM:
    def __init__(self, plans: dict[str, dict[str, Any]]):
        self.plans = plans
        self.model = "fake"

    async def generate_json(self, system: str, prompt: str, **_: Any) -> LLMResult:
        marker = "QUESTION: "
        if marker not in prompt:
            raise RuntimeError("no question in prompt")
        question = prompt.split(marker, 1)[1].split("\n\n", 1)[0].strip()
        plan = self.plans.get(question)
        if plan is None:
            raise RuntimeError(f"no fake plan for: {question!r}")
        return LLMResult(data=json.loads(json.dumps(plan)), model="fake", raw_text=json.dumps(plan))

    async def close(self) -> None:
        return None


async def run(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 2
    cases = CASES
    if args.ids:
        wanted = [x.strip() for x in args.ids.split(",") if x.strip()]
        by_id = {c.id: c for c in CASES}
        missing = [x for x in wanted if x not in by_id]
        if missing:
            print(f"Unknown case ids: {', '.join(missing)}", file=sys.stderr)
            return 2
        cases = [by_id[x] for x in wanted]
    elif args.filter:
        needle = args.filter.lower()
        cases = [c for c in cases if needle in c.id.lower() or needle in c.question.lower()]
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        print("No cases matched.", file=sys.stderr)
        return 2

    if args.offline:
        plans = {c.question: c.plan for c in cases if c.plan is not None}
        llm: Any = FakeLLM(plans)
        model_label = "offline"
    else:
        if not GEMINI_API_KEY:
            print("GEMINI_API_KEY is missing in .env; use --offline or add the key.", file=sys.stderr)
            return 2
        llm = GeminiClient(GEMINI_API_KEY, model=GEMINI_MODEL)
        model_label = GEMINI_MODEL

    agent = Agent(db_path, llm)
    con = sqlite3.connect(db_path)
    failures = 0
    try:
        for i, case in enumerate(cases, 1):
            if args.delay and i > 1:
                await asyncio.sleep(args.delay)
            try:
                answer = await agent.ask(case.question)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"[{i:02d}/{len(cases)}] FAIL {case.id}: crashed: {exc}")
                continue
            problems = _check_answer(con, case, answer)
            if problems:
                failures += 1
                print(f"[{i:02d}/{len(cases)}] FAIL {case.id}: {case.question}")
                for p in problems:
                    print(f"      - {p}")
                if args.verbose:
                    print("      answer:", answer.text.replace("\n", " | ")[:800])
                    print("      sql:", (answer.sql or "")[:500])
            else:
                print(f"[{i:02d}/{len(cases)}] ok   {case.id}  ({answer.elapsed_ms or 0} ms)")
    finally:
        con.close()
        if hasattr(llm, "close"):
            await llm.close()

    total = len(cases)
    print(f"\n{total - failures}/{total} passed ({model_label}).")
    return 0 if failures == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the UZEX agent.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true", help="use scripted plans (no API key)")
    mode.add_argument("--live", action="store_true", help="use Gemini for planning")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--filter", default="", help="substring of case id/question")
    parser.add_argument("--ids", default="", help="comma-separated case ids")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.0, help="seconds between live questions")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
