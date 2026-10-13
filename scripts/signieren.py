#!/usr/bin/env python3
"""Signiert alle Connectoren: schreibt connectors/SIGNATUR.json.

    python3 scripts/signieren.py                 nach jeder Änderung an einem Connector
    python3 scripts/signieren.py --pruefen       nur prüfen, nichts schreiben

Beim ersten Aufruf entsteht ein Schlüsselpaar: der öffentliche Teil in config/signatur.pub
(gehört ins Repo), der private in ~/.config/qulsi/signatur.key (QULSI_SIGNATUR_KEY), nur bei dir.
Ab dann laden Core und Egress nur noch Connectoren, deren Dateien zur Signatur passen.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "egress"))
from qulsi_integritaet import nachricht, pruefen, summen  # noqa: E402

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

PUB = ROOT / "config" / "signatur.pub"
KEY = Path(os.environ.get("QULSI_SIGNATUR_KEY", Path.home() / ".config" / "qulsi" / "signatur.key"))


def schluessel() -> Ed25519PrivateKey:
    if KEY.is_file():
        return serialization.load_pem_private_key(KEY.read_bytes(), password=None)
    if PUB.is_file():
        sys.exit(f"{PUB} gibt es schon, aber den privaten Schlüssel {KEY} nicht. "
                 "Entweder den Schlüssel zurückholen oder config/signatur.pub löschen (dann neues Paar).")
    k = Ed25519PrivateKey.generate()
    KEY.parent.mkdir(parents=True, exist_ok=True)
    KEY.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    KEY.chmod(0o600)
    roh = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    PUB.write_text(base64.b64encode(roh).decode() + "\n")
    print(f"Neues Schlüsselpaar: {PUB} (öffentlich), {KEY} (privat, gut aufheben)")
    return k


if __name__ == "__main__":
    d = ROOT / "connectors"
    if "--pruefen" in sys.argv:
        fehler, alle = pruefen(d, PUB)
        print(alle or "\n".join(f"{n}: {g}" for n, g in fehler.items()) or "Alle Connectoren passen zur Signatur.")
        sys.exit(1 if alle or fehler else 0)
    k = schluessel()
    werte = summen(d)
    sig = base64.b64encode(k.sign(nachricht(werte))).decode()
    (d / "SIGNATUR.json").write_text(json.dumps({"connectoren": werte, "signatur": sig}, indent=2, sort_keys=True) + "\n")
    print(f"Signiert: {', '.join(n for n in werte if not n.startswith('_'))}")
