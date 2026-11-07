"""Build the enriched SQLite database used by the UZEX assistant.

The source data is CSV.  This script is intentionally dependency-free (stdlib
only) so it can run on a small Oracle/DigitalOcean VM without pandas.

Usage:
    python app/build_db.py --data-dir uzex_spot_data --out data/uzex.sqlite
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_APOSTROPHES = str.maketrans({
    "‘": "'", "’": "'", "ʻ": "'", "ʼ": "'", "`": "'", "´": "'",
})


def norm_text(value: str | None) -> str:
    """Lower-case, unify apostrophes and collapse punctuation/whitespace."""
    if not value:
        return ""
    s = value.translate(_APOSTROPHES).lower().replace("ё", "е")
    # Keep unicode letters, digits, apostrophes, dots and hyphens.
    s = re.sub(r"[^\w\s'.-]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def iso_date(value: str | None) -> str | None:
    """Convert DD.MM.YYYY (the source format) to ISO YYYY-MM-DD."""
    if not value or not value.strip():
        return None
    value = value.strip()
    try:
        return datetime.strptime(value, "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def num(value: str | None) -> float | None:
    """Parse '1 234 567.89' or '' into a float."""
    if value is None:
        return None
    value = value.strip().replace(" ", "").replace("\u00a0", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def integer(value: str | None) -> int | None:
    n = num(value)
    return int(n) if n is not None else None


# ---------------------------------------------------------------------------
# Product taxonomy
# ---------------------------------------------------------------------------

# Order is important: specific rules must precede generic ones.
_FAMILY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("хлопок", "сырец"), "Хлопок-сырец"),
    (("хлопок-сырец",), "Хлопок-сырец"),
    (("масло", "хлопков"), "Масло хлопковое"),
    (("хлопков", "масло"), "Масло хлопковое"),
    (("шрот", "хлопков"), "Шрот хлопковый"),
    (("хлопков", "шрот"), "Шрот хлопковый"),
    (("пряжа", "хлоп"), "Пряжа хлопчатобумажная"),
    (("хлопчатобумаж", "пряжа"), "Пряжа хлопчатобумажная"),
    (("мука",), "Мука пшеничная"),
    (("пшеница",), "Пшеница продовольственная"),
    (("портландцемент",), "Портландцемент"),
    (("цемент",), "Портландцемент"),
    (("бензин",), "Бензин автомобильный"),
    (("дизель",), "Дизельное топливо"),
    (("мазут",), "Мазут топочный"),
    (("сжиженн", "газ"), "Сжиженный газ"),
    (("пропан",), "Сжиженный газ"),
    (("карбамид",), "Карбамид"),
    (("мочевина",), "Карбамид"),
    (("селитра",), "Аммиачная селитра"),
    (("катод", "мед"), "Катод медный"),
    (("медный", "катод"), "Катод медный"),
    (("профиль", "алюмин"), "Профиль алюминиевый"),
    (("алюминиев", "профиль"), "Профиль алюминиевый"),
    (("лист", "сталь"), "Лист стальной горячекатаный"),
    (("сталь", "лист"), "Лист стальной горячекатаный"),
    (("проволока",), "Проволока арматурная"),
    (("арматура",), "Арматура стальная"),
    (("полиэтилен",), "Полиэтилен"),
    (("полипропилен",), "Полипропилен"),
    (("поливинилхлорид",), "Поливинилхлорид"),
    (("пвх",), "Поливинилхлорид"),
    (("пиломатериал",), "Пиломатериалы"),
    (("кирпич",), "Кирпич керамический"),
    (("стекло",), "Стекло листовое"),
    (("гипс",), "Гипс строительный"),
    (("известь",), "Известь негашеная"),
    (("серная", "кислота"), "Кислота серная"),
    (("кислота", "серная"), "Кислота серная"),
    (("сода", "каустическ"), "Сода каустическая"),
    (("аргон",), "Аргон газообразный"),
    (("азот",), "Азот"),
    (("вода", "питьев"), "Вода питьевая"),
    (("питьев", "вода"), "Вода питьевая"),
    (("соль",), "Соль пищевая"),
    (("сахар",), "Сахар белый"),
    (("рис",), "Рис шлифованный"),
    (("подсолнечн", "масло"), "Масло подсолнечное"),
    (("масло", "подсолнечн"), "Масло подсолнечное"),
    (("эмаль",), "Эмаль"),
    (("кабель",), "Кабель силовой"),
    (("подшипник",), "Подшипник"),
    (("труб",), "Трубы полиэтиленовые"),
    (("комбикорм",), "Комбикорм"),
    (("кормосмесь",), "Комбикорм"),
    (("битум",), "Битум нефтяной"),
]


def product_family(raw: str | None) -> str:
    s = norm_text(raw)
    for keys, family in _FAMILY_RULES:
        if all(k in s for k in keys):
            return family
    # Fallback: a readable version of the raw product name.
    cleaned = re.sub(r"\s+", " ", (raw or "").strip())
    return cleaned[:120] or "Неизвестно"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CONTRACT_COLUMNS = [
    "contract_no", "product_raw", "product_family", "product_key", "brand", "size",
    "sort", "type", "class", "tnved", "group_name", "category", "industry",
    "unit", "lot", "currency", "base_price", "deposit", "delivery_basis",
    "region", "address", "auction_type", "contract_type", "form",
    "payment_days", "delivery_days", "packaging", "seller_inn",
    "reg_date", "valid_until",
]

DEAL_COLUMNS = [
    "deal_no", "deal_date", "deal_time", "auction_type", "quantity", "amount",
    "start_price", "contract_no", "product_raw", "product_family", "product_key",
    "product_brand", "product_sort", "product_type", "product_class",
    "category", "industry", "group_name", "lot",
    "unit", "region", "contract_region", "contract_type", "form", "status",
    "seller_inn", "buyer_inn", "seller_name", "buyer_name", "seller_name_norm",
    "buyer_name_norm", "seller_region", "buyer_region", "seller_broker",
    "seller_broker_name", "buyer_broker", "buyer_broker_name", "payment_date",
    "delivery_date", "delivered", "currency", "units", "unit_price",
    "amount_uzs", "amount_usd", "usd_rate", "contract_base_price",
    "payment_days", "delivery_days", "is_canceled", "is_completed", "is_paid",
    "is_delivered", "year", "month", "month_key", "quarter",
]

PARTICIPANT_COLUMNS = ["inn", "name", "name_norm", "region", "activity", "role", "vat"]
BROKER_COLUMNS = ["code", "name", "name_norm", "inn", "region"]
RATE_COLUMNS = ["date", "rate"]


def create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        PRAGMA page_size=4096;
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        PRAGMA cache_size=-64000;

        CREATE TABLE contracts (
            contract_no INTEGER PRIMARY KEY,
            product_raw TEXT NOT NULL,
            product_family TEXT NOT NULL,
            product_key TEXT NOT NULL,
            brand TEXT,
            size TEXT,
            sort TEXT,
            type TEXT,
            class TEXT,
            tnved TEXT,
            group_name TEXT,
            category TEXT,
            industry TEXT,
            unit TEXT,
            lot INTEGER,
            currency TEXT,
            base_price REAL,
            deposit TEXT,
            delivery_basis TEXT,
            region TEXT,
            address TEXT,
            auction_type TEXT,
            contract_type TEXT,
            form TEXT,
            payment_days INTEGER,
            delivery_days INTEGER,
            packaging TEXT,
            seller_inn TEXT,
            reg_date TEXT,
            valid_until TEXT
        );

        CREATE TABLE participants (
            inn TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            name_norm TEXT NOT NULL,
            region TEXT,
            activity TEXT,
            role TEXT,
            vat TEXT
        );

        CREATE TABLE brokers (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            name_norm TEXT NOT NULL,
            inn TEXT,
            region TEXT
        );

        CREATE TABLE rates (
            date TEXT PRIMARY KEY,
            rate REAL NOT NULL
        );

        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE deals (
            deal_no INTEGER PRIMARY KEY,
            deal_date TEXT NOT NULL,
            deal_time TEXT,
            auction_type TEXT,
            quantity INTEGER NOT NULL,
            amount REAL NOT NULL,
            start_price REAL,
            contract_no INTEGER NOT NULL,
            product_raw TEXT NOT NULL,
            product_family TEXT NOT NULL,
            product_key TEXT NOT NULL,
            product_brand TEXT,
            product_sort TEXT,
            product_type TEXT,
            product_class TEXT,
            category TEXT,
            industry TEXT,
            group_name TEXT,
            lot INTEGER NOT NULL,
            unit TEXT NOT NULL,
            region TEXT,
            contract_region TEXT,
            contract_type TEXT,
            form TEXT,
            status TEXT,
            seller_inn TEXT,
            buyer_inn TEXT,
            seller_name TEXT,
            buyer_name TEXT,
            seller_name_norm TEXT,
            buyer_name_norm TEXT,
            seller_region TEXT,
            buyer_region TEXT,
            seller_broker TEXT,
            seller_broker_name TEXT,
            buyer_broker TEXT,
            buyer_broker_name TEXT,
            payment_date TEXT,
            delivery_date TEXT,
            delivered INTEGER,
            currency TEXT,
            units REAL NOT NULL,
            unit_price REAL,
            amount_uzs REAL,
            amount_usd REAL,
            usd_rate REAL,
            contract_base_price REAL,
            payment_days INTEGER,
            delivery_days INTEGER,
            is_canceled INTEGER NOT NULL,
            is_completed INTEGER NOT NULL,
            is_paid INTEGER NOT NULL,
            is_delivered INTEGER NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            month_key TEXT NOT NULL,
            quarter INTEGER NOT NULL
        );
        """
    )


