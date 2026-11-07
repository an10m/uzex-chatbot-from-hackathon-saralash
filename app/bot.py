"""Telegram bot entry point for the UZEX grounded assistant."""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import os
import re
import sys
import time
from logging.handlers import RotatingFileHandler

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agent import Agent
from .config import (
    ADMIN_CHAT_ID,
    DB_PATH,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    LOG_FILE,
    LOG_LEVEL,
    TELEGRAM_BOT_TOKEN,
)
from .html_safe import sanitize_telegram_html
from .llm import GeminiClient

log = logging.getLogger("uzex.bot")

# Lightweight anti-abuse limits.  The Telegram API has no built-in per-user
# quota, and every question costs one or more Gemini requests.  The limits are
# intentionally generous for a 10-question evaluation but stop a public bot
# from being used as a free LLM proxy.
_RATE_PER_MINUTE = 12
_RATE_PER_HOUR = 80
_GLOBAL_PER_MINUTE = 150
_RATE_WINDOW_SECONDS = 3600
# The evaluation allows 90 seconds per answer.  Leave a margin for sending the
# Telegram messages and for the event loop.
_QUESTION_DEADLINE_SECONDS = 80

_BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_GOOGLE_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")


def _redact_secrets(text: str) -> str:
    """Keep tokens and API keys out of logs and user-visible error paths."""
    text = _BOT_TOKEN_RE.sub("[REDACTED_TELEGRAM_TOKEN]", text)
    text = _GOOGLE_KEY_RE.sub("[REDACTED_GEMINI_KEY]", text)
    return text

EXAMPLES = [
    ("uz", "2025-yilda nechta bitim tuzilgan?"),
    ("uz", "2025-yilda paxta bo‘yicha qancha bitim va qancha kilogramm?"),
    ("uz", "Eng ko‘p bitim tuzgan 5 ta sotuvchi kimlar?"),
    ("uz", "504707-sonli bitim haqida ma’lumot bering."),
    ("uz", "100001-sonli shartnoma shartlari qanday?"),
    ("uz", "01.01.2025 dagi dollar kursi qancha?"),
    ("uz", "Toshkent shahridagi eng yirik xaridor kim?"),
    ("uz", "2024 va 2025 yillarni bitimlar soni bo‘yicha solishtiring."),
    ("ru", "Сколько сделок было в 2025 году?"),
    ("ru", "Сколько хлопка продано в 2025 году в килограммах?"),
    ("ru", "Топ-5 продавцов по количеству сделок."),
    ("ru", "Информация о сделке 504707."),
    ("ru", "Условия контракта 100001."),
    ("ru", "Какой курс доллара был 01.01.2025?"),
    ("ru", "Крупнейший покупатель в городе Ташкент."),
    ("ru", "Сравните 2024 и 2025 годы по количеству сделок."),
]

WELCOME = """Assalomu alaykum! Men UZEX birja bitimlari bo‘yicha AI-agentman.

Rus yoki o‘zbek tilida savol bering. Javobni faqat taqdim etilgan ma’lumotlar asosida beraman. Ma’lumotda javob bo‘lmasa, uni o‘ylab topmayman — ochiq aytaman.

Ma’lumotlar: 03.01.2022 – 30.10.2026, 500 000 bitim.

<b>Misollar:</b>
• 2025-yilda nechta bitim tuzilgan?
• 2025-yilda paxta bo‘yicha qancha bitim va qancha kilogramm?
• Eng ko‘p bitim tuzgan 5 ta sotuvchi kimlar?
• 504707-sonli bitim haqida ma’lumot bering.
• 01.01.2025 dagi dollar kursi qancha?

—————
Здравствуйте! Я AI-агент по сделкам UZEX.

Задайте вопрос на русском или узбекском языке. Отвечаю только по предоставленным данным; если ответа в данных нет — прямо об этом сообщу.

Данные: 03.01.2022 – 30.10.2026, 500 000 сделок.

<b>Примеры:</b>
• Сколько сделок было в 2025 году?
• Сколько хлопка продано в 2025 году в килограммах?
• Топ-5 продавцов по количеству сделок.
• Информация о сделке 504707.
• Какой курс доллара был 01.01.2025?
"""

