"""Hält Schlüssel, Tokens und Passwörter aus allen Logs heraus.

Maskiert key=, token=, password=, secret=, pin=, code= ... in URLs und Texten sowie "Bearer <wert>",
bevor eine Zeile geschrieben wird: uvicorn-Zugriffslog, httpx und alle eigenen Logger.
"""

from __future__ import annotations

import logging
import re

NAMEN = r"(?:api[_-]?key|key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|ticket|password|passwort|" \
        r"passwd|secret|client[_-]?secret|pin|code|device[_-]?code|sig|signature|auth|authorization)"
MUSTER = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
    (re.compile(rf"(?i)([?&;](?:{NAMEN}|t)=)[^&\s\"'#]+"), r"\1***"),   # t: Event-Token der Web-App
    (re.compile(rf"(?i)(\b{NAMEN}[\"']?\s*[:=]\s*[\"']?)(?!\*\*\*|bearer\b)[^\s\"',&}}]{{4,}}"), r"\1***"),
]


def maskieren(text: str) -> str:
    for muster, ersatz in MUSTER:
        text = muster.sub(ersatz, text)
    return text


class Maske(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        neu = maskieren(text)
        if neu != text:
            record.msg, record.args = neu, ()
        if record.exc_info and not record.exc_text:
            record.exc_text = maskieren(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = maskieren(record.exc_text)
        return True


def installieren() -> None:
    """An alle Handler und die bekannten Logger hängen (auch die von uvicorn, die schon eingerichtet sind)."""
    maske = Maske()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "httpcore", "fastapi"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, Maske) for f in lg.filters):
            lg.addFilter(maske)
        for h in lg.handlers:
            if not any(isinstance(f, Maske) for f in h.filters):
                h.addFilter(maske)