def insert_many(con: sqlite3.Connection, table: str, columns: list[str], rows: Iterable[tuple]) -> int:
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
    n = 0
    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= 5000:
            con.executemany(sql, batch)
            n += len(batch)
            batch.clear()
    if batch:
        con.executemany(sql, batch)
        n += len(batch)
    return n


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def read_csv(path: Path) -> Iterable[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


def build(data_dir: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    t0 = time.time()
    con = sqlite3.connect(out_path)
    create_schema(con)

    contracts: dict[int, dict] = {}
    contract_rows = []
    for x in read_csv(data_dir / "contracts.csv"):
        no = int(x["Номер контракта"])
        raw = x["Наименование продукта"].strip()
        fam = product_family(raw)
        row = {
            "contract_no": no,
            "product_raw": raw,
            "product_family": fam,
            "product_key": norm_text(raw),
            "brand": x["Марка товара"].strip(),
            "size": x["Размер товара"].strip(),
            "sort": x["Сорт товара"].strip(),
            "type": x["Тип товара"].strip(),
            "class": x["Класс товара"].strip(),
            "tnved": x["Код ТН ВЭД"].strip(),
            "group_name": x["Группа товара"].strip(),
            "category": x["Категория товара"].strip(),
            "industry": x["Отрасль промышленности"].strip(),
            "unit": x["Единица измерения"].strip(),
            "lot": int(x["Лот"]),
            "currency": x["Валюта"].strip(),
            "base_price": num(x["Базисная цена"]),
            "deposit": x["Сумма залога"].strip(),
            "delivery_basis": x["Базис поставки"].strip(),
            "region": x["Регион"].strip(),
            "address": x["Адрес склада"].strip(),
            "auction_type": x["Тип торгов"].strip(),
            "contract_type": x["Тип контракта"].strip(),
            "form": x["Форма контракта"].strip(),
            "payment_days": integer(x["Срок оплаты"]),
            "delivery_days": integer(x["Срок поставки"]),
            "packaging": x["Упаковка"].strip(),
            "seller_inn": x["ИНН продавца"].strip(),
            "reg_date": iso_date(x["Дата регистрации"]),
            "valid_until": iso_date(x["Действует до"]),
        }
        contracts[no] = row
        contract_rows.append(tuple(row[c] for c in CONTRACT_COLUMNS))
    insert_many(con, "contracts", CONTRACT_COLUMNS, contract_rows)

    participants: dict[str, dict] = {}
    participant_rows = []
    for x in read_csv(data_dir / "participants.csv"):
        d = {
            "inn": x["ИНН"].strip(),
            "name": x["Наименование"].strip(),
            "name_norm": norm_text(x["Наименование"]),
            "region": x["Регион"].strip(),
            "activity": x["Вид деятельности"].strip(),
            "role": x["Роль"].strip(),
            "vat": x["Плательщик НДС"].strip(),
        }
        participants[d["inn"]] = d
        participant_rows.append(tuple(d[c] for c in PARTICIPANT_COLUMNS))
    insert_many(con, "participants", PARTICIPANT_COLUMNS, participant_rows)

    brokers: dict[str, dict] = {}
    broker_rows = []
    for x in read_csv(data_dir / "brokers.csv"):
        d = {
            "code": x["Код брокера"].strip(),
            "name": x["Наименование"].strip(),
            "name_norm": norm_text(x["Наименование"]),
            "inn": x["ИНН"].strip(),
            "region": x["Регион"].strip(),
        }
        brokers[d["code"]] = d
        broker_rows.append(tuple(d[c] for c in BROKER_COLUMNS))
    insert_many(con, "brokers", BROKER_COLUMNS, broker_rows)

    rates: dict[str, float] = {}
    rate_rows = []
    for x in read_csv(data_dir / "usd_rates.csv"):
        d = iso_date(x["Дата"])
        if d:
            rates[d] = num(x["Курс"]) or 0.0
            rate_rows.append((d, rates[d]))
    insert_many(con, "rates", RATE_COLUMNS, rate_rows)

    total = 0
    yearly: dict[int, int] = {}
    for year in range(2022, 2027):
        path = data_dir / f"deals_{year}.csv"
        rows = []
        for x in read_csv(path):
            c = contracts[int(x["Номер контракта"])]
            d = iso_date(x["Дата сделки"])
            if not d:
                continue
            dt = datetime.strptime(d, "%Y-%m-%d")
            rate = rates.get(d)
            qty = int(x["Количество"])
            lot = int(x["Лот"])
            amt = num(x["Сумма сделки"])
            if amt is None:
                continue
            units = qty * lot
            currency = c["currency"]
            if currency == "Доллар США":
                amount_uzs = amt * rate if rate else None
                amount_usd = amt
            else:
                amount_uzs = amt
                amount_usd = amt / rate if rate else None
            status = x["Состояние сделки"].strip()
            seller = participants.get(x["ИНН продавца"].strip(), {})
            buyer = participants.get(x["ИНН покупателя"].strip(), {})
            delivered = integer(x["Поставлено"])
            rows.append((
                int(x["Номер сделки"]),
                d,
                x["Время сделки"].strip(),
                x["Тип торгов"].strip(),
                qty,
                amt,
                num(x["Стартовая цена"]),
                int(x["Номер контракта"]),
                x["Наименование товара"].strip(),
                c["product_family"],
                c["product_key"],
                c["brand"],
                c["sort"],
                c["type"],
                c["class"],
                c["category"],
                c["industry"],
                c["group_name"],
                lot,
                x["Единица измерения"].strip(),
                x["Регион"].strip(),
                c["region"],
                x["Тип контракта"].strip(),
                x["Форма контракта"].strip(),
                status,
                x["ИНН продавца"].strip(),
                x["ИНН покупателя"].strip(),
                seller.get("name"),
                buyer.get("name"),
                seller.get("name_norm"),
                buyer.get("name_norm"),
                seller.get("region"),
                buyer.get("region"),
                x["Брокер продавца"].strip(),
                brokers.get(x["Брокер продавца"].strip(), {}).get("name"),
                x["Брокер покупателя"].strip(),
                brokers.get(x["Брокер покупателя"].strip(), {}).get("name"),
                iso_date(x["Дата оплаты"]),
                iso_date(x["Дата поставки"]),
                delivered,
                currency,
                units,
                amt / units if units else None,
                amount_uzs,
                amount_usd,
                rate,
                c["base_price"],
                c["payment_days"],
                c["delivery_days"],
                1 if status == "Аннулирован(Не оплачен)" else 0,
                1 if status in ("Исполнен", "Исполнен(Частично)") else 0,
                1 if x["Дата оплаты"].strip() or status in ("Оплачен", "Исполнен", "Исполнен(Частично)") else 0,
                1 if (delivered or 0) > 0 else 0,
                dt.year,
                dt.month,
                dt.strftime("%Y-%m"),
                (dt.month - 1) // 3 + 1,
            ))
        n = insert_many(con, "deals", DEAL_COLUMNS, rows)
        total += n
        yearly[year] = n
        print(f"  {year}: {n:,} deals  ({time.time() - t0:.1f}s)", flush=True)

    con.commit()
    print("Creating indexes...", flush=True)
    con.executescript(
        """
        CREATE INDEX ix_deals_date ON deals(deal_date);
        CREATE INDEX ix_deals_year ON deals(year);
        CREATE INDEX ix_deals_month ON deals(month_key);
        CREATE INDEX ix_deals_contract ON deals(contract_no);
        CREATE INDEX ix_deals_product ON deals(product_family);
        CREATE INDEX ix_deals_region ON deals(region);
        CREATE INDEX ix_deals_seller ON deals(seller_inn);
        CREATE INDEX ix_deals_buyer ON deals(buyer_inn);
        CREATE INDEX ix_deals_seller_broker ON deals(seller_broker);
        CREATE INDEX ix_deals_buyer_broker ON deals(buyer_broker);
        CREATE INDEX ix_deals_amount_uzs ON deals(amount_uzs);
        CREATE INDEX ix_deals_amount_usd ON deals(amount_usd);
        CREATE INDEX ix_deals_units ON deals(units);
        CREATE INDEX ix_contracts_product ON contracts(product_family);
        CREATE INDEX ix_contracts_seller ON contracts(seller_inn);
        CREATE INDEX ix_contracts_region ON contracts(region);
        CREATE INDEX ix_participants_name ON participants(name_norm);
        CREATE INDEX ix_participants_role ON participants(role);
        CREATE INDEX ix_brokers_name ON brokers(name_norm);
        """
    )
    con.commit()

    # Metadata is intentionally computed from the actual data (not hard-coded).
    min_date, max_date = con.execute("SELECT MIN(deal_date), MAX(deal_date) FROM deals").fetchone()
    meta = {
        "total_deals": total,
        "min_date": min_date,
        "max_date": max_date,
        "contracts": len(contracts),
        "participants": len(participants),
        "brokers": len(brokers),
        "rates": len(rates),
        "rate_min_date": con.execute("SELECT MIN(date) FROM rates").fetchone()[0],
        "rate_max_date": con.execute("SELECT MAX(date) FROM rates").fetchone()[0],
        "yearly": yearly,
        "statuses": dict(con.execute("SELECT status, COUNT(*) FROM deals GROUP BY status").fetchall()),
        "contract_types": dict(con.execute("SELECT contract_type, COUNT(*) FROM deals GROUP BY contract_type").fetchall()),
        "forms": dict(con.execute("SELECT form, COUNT(*) FROM deals GROUP BY form").fetchall()),
        "auction_types": dict(con.execute("SELECT auction_type, COUNT(*) FROM deals GROUP BY auction_type").fetchall()),
        "units": dict(con.execute("SELECT unit, COUNT(*) FROM deals GROUP BY unit ORDER BY COUNT(*) DESC").fetchall()),
        "regions": dict(con.execute("SELECT region, COUNT(*) FROM deals GROUP BY region ORDER BY COUNT(*) DESC").fetchall()),
        "product_families": dict(con.execute("SELECT product_family, COUNT(*) FROM deals GROUP BY product_family ORDER BY COUNT(*) DESC").fetchall()),
        "currencies": dict(con.execute("SELECT currency, COUNT(*) FROM deals GROUP BY currency").fetchall()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    con.executemany(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()],
    )
    con.commit()

    # Report unmapped product families (fallback names are still usable).
    unmapped = sorted({c["product_raw"] for c in contracts.values() if c["product_family"] == c["product_raw"]})
    if unmapped:
        print(f"WARNING: {len(unmapped)} product names used fallback family", flush=True)
        for name in unmapped[:20]:
            print("  -", name, flush=True)

    con.execute("ANALYZE")
    con.commit()
    con.close()

    size_mb = out_path.stat().st_size / 1e6
    print(f"Done: {out_path}  ({size_mb:.1f} MB, {total:,} deals, {time.time() - t0:.1f}s)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="uzex_spot_data")
    parser.add_argument("--out", default="data/uzex.sqlite")
    args = parser.parse_args()
    build(Path(args.data_dir), Path(args.out))


if __name__ == "__main__":
    main()
