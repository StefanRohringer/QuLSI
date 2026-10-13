"""Modelle erst laden, wenn sie gebraucht werden, und nach einer Leerlaufzeit wieder freigeben.

Die Modelle laufen nie alle gleichzeitig: so belegt ein Modell nur Grafikspeicher, solange es
benutzt wird. Diese Datei gibt es dreimal (vision/, voice/, tts/), ein Test prüft, dass alle gleich sind.
"""

from __future__ import annotations

import gc
import logging
import sys
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)


class BeiBedarf:
    """Hülle um ein Modell. leerlauf_sekunden <= 0: sofort laden und nie freigeben."""

    def __init__(self, name: str, laden: Callable[[], Any], leerlauf_sekunden: float):
        self.name = name
        self._laden = laden
        self.leerlauf = leerlauf_sekunden
        self._modell: Any = None
        self._lock = threading.RLock()
        self._aktiv = 0
        self._zuletzt = 0.0
        if self.leerlauf <= 0:
            self._modell = laden()

    @property
    def geladen(self) -> bool:
        return self._modell is not None

    def _holen(self) -> Any:
        with self._lock:
            if self._modell is None:
                log.info("Lade %s", self.name)
                self._modell = self._laden()
            self._aktiv += 1
            return self._modell

    def _fertig(self) -> None:
        with self._lock:
            self._aktiv -= 1
            self._zuletzt = time.monotonic()

    def __getattr__(self, attr: str):
        if attr.startswith("_"):
            raise AttributeError(attr)

        def aufruf(*args, **kwargs):
            modell = self._holen()
            try:
                return getattr(modell, attr)(*args, **kwargs)
            finally:
                self._fertig()
        return aufruf

    def freigeben_wenn_leerlauf(self) -> bool:
        if self.leerlauf <= 0:
            return False
        return self.freigeben(nur_im_leerlauf=True)

    def freigeben(self, nur_im_leerlauf: bool = False) -> bool:
        """Modell aus dem Speicher nehmen (nicht während einer Anfrage); die nächste lädt es neu."""
        with self._lock:
            if self._modell is None or self._aktiv:
                return False
            if nur_im_leerlauf and time.monotonic() - self._zuletzt < self.leerlauf:
                return False
            self._modell = None
        gc.collect()
        torch = sys.modules.get("torch")
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
        log.info("%s freigegeben%s", self.name, " (Leerlauf)" if nur_im_leerlauf else "")
        return True


async def freigabe_schleife(modelle: list[BeiBedarf], takt: float = 30) -> None:
    import asyncio

    while True:
        await asyncio.sleep(takt)
        for m in modelle:
            await asyncio.to_thread(m.freigeben_wenn_leerlauf)
