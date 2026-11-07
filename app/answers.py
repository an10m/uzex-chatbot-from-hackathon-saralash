"""Deterministic, grounded answer rendering.

The LLM writes SQL and chooses an intent, but the final numbers and names are
rendered from the actual query result.  This is the main anti-hallucination
layer.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .sql_guard import QueryResult

MAX_ANSWER_CHARS = 20_000

UZ_MONTHS = {
    1: "yanvar", 2: "fevral", 3: "mart", 4: "aprel", 5: "may", 6: "iyun",
    7: "iyul", 8: "avgust", 9: "sentabr", 10: "oktabr", 11: "noyabr", 12: "dekabr",
}
RU_MONTHS = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
    7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}

LABELS: dict[str, dict[str, str]] = {
    "deals_count": {"uz": "Bitimlar soni", "ru": "Количество сделок"},
    "count": {"uz": "Soni", "ru": "Количество"},
    "cnt": {"uz": "Soni", "ru": "Количество"},
    "n": {"uz": "Soni", "ru": "Количество"},
    "total_uzs": {"uz": "Summa (so‘m)", "ru": "Сумма (сум)"},
    "sum_uzs": {"uz": "Summa (so‘m)", "ru": "Сумма (сум)"},
    "amount_uzs": {"uz": "Summa (so‘m)", "ru": "Сумма (сум)"},
    "total_usd": {"uz": "Summa (AQSh dollari)", "ru": "Сумма (долл. США)"},
    "sum_usd": {"uz": "Summa (AQSh dollari)", "ru": "Сумма (долл. США)"},
    "amount_usd": {"uz": "Summa (AQSh dollari)", "ru": "Сумма (долл. США)"},
    "units": {"uz": "Hajm (birlik)", "ru": "Объём (ед.)"},
    "total_units": {"uz": "Hajm (birlik)", "ru": "Объём (ед.)"},
    "avg_unit_price": {"uz": "O‘rtacha narx", "ru": "Средняя цена"},
    "avg_price": {"uz": "O‘rtacha narx", "ru": "Средняя цена"},
    "avg_amount": {"uz": "O‘rtacha bitim summasi", "ru": "Средняя сумма сделки"},
    "unit_price": {"uz": "Narx (birlik)", "ru": "Цена (ед.)"},
    "seller_name": {"uz": "Sotuvchi", "ru": "Продавец"},
    "buyer_name": {"uz": "Xaridor", "ru": "Покупатель"},
    "company": {"uz": "Kompaniya", "ru": "Компания"},
    "product_family": {"uz": "Mahsulot", "ru": "Товар"},
    "product_raw": {"uz": "Mahsulot", "ru": "Товар"},
    "product_sort": {"uz": "Sort", "ru": "Сорт"},
    "product_class": {"uz": "Klass", "ru": "Класс"},
    "region": {"uz": "Hudud", "ru": "Регион"},
    "status": {"uz": "Holat", "ru": "Статус"},
    "year": {"uz": "Yil", "ru": "Год"},
    "month_key": {"uz": "Oy", "ru": "Месяц"},
    "quarter": {"uz": "Chorak", "ru": "Квартал"},
    "deal_no": {"uz": "Bitim raqami", "ru": "Номер сделки"},
    "contract_no": {"uz": "Shartnoma raqami", "ru": "Номер контракта"},
    "deal_date": {"uz": "Sana", "ru": "Дата"},
    "deal_time": {"uz": "Vaqt", "ru": "Время"},
    "seller_inn": {"uz": "Sotuvchi STIR", "ru": "ИНН продавца"},
    "buyer_inn": {"uz": "Xaridor STIR", "ru": "ИНН покупателя"},
    "seller_broker": {"uz": "Sotuvchi brokeri", "ru": "Брокер продавца"},
    "buyer_broker": {"uz": "Xaridor brokeri", "ru": "Брокер покупателя"},
    "broker_name": {"uz": "Broker", "ru": "Брокер"},
    "as_seller": {"uz": "Sotuvchi sifatida", "ru": "Как продавец"},
    "as_buyer": {"uz": "Xaridor sifatida", "ru": "Как покупатель"},
    "role": {"uz": "Rol", "ru": "Роль"},
    "currency": {"uz": "Valyuta", "ru": "Валюта"},
    "unit": {"uz": "Birlik", "ru": "Ед. изм."},
    "amount": {"uz": "Summa", "ru": "Сумма"},
    "delivered": {"uz": "Yetkazilgan (lot)", "ru": "Поставлено (лот)"},
}


@dataclass
class Answer:
    text: str
    language: str
    kind: str = "answer"  # answer | abstain | ambiguous | error
    source: str | None = None
    sql: str | None = None
    row_count: int | None = None
    elapsed_ms: int | None = None
    plan: dict[str, Any] = field(default_factory=dict)


def fmt_number(value: Any, lang: str = "uz", decimals: int | None = None) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return ("Ha" if value else "Yo‘q") if lang == "uz" else ("Да" if value else "Нет")
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    if isinstance(value, float):
        if not math.isfinite(value):  # NaN / ±Infinity
            return "—"
        if decimals is None:
            decimals = 0 if abs(value - round(value)) < 1e-9 else 2
        s = f"{value:,.{decimals}f}"
        if lang == "ru":
            return s.replace(",", " ").replace(".", ",")
        return s.replace(",", " ")
    return str(value)


def fmt_money(value: Any, lang: str = "uz") -> str:
    return fmt_number(value, lang, 2)


def fmt_date(value: Any, lang: str = "uz") -> str:
    if not value:
        return "—"
    s = str(value)
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
        return d.strftime("%d.%m.%Y")
    except ValueError:
        return html.escape(s, quote=False)


def fmt_datetime(date_value: Any, time_value: Any) -> str:
    d = fmt_date(date_value)
    if not time_value:
        return d
    return f"{d} {html.escape(str(time_value), quote=False)}"


def _html_value(value: Any, dash: str = "—") -> str:
    """Escape a dynamic text value before it is embedded in Telegram HTML."""
    if value is None or value == "":
        return dash
    return html.escape(str(value), quote=False)


def label(col: str, lang: str) -> str:
    key = col.lower()
    if key in LABELS:
        return LABELS[key][lang]
    # Column aliases come from LLM-generated SQL, so treat the fallback as text.
    return html.escape(col.replace("_", " "), quote=False)


def _period_from_sql(sql: str | None, lang: str) -> str:
    if not sql:
        return "Butun davr" if lang == "uz" else "весь период"
    s = sql

    m = re.search(r"month_key\s*=\s*'(\d{4})-(\d{2})'", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        name = (UZ_MONTHS if lang == "uz" else RU_MONTHS).get(mo, str(mo))
        return f"{y}-yil {name}" if lang == "uz" else f"{name} {y} года"

    m = re.search(r"quarter\s*=\s*(\d).{0,80}?year\s*=\s*(\d{4})", s, re.IGNORECASE)
    if not m:
        m = re.search(r"year\s*=\s*(\d{4}).{0,80}?quarter\s*=\s*(\d)", s, re.IGNORECASE)
        if m:
            y, q = m.group(1), m.group(2)
        else:
            y = q = None
    else:
        q, y = m.group(1), m.group(2)
    if y:
        return f"{y}-yil {q}-chorak" if lang == "uz" else f"{q}-й квартал {y} года"

    m = re.search(r"year\s*=\s*(\d{4})", s)
    if m:
        y = m.group(1)
        return f"{y}-yil" if lang == "uz" else f"{y} год"

    m = re.search(r"year\s+IN\s*\(([^)]+)\)", s, re.IGNORECASE)
    if m:
        years = re.findall(r"\d{4}", m.group(1))
        if len(years) >= 2:
            return f"{min(years)}–{max(years)}"
        if years:
            return f"{years[0]}-yil" if lang == "uz" else f"{years[0]} год"

    m = re.search(r"year\s*>=\s*(\d{4})", s)
    if m:
        return f"{m.group(1)}-yildan" if lang == "uz" else f"с {m.group(1)} года"

    dates = re.findall(r"'(\d{4}-\d{2}-\d{2})'", s)
    if len(dates) >= 2:
        return f"{fmt_date(min(dates), lang)}–{fmt_date(max(dates), lang)}"
    if len(dates) == 1 and "rates" in s.lower():
        return fmt_date(dates[0], lang)

    if re.search(r"month_key\s*>=", s):
        return "2026-yil oktabr" if lang == "uz" else "октябрь 2026"

    return "Butun davr" if lang == "uz" else "весь период"


def _scope_note(sql: str | None, plan: dict, lang: str) -> str:
    """Describe the status scope from the SQL itself, not from the LLM's claim.

    This keeps the source note grounded when the model sets includes_canceled
    incorrectly while writing SQL that still includes every status.
    """
    s = (sql or "").lower()
    if "is_canceled = 1" in s or "is_canceled=1" in s:
        return "Faqat bekor qilingan bitimlar." if lang == "uz" else "Только аннулированные сделки."
    if "is_completed = 1" in s or "is_completed=1" in s:
        return "Faqat bajarilgan bitimlar." if lang == "uz" else "Только исполненные сделки."
    if "is_canceled = 0" in s or "is_canceled=0" in s:
        return "Bekor qilingan bitimlar hisobga olinmagan." if lang == "uz" else "Аннулированные сделки не учтены."
    if re.search(r"\bstatus\s*(=|IN)\b", s, re.IGNORECASE):
        return "Holat bo‘yicha filtrlangan." if lang == "uz" else "Отфильтровано по статусу."
    return "Barcha holatlar, shu jumladan bekor qilinganlar." if lang == "uz" else "Все статусы, включая аннулированные."


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _render_value(col: str, value: Any, lang: str, metric: str | None = None) -> str:
    c = col.lower()
    if _is_number(value):
        if any(x in c for x in ("amount", "sum", "total", "price", "uzs", "usd")):
            return fmt_money(value, lang)
        if c in ("units", "total_units", "volume"):
            return fmt_number(value, lang, 2)
        if c in ("deals_count", "count", "cnt", "n", "as_seller", "as_buyer"):
            return fmt_number(int(value), lang)
        if "pct" in c or "percent" in c or "share" in c:
            return f"{fmt_number(value, lang, 1)}%"
        return fmt_number(value, lang)
    if c in ("deal_date", "payment_date", "delivery_date", "reg_date", "valid_until"):
        return fmt_date(value, lang)
    if c == "role":
        if value == "seller":
            return "sotuvchi" if lang == "uz" else "продавец"
        if value == "buyer":
            return "xaridor" if lang == "uz" else "покупатель"
    if c == "currency":
        if value == "Узбекский сум":
            return "so‘m" if lang == "uz" else "сум"
        if value == "Доллар США":
            return "AQSh dollari" if lang == "uz" else "долл. США"
    return _html_value(value)


def _render_rows_table(columns: list[str], rows: list[tuple], lang: str, metric: str | None) -> str:
    if not rows:
        return ""
    if len(columns) == 1:
        return "\n".join(f"• {_render_value(columns[0], r[0], lang, metric)}" for r in rows)
    # Pick the first text-like column as the label; fall back to the first column.
    label_idx = 0
    for i, c in enumerate(columns):
        if any(isinstance(r[i], str) for r in rows):
            label_idx = i
            break
    value_indices = [i for i in range(len(columns)) if i != label_idx]
    lines = []
    for n, row in enumerate(rows, 1):
        lbl = _render_value(columns[label_idx], row[label_idx], lang, metric)
        vals = []
        for i in value_indices:
            v = _render_value(columns[i], row[i], lang, metric)
            vals.append(f"{label(columns[i], lang)}: {v}" if len(value_indices) > 1 else v)
        lines.append(f"{n}. {lbl} — " + "; ".join(vals))
    return "\n".join(lines)


def _scalar_sentence(value: Any, plan: dict, lang: str, period: str, sql: str | None) -> str:
    metric = plan.get("metric", "other")
    v = _render_value("", value, lang, metric) if not _is_number(value) else None
    if _is_number(value):
        if metric == "count" or any(x in (sql or "").lower() for x in ("count(", " as deals_count", " as count")):
            v = fmt_number(int(value), lang)
            return (
                f"{period} bo‘yicha {v} ta bitim qayd etilgan."
                if lang == "uz" else f"За {period} зарегистрировано {v} сделок."
            )
        if metric == "sum_usd":
            v = fmt_money(value, lang)
            return (
                f"{period} bo‘yicha umumiy summa {v} AQSh dollari."
                if lang == "uz" else f"Общая сумма за {period} — {v} долл. США."
            )
        if metric == "units":
            v = fmt_number(value, lang, 2)
            return (
                f"{period} bo‘yicha umumiy hajm {v} birlik."
                if lang == "uz" else f"Общий объём за {period} — {v} ед."
            )
        if metric == "avg_price":
            v = fmt_money(value, lang)
            return (
                f"{period} bo‘yicha o‘rtacha narx {v}."
                if lang == "uz" else f"Средняя цена за {period} — {v}."
            )
        if metric == "avg_amount":
            v = fmt_money(value, lang)
            return (
                f"{period} bo‘yicha o‘rtacha bitim summasi {v} so‘m."
                if lang == "uz" else f"Средняя сумма сделки за {period} — {v} сум."
            )
        if metric in ("sum_uzs", "amount"):
            v = fmt_money(value, lang)
            return (
                f"{period} bo‘yicha umumiy summa {v} so‘m."
                if lang == "uz" else f"Общая сумма за {period} — {v} сум."
            )
        v = fmt_number(value, lang, 2)
        return f"Natija: {v}." if lang == "uz" else f"Результат: {v}."
    return f"Natija: {v}." if lang == "uz" else f"Результат: {v}."


def render_query_answer(
    question: str,
    plan: dict,
    result: QueryResult,
    lang: str,
    meta: dict[str, Any],
) -> Answer:
    if result.row_count == 0:
        return render_not_found(lang, "so‘rov bo‘yicha yozuv topilmadi", meta)
    period = _period_from_sql(plan.get("sql"), lang)
    scope = _scope_note(plan.get("sql"), plan, lang)
    metric = plan.get("metric", "other")
    columns = result.columns
    rows = result.rows

    # Single scalar
    if len(columns) == 1 and len(rows) == 1 and _is_number(rows[0][0]):
        text = _scalar_sentence(rows[0][0], plan, lang, period, plan.get("sql"))
    elif len(columns) == 1 and len(rows) > 1:
        text = _render_rows_table(columns, rows, lang, metric)
    elif len(rows) == 1 and len(columns) > 1:
        # A single record-like row: render field/value pairs.
        parts = []
        for c, v in zip(columns, rows[0]):
            parts.append(f"{label(c, lang)}: {_render_value(c, v, lang, metric)}")
        text = "\n".join(parts)
    else:
        text = _render_rows_table(columns, rows, lang, metric)

    notes = []
    if scope:
        notes.append(scope)
    if result.truncated:
        notes.append(
            f"Natijalar {len(rows)} qatordan ko‘rsatilgan."
            if lang == "uz" else f"Показаны первые {len(rows)} строк."
        )
    if notes:
        text += "\n\n" + " ".join(notes)
    if len(text) > MAX_ANSWER_CHARS:
        text = text[:MAX_ANSWER_CHARS].rstrip() + "\n…"
    source = source_html(plan.get("sql"), result, lang, period, scope, meta)
    return Answer(text=text, language=lang, source=source, sql=plan.get("sql"), row_count=result.row_count, plan=plan)


def source_html(
    sql: str | None,
    result: QueryResult | None,
    lang: str,
    period: str,
    scope: str,
    meta: dict[str, Any],
) -> str:
    title = "Manba" if lang == "uz" else "Источник"
    rows_label = "qator" if lang == "uz" else "строк"
    scope_label = "doira" if lang == "uz" else "охват"
    period_label = "davr" if lang == "uz" else "период"
    data_label = "Ma’lumotlar" if lang == "uz" else "Данные"
    parts = [f"<b>{title}</b>"]
    if sql:
        sql_short = sql.strip()
        if len(sql_short) > 1600:
            sql_short = sql_short[:1600] + " …"
        parts.append(f"<pre><code>{html.escape(sql_short)}</code></pre>")
    if result is not None:
        parts.append(f"<b>{rows_label}:</b> {result.row_count}")
    parts.append(f"<b>{period_label}:</b> {html.escape(period)}")
    parts.append(f"<b>{scope_label}:</b> {html.escape(scope)}")
    parts.append(
        f"<b>{data_label}:</b> {_html_value(meta.get('min_date', '2022-01-03'))} – "
        f"{_html_value(meta.get('max_date', '2026-10-30'))}"
    )
    return "<blockquote expandable>" + "\n".join(parts) + "</blockquote>"


# ---------------------------------------------------------------------------
# Deterministic lookup renderers
# ---------------------------------------------------------------------------

def render_deal(row: dict, lang: str, meta: dict[str, Any]) -> Answer:
    lines = [
        f"<b>Bitim №{_html_value(row['deal_no'])}</b>" if lang == "uz" else f"<b>Сделка №{_html_value(row['deal_no'])}</b>",
        f"{'Sana' if lang == 'uz' else 'Дата'}: {fmt_datetime(row.get('deal_date'), row.get('deal_time'))}",
        f"{'Mahsulot' if lang == 'uz' else 'Товар'}: {_html_value(row.get('product_raw') or row.get('product_family'))}",
        f"{'Sotuvchi' if lang == 'uz' else 'Продавец'}: {_html_value(row.get('seller_name'))} (STIR {_html_value(row.get('seller_inn'))})",
        f"{'Xaridor' if lang == 'uz' else 'Покупатель'}: {_html_value(row.get('buyer_name'))} (STIR {_html_value(row.get('buyer_inn'))})",
        f"{'Miqdor' if lang == 'uz' else 'Количество'}: {fmt_number(row.get('quantity'), lang)} lot × {fmt_number(row.get('lot'), lang)} = {fmt_number(row.get('units'), lang)} {_html_value(row.get('unit'), '')}",
        f"{'Summa' if lang == 'uz' else 'Сумма'}: {fmt_money(row.get('amount'), lang)} {_render_value('currency', row.get('currency'), lang)}",
        f"{'Holat' if lang == 'uz' else 'Статус'}: {_html_value(row.get('status'))}",
        f"{'Hudud' if lang == 'uz' else 'Регион'}: {_html_value(row.get('region'))}",
        f"{'Shartnoma' if lang == 'uz' else 'Контракт'}: {_html_value(row.get('contract_no'))}",
        f"{'Brokerlar' if lang == 'uz' else 'Брокеры'}: {'sotuvchi' if lang == 'uz' else 'продавца'} {_html_value(row.get('seller_broker'))} {_html_value(row.get('seller_broker_name'), '')}; {'xaridor' if lang == 'uz' else 'покупателя'} {_html_value(row.get('buyer_broker'))} {_html_value(row.get('buyer_broker_name'), '')}",
        f"{'To‘lov' if lang == 'uz' else 'Оплата'}: {fmt_date(row.get('payment_date'), lang)}; {'yetkazish' if lang == 'uz' else 'поставка'}: {fmt_date(row.get('delivery_date'), lang)}; {'yetkazilgan' if lang == 'uz' else 'поставлено'}: {fmt_number(row.get('delivered'), lang)} lot",
    ]
    text = "\n".join(lines)
    sql = f"SELECT * FROM deals WHERE deal_no = {int(row['deal_no'])}"
    return Answer(text=text, language=lang, source=source_html(sql, None, lang, fmt_date(row.get('deal_date')), "bitta yozuv" if lang == "uz" else "одна запись", meta), sql=sql, row_count=1)


def render_contract(row: dict, lang: str, meta: dict[str, Any]) -> Answer:
    lines = [
        f"<b>Shartnoma №{_html_value(row['contract_no'])}</b>" if lang == "uz" else f"<b>Контракт №{_html_value(row['contract_no'])}</b>",
        f"{'Mahsulot' if lang == 'uz' else 'Товар'}: {_html_value(row.get('product_raw'))}",
        f"{'Sotuvchi STIR' if lang == 'uz' else 'ИНН продавца'}: {_html_value(row.get('seller_inn'))}",
        f"{'Birlik / lot' if lang == 'uz' else 'Единица / лот'}: {_html_value(row.get('unit'))} / {fmt_number(row.get('lot'), lang)}",
        f"{'Valyuta' if lang == 'uz' else 'Валюта'}: {_render_value('currency', row.get('currency'), lang)}",
        f"{'Bazis narx' if lang == 'uz' else 'Базовая цена'}: {fmt_money(row.get('base_price'), lang)}",
        f"{'Zaklat' if lang == 'uz' else 'Залог'}: {_html_value(row.get('deposit'))}",
        f"{'To‘lov muddati' if lang == 'uz' else 'Срок оплаты'}: {_html_value(row.get('payment_days'))} {'kun' if lang == 'uz' else 'дн.'}; {'yetkazish' if lang == 'uz' else 'поставка'}: {_html_value(row.get('delivery_days'))} {'kun' if lang == 'uz' else 'дн.'}",
        f"{'Hudud' if lang == 'uz' else 'Регион'}: {_html_value(row.get('region'))}",
        f"{'Manzil' if lang == 'uz' else 'Адрес'}: {_html_value(row.get('address'))}",
        f"{'Tur / forma' if lang == 'uz' else 'Тип / форма'}: {_html_value(row.get('contract_type'))} / {_html_value(row.get('form'))}",
        f"{'Amal qilish' if lang == 'uz' else 'Действует'}: {fmt_date(row.get('reg_date'), lang)} – {fmt_date(row.get('valid_until'), lang)}",
    ]
    text = "\n".join(lines)
    sql = f"SELECT * FROM contracts WHERE contract_no = {int(row['contract_no'])}"
    return Answer(text=text, language=lang, source=source_html(sql, None, lang, fmt_date(row.get('reg_date')), "bitta yozuv" if lang == "uz" else "одна запись", meta), sql=sql, row_count=1)


def render_company(company: dict, stats: dict, lang: str, meta: dict[str, Any]) -> Answer:
    lines = [
        f"<b>{_html_value(company['name'])}</b>",
        f"STIR: {_html_value(company['inn'])}",
        f"{'Hudud' if lang == 'uz' else 'Регион'}: {_html_value(company.get('region'))}",
        f"{'Faoliyat' if lang == 'uz' else 'Деятельность'}: {_html_value(company.get('activity'))}",
        f"{'Rol' if lang == 'uz' else 'Роль'}: {_html_value(company.get('role'))}",
        f"{'QQS to‘lovchi' if lang == 'uz' else 'Плательщик НДС'}: {_html_value(company.get('vat'))}",
        f"{'Sotuvchi sifatida' if lang == 'uz' else 'Как продавец'}: {fmt_number(stats.get('seller_deals'), lang)} {'bitim' if lang == 'uz' else 'сделок'}, {fmt_money(stats.get('seller_total_uzs'), lang)} {'so‘m' if lang == 'uz' else 'сум'}",
        f"{'Xaridor sifatida' if lang == 'uz' else 'Как покупатель'}: {fmt_number(stats.get('buyer_deals'), lang)} {'bitim' if lang == 'uz' else 'сделок'}, {fmt_money(stats.get('buyer_total_uzs'), lang)} {'so‘m' if lang == 'uz' else 'сум'}",
        "Ko‘rsatkichlar barcha holatlar bo‘yicha, bekor qilinganlar ham kiradi."
        if lang == "uz" else "Показатели по всем статусам, включая аннулированные.",
    ]
    text = "\n".join(lines)
    inn = company["inn"]
    sql = (
        f"SELECT (SELECT COUNT(*) FROM deals WHERE seller_inn='{inn}') AS seller_deals, "
        f"(SELECT SUM(amount_uzs) FROM deals WHERE seller_inn='{inn}') AS seller_total_uzs, "
        f"(SELECT COUNT(*) FROM deals WHERE buyer_inn='{inn}') AS buyer_deals, "
        f"(SELECT SUM(amount_uzs) FROM deals WHERE buyer_inn='{inn}') AS buyer_total_uzs"
    )
    return Answer(text=text, language=lang, source=source_html(sql, None, lang, "butun davr" if lang == "uz" else "весь период", "kompaniya kartasi" if lang == "uz" else "карточка компании", meta), sql=sql, row_count=1)


def render_broker(broker: dict, stats: dict, lang: str, meta: dict[str, Any]) -> Answer:
    lines = [
        f"<b>{_html_value(broker['name'])}</b>",
        f"{'Kod' if lang == 'uz' else 'Код'}: {_html_value(broker['code'])}",
        f"STIR: {_html_value(broker.get('inn'))}",
        f"{'Hudud' if lang == 'uz' else 'Регион'}: {_html_value(broker.get('region'))}",
        f"{'Sotuvchi brokeri sifatida' if lang == 'uz' else 'Как брокер продавца'}: {fmt_number(stats.get('seller_deals'), lang)} {'bitim' if lang == 'uz' else 'сделок'}",
        f"{'Xaridor brokeri sifatida' if lang == 'uz' else 'Как брокер покупателя'}: {fmt_number(stats.get('buyer_deals'), lang)} {'bitim' if lang == 'uz' else 'сделок'}",
        "Ko‘rsatkichlar barcha holatlar bo‘yicha, bekor qilinganlar ham kiradi."
        if lang == "uz" else "Показатели по всем статусам, включая аннулированные.",
    ]
    text = "\n".join(lines)
    code = broker["code"]
    sql = (
        f"SELECT (SELECT COUNT(*) FROM deals WHERE seller_broker='{code}') AS seller_deals, "
        f"(SELECT COUNT(*) FROM deals WHERE buyer_broker='{code}') AS buyer_deals"
    )
    return Answer(text=text, language=lang, source=source_html(sql, None, lang, "butun davr" if lang == "uz" else "весь период", "broker kartasi" if lang == "uz" else "карточка брокера", meta), sql=sql, row_count=1)


def render_rate(date: str, rate: float, lang: str, meta: dict[str, Any], amount: float | None = None, currency: str | None = None) -> Answer:
    d = fmt_date(date, lang)
    if amount is None:
        text = (
            f"{d} holatiga ko‘ra 1 AQSh dollari = {fmt_money(rate, lang)} so‘m."
            if lang == "uz" else f"На {d} 1 долл. США = {fmt_money(rate, lang)} сум."
        )
    elif (currency or "USD").upper() == "USD":
        total = amount * rate
        text = (
            f"{d} holatiga ko‘ra {fmt_money(amount, lang)} AQSh dollari = {fmt_money(total, lang)} so‘m (1 USD = {fmt_money(rate, lang)} so‘m)."
            if lang == "uz" else f"На {d} {fmt_money(amount, lang)} долл. США = {fmt_money(total, lang)} сум (1 USD = {fmt_money(rate, lang)} сум)."
        )
    else:
        total = amount / rate if rate else None
        text = (
            f"{d} holatiga ko‘ra {fmt_money(amount, lang)} so‘m = {fmt_money(total, lang)} AQSh dollari (1 USD = {fmt_money(rate, lang)} so‘m)."
            if lang == "uz" else f"На {d} {fmt_money(amount, lang)} сум = {fmt_money(total, lang)} долл. США (1 USD = {fmt_money(rate, lang)} сум)."
        )
    sql = f"SELECT date, rate FROM rates WHERE date = '{date}'"
    return Answer(text=text, language=lang, source=source_html(sql, None, lang, d, "kurs" if lang == "uz" else "курс", meta), sql=sql, row_count=1)


def render_metadata(topic: str, meta: dict[str, Any], catalog: Any, lang: str) -> Answer:
    total = meta.get("total_deals", 500000)
    min_date = fmt_date(meta.get("min_date", "2022-01-03"), lang)
    max_date = fmt_date(meta.get("max_date", "2026-10-30"), lang)
    yearly = meta.get("yearly", {})
    yearly_text = ", ".join(f"{_html_value(k)}: {fmt_number(v, lang)}" for k, v in yearly.items())
    statuses = meta.get("statuses", {})
    status_text = "\n".join(f"• {_html_value(k)} — {fmt_number(v, lang)}" for k, v in statuses.items())
    if topic == "files":
        text = (
            "Ma’lumotlar quyidagi fayllardan iborat: deals_2022.csv … deals_2026.csv (bitimlar), "
            "contracts.csv (shartnoma kartochkalari), participants.csv (kompaniyalar), "
            "brokers.csv (brokerlar), usd_rates.csv (dollar kursi)."
            if lang == "uz" else
            "Данные состоят из файлов deals_2022.csv … deals_2026.csv (сделки), "
            "contracts.csv (карточки контрактов), participants.csv (компании), "
            "brokers.csv (брокеры), usd_rates.csv (курс доллара)."
        )
    elif topic == "fields":
        text = (
            "Bitimlar jadvalida sana, vaqt, savdo turi, lot soni, summa, shartnoma, mahsulot, "
            "hudud, holat, sotuvchi/xaridor STIR, brokerlar, to‘lov/yetkazish sanalari, hajm va "
            "summalar (so‘m/dollar) mavjud. Shartnomalar, kompaniyalar, brokerlar va dollar kursi "
            "alohida jadvallarda."
            if lang == "uz" else
            "В таблице сделок есть дата, время, тип торгов, количество лотов, сумма, контракт, товар, "
            "регион, статус, ИНН продавца/покупателя, брокеры, даты оплаты/поставки, объём и суммы "
            "(сум/доллар). Контракты, компании, брокеры и курс доллара — в отдельных таблицах."
        )
    elif topic == "statuses":
        text = (
            "Bitim holatlari:\n" + status_text
            if lang == "uz" else "Статусы сделок:\n" + status_text
        )
    elif topic == "calculation":
        text = (
            "Hajm = lot soni × lot. Birlik narxi = summa ÷ hajm. Eksport bitimlari dollarda; "
            "ularning so‘mdagi summasi bitim sanasidagi kurs bo‘yicha hisoblanadi."
            if lang == "uz" else
            "Объём = количество лотов × лот. Цена за единицу = сумма ÷ объём. Экспортные сделки — "
            "в долларах; сумма в сумах пересчитывается по курсу на дату сделки."
        )
    elif topic == "currency":
        text = (
            "Ichki bitimlar so‘mda, eksport bitimlari dollarda. Jami summalar so‘mda "
            "(amount_uzs) yoki dollarda (amount_usd) ko‘rsatilishi mumkin."
            if lang == "uz" else
            "Внутренние сделки — в сумах, экспортные — в долларах. Итоговые суммы можно "
            "показывать в сумах (amount_uzs) или долларах (amount_usd)."
        )
    elif topic == "counts":
        text = (
            f"Jami {fmt_number(total, lang)} bitim, {fmt_number(meta.get('contracts', 6616), lang)} shartnoma, "
            f"{fmt_number(meta.get('participants', 14392), lang)} kompaniya, {fmt_number(meta.get('brokers', 120), lang)} broker. "
            f"Yillar bo‘yicha: {yearly_text}."
            if lang == "uz" else
            f"Всего {fmt_number(total, lang)} сделок, {fmt_number(meta.get('contracts', 6616), lang)} контрактов, "
            f"{fmt_number(meta.get('participants', 14392), lang)} компаний, {fmt_number(meta.get('brokers', 120), lang)} брокеров. "
            f"По годам: {yearly_text}."
        )
    elif topic == "limitations":
        text = (
            f"Ma’lumotlar {min_date} – {max_date} davrini qamraydi. Barcha kompaniyalar, STIR, "
            "brokerlar va summalar o‘ylab topilgan. Prognoz, siyosat, ob-havo yoki real bozor "
            "ma’lumotlari yo‘q."
            if lang == "uz" else
            f"Данные охватывают период {min_date} – {max_date}. Все компании, ИНН, брокеры и суммы "
            "вымышлены. Прогнозов, политики, погоды и реальных рыночных данных нет."
        )
    else:  # period
        text = (
            f"Ma’lumotlar {min_date} – {max_date} davrini qamraydi. Jami {fmt_number(total, lang)} bitim. "
            f"Yillar bo‘yicha: {yearly_text}."
            if lang == "uz" else
            f"Данные охватывают период {min_date} – {max_date}. Всего {fmt_number(total, lang)} сделок. "
            f"По годам: {yearly_text}."
        )
    return Answer(text=text, language=lang, source=None, row_count=None, plan={"intent": "metadata", "topic": topic})


def render_not_found(lang: str, reason: str, meta: dict[str, Any] | None = None) -> Answer:
    min_date = fmt_date((meta or {}).get("min_date", "2022-01-03"), lang)
    max_date = fmt_date((meta or {}).get("max_date", "2026-10-30"), lang)
    if lang == "uz":
        text = (
            "Bu savolga javob ma’lumotlarda topilmadi."
            + (f" Sabab: {_html_value(reason)}." if reason else "")
            + f" Ma’lumotlar {min_date} – {max_date} davrini qamraydi."
        )
    else:
        text = (
            "Ответ на этот вопрос в данных не найден."
            + (f" Причина: {_html_value(reason)}." if reason else "")
            + f" Данные охватывают период {min_date} – {max_date}."
        )
    return Answer(text=text, language=lang, kind="abstain", plan={"intent": "not_found", "reason": reason})


def render_unsupported(lang: str, reason: str, meta: dict[str, Any] | None = None) -> Answer:
    min_date = fmt_date((meta or {}).get("min_date", "2022-01-03"), lang)
    max_date = fmt_date((meta or {}).get("max_date", "2026-10-30"), lang)
    if lang == "uz":
        text = (
            "Bu savol taqdim etilgan ma’lumotlar doirasidan tashqarida."
            + (f" Sabab: {_html_value(reason)}." if reason else "")
            + f" Ma’lumotlarda faqat {min_date} – {max_date} davridagi birja bitimlari, shartnomalar, "
            "ishtirokchilar, brokerlar va dollar kursi mavjud."
        )
    else:
        text = (
            "Этот вопрос выходит за рамки предоставленных данных."
            + (f" Причина: {_html_value(reason)}." if reason else "")
            + f" В данных есть только сделки, контракты, участники, брокеры и курс доллара за период "
            f"{min_date} – {max_date}."
        )
    return Answer(text=text, language=lang, kind="abstain", plan={"intent": "unsupported", "reason": reason})


def render_ambiguous(lang: str, entity_type: str, term: str, candidates: list[str], meta: dict[str, Any] | None = None) -> Answer:
    safe_term = _html_value(term)
    if lang == "uz":
        text = f"«{safe_term}» bo‘yicha bir nechta mos variant bor. Iltimos, aniqlashtiring:"
    else:
        text = f"По запросу «{safe_term}» есть несколько вариантов. Уточните, пожалуйста:"
    if candidates:
        text += "\n" + "\n".join(f"• {_html_value(c)}" for c in candidates[:8])
    return Answer(text=text, language=lang, kind="ambiguous", plan={"intent": "ambiguous", "entity_type": entity_type, "entity_term": term})


def render_need_date(lang: str) -> Answer:
    text = (
        "Iltimos, sanani DD.MM.YYYY formatida yozing (masalan, 01.01.2025)."
        if lang == "uz" else
        "Укажите дату в формате ДД.ММ.ГГГГ (например, 01.01.2025)."
    )
    return Answer(text=text, language=lang, kind="ambiguous", plan={"intent": "ambiguous", "entity_type": "date"})


def render_error(lang: str) -> Answer:
    text = (
        "Texnik xatolik yuz berdi. Iltimos, savolni qayta yuboring."
        if lang == "uz" else
        "Произошла техническая ошибка. Пожалуйста, отправьте вопрос ещё раз."
    )
    return Answer(text=text, language=lang, kind="error")