HELP = """<b>Qanday savol berish mumkin / Как спрашивать</b>

Savolni oddiy tilda yozing: mahsulot, davr, hudud, kompaniya, broker, bitim yoki shartnoma raqami, dollar kursi.

<b>Misol / Пример:</b>
• 2025-yilda qancha paxta sotilgan?
• Какая компания купила больше всего цемента в 2025 году?
• 2025-yilda eksport bitimlari summasi qancha dollar?
• Broker BK001 qancha bitimda qatnashgan?
• 2024 va 2025 yillarni solishtiring.

Har bir javob ostida <b>Manba</b> bo‘limi bor: SQL so‘rov, qatorlar soni va ma’lumot davri. Bu javob nimaga asoslanganini ko‘rsatadi.

<b>Cheklovlar:</b> ma’lumotlar 03.01.2022 – 30.10.2026; prognoz, siyosat, ob-havo va real bozor ma’lumotlari yo‘q. Barcha kompaniyalar va summalar o‘ylab topilgan.
"""

ABOUT = """<b>UZEX AI Hackathon — saralash prototipi</b>

Agent 500 000 ta sintetik birja bitimi, 6 616 shartnoma, 14 392 kompaniya, 120 broker va kunlik dollar kursi bilan ishlaydi.

Ma’lumotlar 2022-yil 3-yanvardan 2026-yil 30-oktabrgacha. Barcha kompaniyalar, STIR, brokerlar, narxlar va summalar o‘ylab topilgan.

Javoblar faqat ma’lumotlar bazasidagi yozuvlar asosida shakllantiriladi. Ma’lumotda javob bo‘lmasa, agent buni ochiq aytadi.

Texnologiyalar: Python, SQLite, Gemini API, python-telegram-bot, SQL-guard va deterministik javob generatori.
"""


def setup_logging() -> None:
    # Keep newly created files private when the service runs outside systemd.
    try:
        os.umask(0o077)
    except OSError:
        pass
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        LOG_FILE.parent.chmod(0o700)
    except OSError:
        pass
    try:
        LOG_FILE.touch(exist_ok=True)
        LOG_FILE.chmod(0o600)
    except OSError:
        pass
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def examples_markup() -> InlineKeyboardMarkup:
    buttons = []
    for i, (_, text) in enumerate(EXAMPLES[:8]):
        buttons.append([InlineKeyboardButton(text[:60], callback_data=f"ex:{i}")])
    return InlineKeyboardMarkup(buttons)


def split_text(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current)
            current = line
            while len(current) > limit:
                chunks.append(current[:limit])
                current = current[limit:]
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


