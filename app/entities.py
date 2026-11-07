"""Entity normalization and resolution for UZEX questions.

The LLM is asked to use canonical names, but we never trust that blindly: every
product, region, status, company and broker is resolved against the actual
database before SQL is built.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from .build_db import norm_text

# ---------------------------------------------------------------------------
# Product aliases: Uzbek (Latin/Cyrillic), Russian and English -> canonical family
# ---------------------------------------------------------------------------

PRODUCT_ALIASES: dict[str, str] = {
    # raw cotton
    "paxta": "Хлопок-сырец",
    "paxta tolasi": "Хлопок-сырец",
    "paxta xom ashyosi": "Хлопок-сырец",
    "хлопок": "Хлопок-сырец",
    "хлопок сырец": "Хлопок-сырец",
    "хлопок-сырец": "Хлопок-сырец",
    "пахта": "Хлопок-сырец",
    "пахта толаси": "Хлопок-сырец",
    "cotton": "Хлопок-сырец",
    "raw cotton": "Хлопок-сырец",
    # cotton oil
    "paxta yog'i": "Масло хлопковое",
    "paxta yogi": "Масло хлопковое",
    "хлопковое масло": "Масло хлопковое",
    "масло хлопковое": "Масло хлопковое",
    "пахта ёғи": "Масло хлопковое",
    "cottonseed oil": "Масло хлопковое",
    # cotton meal
    "kunjar": "Шрот хлопковый",
    "шрот": "Шрот хлопковый",
    "шрот хлопковый": "Шрот хлопковый",
    "кунжара": "Шрот хлопковый",
    "cottonseed meal": "Шрот хлопковый",
    # cotton yarn
    "paxta ip": "Пряжа хлопчатобумажная",
    "paxta ipi": "Пряжа хлопчатобумажная",
    "пряжа": "Пряжа хлопчатобумажная",
    "хлопчатобумажная пряжа": "Пряжа хлопчатобумажная",
    "ип": "Пряжа хлопчатобумажная",
    "cotton yarn": "Пряжа хлопчатобумажная",
    # wheat / flour
    "bug'doy": "Пшеница продовольственная",
    "bugdoy": "Пшеница продовольственная",
    "пшеница": "Пшеница продовольственная",
    "буғдой": "Пшеница продовольственная",
    "wheat": "Пшеница продовольственная",
    "un": "Мука пшеничная",
    "мука": "Мука пшеничная",
    "мука пшеничная": "Мука пшеничная",
    "ун": "Мука пшеничная",
    "flour": "Мука пшеничная",
    # cement
    "tsement": "Портландцемент",
    "цемент": "Портландцемент",
    "портландцемент": "Портландцемент",
    "cement": "Портландцемент",
    # fuel
    "benzin": "Бензин автомобильный",
    "бензин": "Бензин автомобильный",
    "бензин автомобильный": "Бензин автомобильный",
    "gasoline": "Бензин автомобильный",
    "petrol": "Бензин автомобильный",
    "dizel": "Дизельное топливо",
    "дизель": "Дизельное топливо",
    "дизельное топливо": "Дизельное топливо",
    "солярка": "Дизельное топливо",
    "diesel": "Дизельное топливо",
    "mazut": "Мазут топочный",
    "мазут": "Мазут топочный",
    "fuel oil": "Мазут топочный",
    "gaz": "Сжиженный газ",
    "газ": "Сжиженный газ",
    "сжиженный газ": "Сжиженный газ",
    "пропан": "Сжиженный газ",
    "propan": "Сжиженный газ",
    "lpg": "Сжиженный газ",
    # fertilizers
    "karbamid": "Карбамид",
    "карбамид": "Карбамид",
    "мочевина": "Карбамид",
    "urea": "Карбамид",
    "selitra": "Аммиачная селитра",
    "селитра": "Аммиачная селитра",
    "аммиачная селитра": "Аммиачная селитра",
    "ammonium nitrate": "Аммиачная селитра",
    # metals
    "mis": "Катод медный",
    "медь": "Катод медный",
    "медный катод": "Катод медный",
    "катод медный": "Катод медный",
    "copper": "Катод медный",
    "alyuminiy": "Профиль алюминиевый",
    "алюминий": "Профиль алюминиевый",
    "алюминиевый профиль": "Профиль алюминиевый",
    "aluminium": "Профиль алюминиевый",
    "aluminum": "Профиль алюминиевый",
    "po'lat": "Лист стальной горячекатаный",
    "сталь": "Лист стальной горячекатаный",
    "стальной лист": "Лист стальной горячекатаный",
    "лист стальной": "Лист стальной горячекатаный",
    "steel sheet": "Лист стальной горячекатаный",
    "armatura": "Арматура стальная",
    "арматура": "Арматура стальная",
    "rebar": "Арматура стальная",
    "sim": "Проволока арматурная",
    "проволока": "Проволока арматурная",
    "wire": "Проволока арматурная",
    # polymers
    "polietilen": "Полиэтилен",
    "полиэтилен": "Полиэтилен",
    "polyethylene": "Полиэтилен",
    "polipropilen": "Полипропилен",
    "полипропилен": "Полипропилен",
    "polypropylene": "Полипропилен",
    "pvh": "Поливинилхлорид",
    "пвх": "Поливинилхлорид",
    "поливинилхлорид": "Поливинилхлорид",
    "pvc": "Поливинилхлорид",
    # construction
    "yog'och": "Пиломатериалы",
    "пиломатериал": "Пиломатериалы",
    "пиломатериалы": "Пиломатериалы",
    "лес": "Пиломатериалы",
    "lumber": "Пиломатериалы",
    "g'isht": "Кирпич керамический",
    "кирпич": "Кирпич керамический",
    "brick": "Кирпич керамический",
    "shisha": "Стекло листовое",
    "стекло": "Стекло листовое",
    "glass": "Стекло листовое",
    "gips": "Гипс строительный",
    "гипс": "Гипс строительный",
    "gypsum": "Гипс строительный",
    "ohak": "Известь негашеная",
    "известь": "Известь негашеная",
    "lime": "Известь негашеная",
    # chemicals
    "sulfat kislota": "Кислота серная",
    "серная кислота": "Кислота серная",
    "sulfuric acid": "Кислота серная",
    "soda": "Сода каустическая",
    "сода": "Сода каустическая",
    "caustic soda": "Сода каустическая",
    # other
    "argon": "Аргон газообразный",
    "аргон": "Аргон газообразный",
    "azot": "Азот",
    "азот": "Азот",
    "nitrogen": "Азот",
    "suv": "Вода питьевая",
    "вода": "Вода питьевая",
    "water": "Вода питьевая",
    "tuz": "Соль пищевая",
    "соль": "Соль пищевая",
    "salt": "Соль пищевая",
    "shakar": "Сахар белый",
    "сахар": "Сахар белый",
    "sugar": "Сахар белый",
    "guruch": "Рис шлифованный",
    "рис": "Рис шлифованный",
    "rice": "Рис шлифованный",
    "kungaboqar yog'i": "Масло подсолнечное",
    "подсолнечное масло": "Масло подсолнечное",
    "sunflower oil": "Масло подсолнечное",
    "emal": "Эмаль",
    "эмаль": "Эмаль",
    "enamel": "Эмаль",
    "kabel": "Кабель силовой",
    "кабель": "Кабель силовой",
    "cable": "Кабель силовой",
    "podshipnik": "Подшипник",
    "подшипник": "Подшипник",
    "bearing": "Подшипник",
    "truba": "Трубы полиэтиленовые",
    "труба": "Трубы полиэтиленовые",
    "трубы": "Трубы полиэтиленовые",
    "pipes": "Трубы полиэтиленовые",
    "комбикорм": "Комбикорм",
    "кормосмесь": "Комбикорм",
    "feed": "Комбикорм",
    "bitum": "Битум нефтяной",
    "битум": "Битум нефтяной",
    "bitumen": "Битум нефтяной",
}

REGION_ALIASES: dict[str, str] = {
    "andijon": "Andijon viloyati",
    "андижан": "Andijon viloyati",
    "andijan": "Andijon viloyati",
    "buxoro": "Buxoro viloyati",
    "бухара": "Buxoro viloyati",
    "bukhara": "Buxoro viloyati",
    "farg'ona": "Farg‘ona viloyati",
    "fargona": "Farg‘ona viloyati",
    "фергана": "Farg‘ona viloyati",
    "fergana": "Farg‘ona viloyati",
    "jizzax": "Jizzax viloyati",
    "джизак": "Jizzax viloyati",
    "jizzakh": "Jizzax viloyati",
    "namangan": "Namangan viloyati",
    "наманган": "Namangan viloyati",
    "navoiy": "Navoiy viloyati",
    "навои": "Navoiy viloyati",
    "navoi": "Navoiy viloyati",
    "qashqadaryo": "Qashqadaryo viloyati",
    "кашкадарья": "Qashqadaryo viloyati",
    "kashkadarya": "Qashqadaryo viloyati",
    "qoraqalpog'iston": "Qoraqalpog‘iston Respublikasi",
    "каракалпакстан": "Qoraqalpog‘iston Respublikasi",
    "karakalpakstan": "Qoraqalpog‘iston Respublikasi",
    "samarqand": "Samarqand viloyati",
    "самарканд": "Samarqand viloyati",
    "samarkand": "Samarqand viloyati",
    "sirdaryo": "Sirdaryo viloyati",
    "сырдарья": "Sirdaryo viloyati",
    "syrdarya": "Sirdaryo viloyati",
    "surxondaryo": "Surxondaryo viloyati",
    "сурхандарья": "Surxondaryo viloyati",
    "surkhandarya": "Surxondaryo viloyati",
    "toshkent shahri": "Toshkent shahri",
    "ташкент город": "Toshkent shahri",
    "tashkent city": "Toshkent shahri",
    "toshkent viloyati": "Toshkent viloyati",
    "ташкентская область": "Toshkent viloyati",
    "tashkent region": "Toshkent viloyati",
    "xorazm": "Xorazm viloyati",
    "хорезм": "Xorazm viloyati",
    "khorezm": "Xorazm viloyati",
}

STATUS_ALIASES: dict[str, list[str]] = {
    "новый": ["Новый"],
    "yangi": ["Новый"],
    "new": ["Новый"],
    "не подписан": ["Не подписан"],
    "imzolanmagan": ["Не подписан"],
    "unsigned": ["Не подписан"],
    "заключен": ["Заключен"],
    "заключён": ["Заключен"],
    "tuzilgan": ["Заключен"],
    "signed": ["Заключен"],
    "ждет оплату": ["Ждёт оплату"],
    "ждёт оплату": ["Ждёт оплату"],
    "to'lov kutilmoqda": ["Ждёт оплату"],
    "waiting for payment": ["Ждёт оплату"],
    "оплачен": ["Оплачен"],
    "to'langan": ["Оплачен"],
    "paid": ["Оплачен"],
    "исполнен": ["Исполнен"],
    "bajarilgan": ["Исполнен"],
    "completed": ["Исполнен", "Исполнен(Частично)"],
    "executed": ["Исполнен", "Исполнен(Частично)"],
    "исполнен частично": ["Исполнен(Частично)"],
    "qisman bajarilgan": ["Исполнен(Частично)"],
    "partially executed": ["Исполнен(Частично)"],
    "аннулирован": ["Аннулирован(Не оплачен)"],
    "аннулирован не оплачен": ["Аннулирован(Не оплачен)"],
    "bekor qilingan": ["Аннулирован(Не оплачен)"],
    "cancelled": ["Аннулирован(Не оплачен)"],
    "canceled": ["Аннулирован(Не оплачен)"],
    "не оплачен": ["Ждёт оплату", "Аннулирован(Не оплачен)"],
    "to'lanmagan": ["Ждёт оплату", "Аннулирован(Не оплачен)"],
    "unpaid": ["Ждёт оплату", "Аннулирован(Не оплачен)"],
}

TYPE_ALIASES: dict[str, str] = {
    "внутренний": "Внутренний",
    "ichki": "Внутренний",
    "domestic": "Внутренний",
    "экспортный": "Экспортный",
    "eksport": "Экспортный",
    "export": "Экспортный",
}

FORM_ALIASES: dict[str, str] = {
    "спот": "Спот",
    "spot": "Спот",
    "форвард": "Форвард",
    "forward": "Форвард",
    "фьючерс": "Фьючерс",
    "futures": "Фьючерс",
}

UNIT_ALIASES: dict[str, str] = {
    "tonna": "тонна",
    "тонна": "тонна",
    "ton": "тонна",
    "kilogramm": "килограмм",
    "килограмм": "килограмм",
    "kg": "килограмм",
    "кг": "килограмм",
    "litr": "литр",
    "литр": "литр",
    "liter": "литр",
    "dona": "штука",
    "штука": "штука",
    "piece": "штука",
    "kub metr": "куб.метр",
    "кубометр": "куб.метр",
    "куб. метр": "куб.метр",
    "m3": "куб.метр",
    "kvadrat metr": "кв.метр",
    "кв. метр": "кв.метр",
    "m2": "кв.метр",
    "metr": "метр",
    "метр": "метр",
    "meter": "метр",
}

AUCTION_ALIASES: dict[str, str] = {
    "английский": "Английский аукцион",
    "английский аукцион": "Английский аукцион",
    "ingliz": "Английский аукцион",
    "english": "Английский аукцион",
    "обратный": "Обратный аукцион",
    "обратный аукцион": "Обратный аукцион",
    "teskari": "Обратный аукцион",
    "reverse": "Обратный аукцион",
    "двусторонний": "Двусторонний аукцион",
    "двусторонний аукцион": "Двусторонний аукцион",
    "ikki tomonlama": "Двусторонний аукцион",
    "two-sided": "Двусторонний аукцион",
}


def _normalize_query(value: str) -> str:
    return norm_text(value)


def _fuzzy_score(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 100.0
    if query in candidate or candidate in query:
        return 92.0
    q_tokens = set(query.split())
    c_tokens = set(candidate.split())
    if q_tokens and q_tokens <= c_tokens:
        return 86.0
    intersection = q_tokens & c_tokens
    if intersection:
        jaccard = len(intersection) / len(q_tokens | c_tokens)
        return 55.0 + 35.0 * jaccard
    ratio = SequenceMatcher(None, query, candidate).ratio()
    return 60.0 * ratio


def _match_aliases(term: str, aliases: dict[str, str]) -> list[str]:
    """Longest-alias-first matching to avoid 'масло' beating 'хлопковое масло'."""
    q = _normalize_query(term)
    if not q:
        return []
    hits: list[tuple[int, str]] = []
    for alias, canonical in aliases.items():
        a = _normalize_query(alias)
        if not a:
            continue
        if q == a or (len(a) >= 4 and re.search(rf"(?<!\w){re.escape(a)}(?!\w)", q)):
            hits.append((len(a), canonical))
    if not hits:
        return []
    max_len = max(n for n, _ in hits)
    return sorted({c for n, c in hits if n == max_len})


def _fuzzy_all(term: str, canonical_values: Iterable[str]) -> list[tuple[float, str]]:
    q = _normalize_query(term)
    scored = [(round(_fuzzy_score(q, _normalize_query(c)), 1), c) for c in canonical_values]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored


@dataclass(frozen=True)
class Resolution:
    values: list[str]
    ambiguous: bool
    score: float
    candidates: list[tuple[float, str]]


def resolve_product(term: str, families: Iterable[str]) -> Resolution:
    families = list(families)
    alias_hits = _match_aliases(term, PRODUCT_ALIASES)
    if alias_hits:
        # Keep only aliases that exist in the actual DB.
        hits = [x for x in alias_hits if x in families]
        if hits:
            return Resolution(hits, len(hits) > 1, 100.0, [(100.0, x) for x in hits])
    scored = _fuzzy_all(term, families)
    if not scored:
        return Resolution([], False, 0.0, [])
    best_score, best = scored[0]
    if best_score >= 88:
        close = [x for x in scored if x[0] >= best_score - 5]
        return Resolution([best], len(close) > 1, best_score, scored[:5])
    if best_score >= 70:
        close = [x for x in scored if x[0] >= best_score - 3]
        return Resolution([x[1] for x in close], len(close) > 1, best_score, scored[:5])
    return Resolution([], False, best_score, scored[:5])


def resolve_region(term: str, regions: Iterable[str]) -> Resolution:
    regions = list(regions)
    q = _normalize_query(term)
    if q in ("toshkent", "ташкент", "tashkent"):
        both = [r for r in regions if r in ("Toshkent shahri", "Toshkent viloyati")]
        return Resolution(both, True, 90.0, [(90.0, r) for r in both])
    alias_hits = _match_aliases(term, REGION_ALIASES)
    if alias_hits:
        hits = [x for x in alias_hits if x in regions]
        if hits:
            return Resolution(hits, len(hits) > 1, 100.0, [(100.0, x) for x in hits])
    scored = _fuzzy_all(term, regions)
    if not scored:
        return Resolution([], False, 0.0, [])
    best_score, best = scored[0]
    if best_score >= 86:
        close = [x for x in scored if x[0] >= best_score - 4]
        return Resolution([best], len(close) > 1, best_score, scored[:5])
    if best_score >= 70:
        close = [x for x in scored if x[0] >= best_score - 2]
        return Resolution([x[1] for x in close], len(close) > 1, best_score, scored[:5])
    return Resolution([], False, best_score, scored[:5])


def resolve_status(term: str, statuses: Iterable[str]) -> Resolution:
    statuses = list(statuses)
    q = _normalize_query(term)
    for alias, values in STATUS_ALIASES.items():
        if q == _normalize_query(alias):
            hits = [v for v in values if v in statuses]
            if hits:
                return Resolution(hits, len(hits) > 1, 100.0, [(100.0, x) for x in hits])
    scored = _fuzzy_all(term, statuses)
    if not scored:
        return Resolution([], False, 0.0, [])
    best_score, best = scored[0]
    if best_score >= 86:
        close = [x for x in scored if x[0] >= best_score - 4]
        return Resolution([best], len(close) > 1, best_score, scored[:5])
    if best_score >= 72:
        close = [x for x in scored if x[0] >= best_score - 2]
        return Resolution([x[1] for x in close], len(close) > 1, best_score, scored[:5])
    return Resolution([], False, best_score, scored[:5])


def resolve_simple(term: str, values: Iterable[str], aliases: dict[str, str]) -> Resolution:
    values = list(values)
    alias_hits = _match_aliases(term, aliases)
    if alias_hits:
        hits = [x for x in alias_hits if x in values]
        if hits:
            return Resolution(hits, len(hits) > 1, 100.0, [(100.0, x) for x in hits])
    scored = _fuzzy_all(term, values)
    if not scored:
        return Resolution([], False, 0.0, [])
    best_score, best = scored[0]
    if best_score >= 86:
        close = [x for x in scored if x[0] >= best_score - 4]
        return Resolution([best], len(close) > 1, best_score, scored[:5])
    return Resolution([], False, best_score, scored[:5])


# ---------------------------------------------------------------------------
# Company / broker resolution
# ---------------------------------------------------------------------------

_LEGAL_FORMS = {
    "mchj", "мчж", "ooo", "ооо", "ok", "оо", "aj", "ао", "qk", "чк",
    "universal", "unit", "корхона", "kompaniya", "компания", "firma", "фирма",
    "enterprise", "предприятие", "jamiyati", "жамияти", "masuliyati",
    "cheklangan", "chegaralangan", "limited", "ltd", "inc", "llc", "jsc",
}


def _name_tokens(value: str) -> set[str]:
    q = _normalize_query(value)
    return {t for t in q.split() if t and t not in _LEGAL_FORMS and len(t) > 1}


def _company_score(query: str, name_norm: str) -> float:
    q = _normalize_query(query)
    if not q:
        return 0.0
    if q == name_norm:
        return 100.0
    if q in name_norm:
        return 95.0
    if name_norm in q and len(name_norm) >= 6:
        return 90.0
    q_tokens = _name_tokens(q)
    n_tokens = _name_tokens(name_norm)
    if q_tokens and q_tokens <= n_tokens:
        return 87.0
    if q_tokens and n_tokens:
        jaccard = len(q_tokens & n_tokens) / len(q_tokens | n_tokens)
        if jaccard >= 0.5:
            return 55.0 + 35.0 * jaccard
    ratio = SequenceMatcher(None, q, name_norm).ratio()
    return 60.0 * ratio


@dataclass(frozen=True)
class NameCandidate:
    key: str
    name: str
    score: float
    inn: str | None = None
    region: str | None = None


def resolve_company(term: str, companies: list[dict]) -> Resolution:
    """companies: list of dicts with inn, name, name_norm, region."""
    q = _normalize_query(term)
    if not q:
        return Resolution([], False, 0.0, [])
    # Exact INN is unambiguous.
    if re.fullmatch(r"\d{9}", q):
        hits = [c for c in companies if c["inn"] == q]
        if hits:
            c = hits[0]
            return Resolution([c["inn"]], False, 100.0, [(100.0, c["inn"])])
    scored: list[tuple[float, dict]] = []
    for c in companies:
        score = _company_score(q, c["name_norm"])
        if score >= 55:
            scored.append((round(score, 1), c))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    if not scored:
        return Resolution([], False, 0.0, [])
    best_score = scored[0][0]
    close = [x for x in scored if x[0] >= best_score - 3]
    candidates = [(s, c["name"] + f" (STIR {c['inn']})") for s, c in scored[:5]]
    if best_score >= 87 and (len(close) == 1 or best_score - close[1][0] >= 8):
        return Resolution([scored[0][1]["inn"]], False, best_score, candidates)
    # Multiple plausible matches: return them as candidates, never pick one.
    return Resolution([], True, best_score, candidates)


def resolve_broker(term: str, brokers: list[dict]) -> Resolution:
    q = _normalize_query(term)
    if not q:
        return Resolution([], False, 0.0, [])
    if re.fullmatch(r"bk\d{3}", q):
        hits = [b for b in brokers if b["code"].lower() == q]
        if hits:
            return Resolution([hits[0]["code"]], False, 100.0, [(100.0, hits[0]["name"])])
    scored: list[tuple[float, dict]] = []
    for b in brokers:
        score = max(_company_score(q, b["name_norm"]), 100.0 if q == b["code"].lower() else 0.0)
        if score >= 55:
            scored.append((round(score, 1), b))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    if not scored:
        return Resolution([], False, 0.0, [])
    best_score = scored[0][0]
    close = [x for x in scored if x[0] >= best_score - 3]
    candidates = [(s, b["name"] + f" ({b['code']})") for s, b in scored[:5]]
    if best_score >= 87 and (len(close) == 1 or best_score - close[1][0] >= 8):
        return Resolution([scored[0][1]["code"]], False, best_score, candidates)
    return Resolution([], True, best_score, candidates)


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_UZ_WORDS = {
    "qancha", "nechta", "bitim", "bitimlar", "bo'yicha", "boyicha", "uchun",
    "haqida", "qaysi", "eng", "bilan", "yil", "yilda", "oy", "oyda", "so'm",
    "som", "paxta", "bug'doy", "bugdoy", "mahsulot", "kompaniya", "sotuvchi",
    "xaridor", "broker", "viloyat", "shahar", "davlat", "summa", "narx",
    "holat", "shartnoma", "sana", "kurs", "dollar", "eksport", "ichki",
    "forvard", "fyanch", "auksion", "savdo", "hajm", "tonna", "kilogramm",
    "litr", "jami", "o'rtacha", "ortacha", "qilib", "bering", "ko'rsating",
    "korsating", "top", "reyting", "hisobot", "ma'lumot", "malumot",
}
# Words that are distinctly Uzbek Cyrillic.  Shared Russian/Uzbek words
# (сумма, компания, брокер, курс, доллар, экспорт, аукцион, ...) must NOT be
# here, otherwise ordinary Russian questions are misclassified as Uzbek.
_UZ_CYR_WORDS = {
    "қанча", "нечта", "битим", "битимлар", "бўйича", "учун", "ҳақида",
    "қайси", "энг", "билан", "йил", "йилда", "йили", "сўм", "пахта",
    "буғдой", "маҳсулот", "сотувчи", "харидор", "вилоят", "шаҳар",
    "шаҳрида", "шаҳридаги", "ҳолат", "шартнома", "сана", "ички", "савдо",
    "ҳажм", "жами", "ўртача", "кўрсатинг", "маълумот", "тузилган",
    "қилинган", "бўлган", "қабул", "ҳисоб", "керак", "қачон", "қаерда",
    "ким", "нима", "нега", "қандай", "ҳамма", "фақат", "орасида",
}


def detect_language(text: str) -> str:
    """Return 'ru' or 'uz'. Uzbek Cyrillic is treated as Uzbek.

    Uzbek Cyrillic has distinctive letters (ў, қ, ғ, ҳ) and function words.
    Ordinary Cyrillic without those markers defaults to Russian, because the
    two languages share many trade words (сумма, компания, брокер, курс).
    """
    if not text or not text.strip():
        return "uz"
    lowered = text.lower()
    cyr = len(re.findall(r"[а-яёўқғҳ]", lowered))
    lat = len(re.findall(r"[a-z]", lowered))
    # Uzbek Latin with a quoted Russian status is still an Uzbek question.
    if lat > 0 and any(w in lowered for w in _UZ_WORDS) and lat >= cyr:
        return "uz"
    if cyr >= 5 or (cyr > 0 and cyr >= lat):
        if re.search(r"[ўқғҳ]", lowered):
            return "uz"
        if any(w in lowered for w in _UZ_CYR_WORDS):
            return "uz"
        return "ru"
    if any(w in lowered for w in _UZ_WORDS):
        return "uz"
    # Latin text that is not obviously Uzbek is more likely Uzbek in this
    # dataset than transliterated Russian.
    return "uz" if lat > 0 else "ru"


# ---------------------------------------------------------------------------
# Database loading
# ---------------------------------------------------------------------------

@dataclass
class Catalog:
    families: list[str]
    regions: list[str]
    statuses: list[str]
    contract_types: list[str]
    forms: list[str]
    auction_types: list[str]
    units: list[str]
    companies: list[dict]
    brokers: list[dict]

    @classmethod
    def from_db(cls, con: sqlite3.Connection) -> "Catalog":
        con.row_factory = sqlite3.Row
        families = [r[0] for r in con.execute(
            "SELECT product_family FROM deals GROUP BY product_family ORDER BY COUNT(*) DESC"
        )]
        regions = [r[0] for r in con.execute(
            "SELECT region FROM deals WHERE region IS NOT NULL GROUP BY region ORDER BY COUNT(*) DESC"
        )]
        statuses = [r[0] for r in con.execute(
            "SELECT status FROM deals GROUP BY status ORDER BY COUNT(*) DESC"
        )]
        contract_types = [r[0] for r in con.execute(
            "SELECT contract_type FROM deals GROUP BY contract_type"
        )]
        forms = [r[0] for r in con.execute("SELECT form FROM deals GROUP BY form")]
        auction_types = [r[0] for r in con.execute(
            "SELECT auction_type FROM deals GROUP BY auction_type"
        )]
        units = [r[0] for r in con.execute("SELECT unit FROM deals GROUP BY unit")]
        companies = [
            {"inn": r["inn"], "name": r["name"], "name_norm": r["name_norm"], "region": r["region"]}
            for r in con.execute("SELECT inn, name, name_norm, region FROM participants")
        ]
        brokers = [
            {"code": r["code"], "name": r["name"], "name_norm": r["name_norm"], "region": r["region"]}
            for r in con.execute("SELECT code, name, name_norm, region FROM brokers")
        ]
        return cls(families, regions, statuses, contract_types, forms, auction_types, units, companies, brokers)
