"""Small allowlist sanitizer for Telegram HTML.

The bot builds HTML from database values and from user-supplied entity terms.
Telegram's HTML parse mode is not a security boundary, so every dynamic value
must be treated as text, not markup.  This sanitizer keeps only the formatting
tags the bot itself uses and escapes everything else.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser

_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "s", "code", "pre", "blockquote",
}


class _TelegramHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            raw = self.get_starttag_text() or f"<{tag}>"
            self.parts.append(html.escape(raw, quote=False))
            return
        if tag == "blockquote":
            self.parts.append("<blockquote expandable>")
        else:
            self.parts.append(f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Telegram does not use XML self-closing tags; render an allowed empty
        # pair, otherwise show the literal text.
        tag = tag.lower()
        if tag in _ALLOWED_TAGS:
            self.handle_starttag(tag, attrs)
            self.parts.append(f"</{tag}>")
        else:
            raw = self.get_starttag_text() or f"<{tag}/>"
            self.parts.append(html.escape(raw, quote=False))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _ALLOWED_TAGS:
            self.parts.append(f"</{tag}>")
        else:
            self.parts.append(html.escape(f"</{tag}>", quote=False))

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        # Comments are never needed in bot output.  Drop them rather than
        # trying to preserve arbitrary text inside markup.
        return

    def handle_decl(self, decl: str) -> None:
        return

    def unknown_decl(self, data: str) -> None:
        return


def sanitize_telegram_html(text: str) -> str:
    """Return text with only the bot's safe formatting tags left intact."""
    if not text:
        return ""
    parser = _TelegramHTMLSanitizer()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # Never fail an answer because of malformed markup; fall back to a
        # fully escaped rendering.
        return html.escape(text, quote=False)
    return "".join(parser.parts)
