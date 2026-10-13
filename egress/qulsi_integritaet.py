"""Integritätsprüfung der Connectoren: Prüfsummen über jeden Connector-Ordner, signiert mit Ed25519.

connectors/SIGNATUR.json enthält die SHA-256-Summe jedes Connector-Ordners (Manifest und Code),
dazu "_basis" für die gemeinsame Bibliothek (_lib) und das Image (Dockerfile, requirements.txt),
und die Signatur darüber. Der öffentliche Schlüssel liegt in config/signatur.pub, der private
Schlüssel nur bei dir (scripts/signieren.py). Core und Egress prüfen beim Start:
  - Passt ein Connector-Ordner nicht, wird dieser Connector nicht geladen und bekommt keinen Internetzugang.
  - Passt "_basis" oder die Signatur nicht, gilt das für alle Connectoren.

Diese Datei gibt es zweimal (egress/ und core/qulsi_core/), ein Test stellt sicher, dass beide gleich sind.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

BASIS = "_basis"
IGNORIEREN = {"__pycache__", ".pytest_cache", "SIGNATUR.json"}


def _dateien(ordner: Path) -> list[Path]:
    return sorted(p for p in ordner.rglob("*")
                  if p.is_file() and not (set(p.relative_to(ordner).parts) & IGNORIEREN) and p.suffix != ".pyc")


def ordner_summe(ordner: Path, dateien: list[Path] | None = None) -> str:
    h = hashlib.sha256()
    for p in dateien if dateien is not None else _dateien(ordner):
        h.update(p.relative_to(ordner).as_posix().encode() + b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def summen(connectors_dir: str | Path) -> dict[str, str]:
    d = Path(connectors_dir)
    out = {c.name: ordner_summe(c) for c in sorted(d.iterdir())
           if c.is_dir() and (c / "manifest.yaml").exists()}
    basis = [p for p in _dateien(d / "_lib")] if (d / "_lib").is_dir() else []
    basis += [d / n for n in ("Dockerfile", "requirements.txt") if (d / n).is_file()]
    out[BASIS] = ordner_summe(d, sorted(basis))
    return out


def nachricht(werte: dict[str, str]) -> bytes:
    return json.dumps({"connectoren": werte}, sort_keys=True, separators=(",", ":")).encode()


def pruefen(connectors_dir: str | Path, schluessel_datei: str | Path | None) -> tuple[dict[str, str], str | None]:
    """Gibt (Fehler je Connector, Fehler für alle) zurück. Leere Ergebnisse heißen: alles in Ordnung.

    Ohne Schlüsseldatei wird nicht geprüft (z. B. RunPod-Test ohne Signatur)."""
    if not schluessel_datei or not Path(schluessel_datei).is_file():
        return {}, None
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    datei = Path(connectors_dir) / "SIGNATUR.json"
    if not datei.is_file():
        return {}, "connectors/SIGNATUR.json fehlt (scripts/signieren.py ausführen)"
    try:
        sig = json.loads(datei.read_text(encoding="utf-8"))
        schluessel = Ed25519PublicKey.from_public_bytes(base64.b64decode(Path(schluessel_datei).read_text().strip()))
        schluessel.verify(base64.b64decode(sig["signatur"]), nachricht(sig["connectoren"]))
    except (InvalidSignature, KeyError, ValueError) as exc:
        return {}, f"Signatur ungültig ({type(exc).__name__})"
    soll: dict[str, str] = sig["connectoren"]
    ist = summen(connectors_dir)
    if ist.get(BASIS) != soll.get(BASIS):
        return {}, "Gemeinsame Connector-Bibliothek oder Image-Dateien wurden verändert"
    fehler = {}
    for name, summe in ist.items():
        if name == BASIS:
            continue
        if name not in soll:
            fehler[name] = "nicht signiert"
        elif soll[name] != summe:
            fehler[name] = "Dateien wurden nach dem Signieren verändert"
    return fehler, None