async def send_html(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    if not text:
        return
    # Database values and user terms are plain text.  Keep only our own
    # formatting tags so an answer can never inject arbitrary Telegram markup.
    text = sanitize_telegram_html(text)
    for chunk in split_text(text):
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            log.warning("HTML send failed (%s); sending plain text", _redact_secrets(str(exc)))
            plain = html_lib.unescape(re.sub(r"<[^>]+>", "", chunk))
            await context.bot.send_message(
                chat_id=chat_id,
                text=plain[:3900],
                disable_web_page_preview=True,
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data.clear()
    context.chat_data["history"] = []
    await update.effective_message.reply_text(
        WELCOME,
        parse_mode=ParseMode.HTML,
        reply_markup=examples_markup(),
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(ABOUT, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def examples(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Misol savollar / Примеры вопросов:",
        reply_markup=examples_markup(),
    )


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"Chat ID: <code>{update.effective_chat.id}</code>", parse_mode=ParseMode.HTML)


def _is_rate_limited(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Apply a small in-process per-user and global question quota."""
    if ADMIN_CHAT_ID and str(user_id) == ADMIN_CHAT_ID:
        return False
    now = time.monotonic()
    buckets: dict[str, list[float]] = context.application.bot_data.setdefault("rate_buckets", {})
    # Avoid unbounded growth if the bot is public for a long time.
    if len(buckets) > 2000:
        for key in list(buckets):
            values = buckets.get(key) or []
            if not values or now - values[-1] > _RATE_WINDOW_SECONDS:
                buckets.pop(key, None)

    def active(key: str) -> list[float]:
        values = buckets.setdefault(key, [])
        values[:] = [t for t in values if now - t < _RATE_WINDOW_SECONDS]
        return values

    user_times = active(f"u:{user_id}")
    global_times = active("_global")
    if (
        sum(1 for t in user_times if now - t < 60) >= _RATE_PER_MINUTE
        or len(user_times) >= _RATE_PER_HOUR
        or sum(1 for t in global_times if now - t < 60) >= _GLOBAL_PER_MINUTE
    ):
        return True
    user_times.append(now)
    global_times.append(now)
    return False


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # This is an operator command.  If no admin is configured, keep it closed
    # rather than exposing model, error and traffic information to everyone.
    if not ADMIN_CHAT_ID or str(update.effective_chat.id) != ADMIN_CHAT_ID:
        return
    s = context.application.bot_data.get("stats", {})
    total = s.get("questions", 0)
    errors = s.get("errors", 0)
    ms = s.get("total_ms", 0)
    avg = int(ms / total) if total else 0
    lines = [
        f"Savollar: {total}",
        f"Xatolar: {errors}",
        f"O‘rtacha javob: {avg} ms",
    ]
    llm = context.application.bot_data.get("llm")
    if llm is not None:
        ls = getattr(llm, "stats", {}) or {}
        lines.append(f"Model: {getattr(llm, 'model', '—')}")
        lines.append(f"LLM so‘rovlari: {ls.get('requests', 0)}")
        lines.append(f"Limit (429): {ls.get('rate_limited', 0)}")
        if ls.get("last_rate_limit"):
            lines.append(f"Oxirgi 429: {ls['last_rate_limit']}")
        if ls.get("last_error"):
            lines.append(f"Oxirgi xato: {str(ls['last_error'])[:180]}")
    await update.effective_message.reply_text("\n".join(lines))


async def warn_rate_limit(context: ContextTypes.DEFAULT_TYPE, count: int) -> None:
    """Send a throttled warning to the admin when Gemini returns 429.

    There is no API to read the remaining daily quota, so a 429 is the earliest
    reliable signal.  Alerts are throttled to one per five minutes.
    """
    if not ADMIN_CHAT_ID:
        return
    now = time.monotonic()
    last = float(context.application.bot_data.get("last_limit_alert", 0.0) or 0.0)
    if now - last < 300:
        return
    context.application.bot_data["last_limit_alert"] = now
    llm = context.application.bot_data.get("llm")
    ls = getattr(llm, "stats", {}) or {}
    text = (
        "⚠️ Gemini 429 (limit) aniqlandi.\n"
        f"Model: {getattr(llm, 'model', '—')}\n"
        f"429 soni: {count}\n"
        f"Oxirgi 429: {ls.get('last_rate_limit') or '—'}\n"
        "Bot zaxira modelga o‘tishga harakat qildi. Agar takrorlansa, kunlik limit tugagan bo‘lishi mumkin."
    )
    try:
        await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text)
    except (TelegramError, ValueError) as exc:
        log.warning("Could not send admin rate-limit alert: %s", _redact_secrets(str(exc)))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    if data.startswith("ex:"):
        try:
            idx = int(data.split(":", 1)[1])
            question = EXAMPLES[idx][1]
        except (ValueError, IndexError):
            return
        await query.message.reply_text(f"❓ {question}")
        await answer_question(
            context,
            query.message.chat_id,
            question,
            user_id=getattr(query.from_user, "id", None),
        )


async def answer_question(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    question: str,
    user_id: int | None = None,
) -> None:
    if _is_rate_limited(context, int(user_id or chat_id)):
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Juda ko‘p savol yuborildi. Iltimos, bir daqiqadan so‘ng qayta urinib ko‘ring."
                if not any("\u0400" <= c <= "\u04FF" for c in question)
                else "Слишком много вопросов. Пожалуйста, повторите через минуту."
            ),
        )
        return
    chat_data = context.chat_data
    if chat_data.get("busy"):
        await context.bot.send_message(
            chat_id=chat_id,
            text="Oldingi savolga javob tayyorlanmoqda. Iltimos, biroz kuting."
            if not any("\u0400" <= c <= "\u04FF" for c in question)
            else "Предыдущий вопрос ещё обрабатывается. Подождите немного.",
        )
        return
    chat_data["busy"] = True
    stats = context.application.bot_data.setdefault("stats", {"questions": 0, "errors": 0, "total_ms": 0})
    stats["questions"] += 1
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        agent: Agent = context.application.bot_data["agent"]
        llm = context.application.bot_data.get("llm")
        rate_before = int((getattr(llm, "stats", {}) or {}).get("rate_limited", 0))
        history = chat_data.get("history", [])
        try:
            async with context.application.bot_data["sem"]:
                answer = await asyncio.wait_for(
                    agent.ask(question, history=history),
                    timeout=_QUESTION_DEADLINE_SECONDS,
                )
        except asyncio.TimeoutError:
            stats["errors"] += 1
            await send_html(
                context,
                chat_id,
                (
                    "Javobni tayyorlash 80 soniyadan oshdi. Iltimos, savolni soddaroq "
                    "yoki aniqroq qilib qayta yuboring."
                    if not any("\u0400" <= c <= "\u04FF" for c in question)
                    else "Подготовка ответа заняла больше 80 секунд. Пожалуйста, "
                    "упростите или уточните вопрос и отправьте его ещё раз."
                ),
            )
            return
        rate_after = int((getattr(llm, "stats", {}) or {}).get("rate_limited", 0))
        if rate_after > rate_before:
            await warn_rate_limit(context, rate_after)
        stats["total_ms"] += answer.elapsed_ms or 0
        if answer.kind == "error":
            stats["errors"] += 1
        await send_html(context, chat_id, answer.text)
        if answer.source:
            await send_html(context, chat_id, answer.source)
        history.append({"q": question, "a": answer.text[:800]})
        chat_data["history"] = history[-4:]
    except TelegramError as exc:
        stats["errors"] += 1
        log.error("Telegram error: %s", _redact_secrets(str(exc)))
    except Exception as exc:
        stats["errors"] += 1
        log.error("Question handling failed: %s", _redact_secrets(f"{type(exc).__name__}: {exc}"))
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Texnik xatolik yuz berdi. Iltimos, savolni qayta yuboring. / Произошла техническая ошибка.",
            )
        except TelegramError:
            pass
    finally:
        chat_data["busy"] = False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    text = message.text.strip()
    if not text:
        return
    log.info(
        "Message from chat=%s user=%s",
        update.effective_chat.id,
        getattr(update.effective_user, "id", None),
    )
    await answer_question(
        context,
        update.effective_chat.id,
        text,
        user_id=getattr(update.effective_user, "id", None),
    )


async def handle_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Iltimos, savolni matn ko‘rinishida yuboring. / Пожалуйста, отправьте вопрос текстом."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc = context.error
    log.error("Update error: %s", _redact_secrets(f"{type(exc).__name__}: {exc}"))


async def post_shutdown(application: Application) -> None:
    client: GeminiClient | None = application.bot_data.get("llm")
    if client:
        await client.close()


def main() -> None:
    setup_logging()
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}\nRun: python app/build_db.py --data-dir uzex_spot_data --out data/uzex.sqlite")
        raise SystemExit(1)
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is missing. Create a bot with @BotFather and put the token in .env")
        raise SystemExit(1)
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY is missing. Create a key at https://aistudio.google.com/apikey and put it in .env")
        raise SystemExit(1)

    llm = GeminiClient(GEMINI_API_KEY, model=GEMINI_MODEL)
    agent = Agent(DB_PATH, llm)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_shutdown(post_shutdown).build()
    application.bot_data["agent"] = agent
    application.bot_data["llm"] = llm
    application.bot_data["sem"] = asyncio.Semaphore(4)
    application.bot_data["stats"] = {"questions": 0, "errors": 0, "total_ms": 0}

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about))
    application.add_handler(CommandHandler("examples", examples))
    application.add_handler(CommandHandler("id", chat_id))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(~filters.TEXT, handle_non_text))
    application.add_error_handler(error_handler)

    log.info("Starting Telegram polling (model=%s, db=%s)", GEMINI_MODEL, DB_PATH)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
