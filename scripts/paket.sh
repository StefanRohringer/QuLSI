#!/usr/bin/env bash
# Packt QuLSI als qulsi.tar.gz zum Herunterladen (z. B. für RunPod).
#   bash scripts/paket.sh [ziel.tar.gz]
# Ohne Daten, Modelle, Schlüssel und persönliche Dateien. Besitzer werden nicht mitgepackt
# (Netzspeicher ohne chown), Ausführungsrechte für Skripte werden immer gesetzt, auch wenn der
# Quellordner sie verloren hat.
set -euo pipefail
cd "$(dirname "$0")/.."
ZIEL=$(realpath -m "${1:-qulsi.tar.gz}")
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/qulsi"
tar -cf - --exclude=./.git --exclude=./data --exclude=./models --exclude=./.env --exclude=./qulsi.tar.gz \
    --exclude=./config/signatur.pub --exclude=./connectors/SIGNATUR.json --exclude='./config/stimme/stimme.*' --exclude=./config/stimme/ausstehend \
    --exclude=./scripts/restic.env --exclude=__pycache__ --exclude=.pytest_cache --exclude='*.pyc' . \
  | tar -xf - -C "$TMP/qulsi"
mkdir -p "$TMP/qulsi/data" "$TMP/qulsi/models"
# Versionsstand: Datum und Uhrzeit des Pakets, dazu die Liste aus NEUES.txt (eine Zeile je Neuerung).
# Die App zeigt das unter Einstellungen > System, start.sh und qulsi.sh status geben es aus.
VERSION=$(TZ=Europe/Berlin date +%Y.%m.%d-%H%M)
GEBAUT=$(TZ=Europe/Berlin date --iso-8601=seconds)
VERSION="$VERSION" GEBAUT="$GEBAUT" python3 - "$TMP/qulsi" <<'PY'
import json, os, sys
from pathlib import Path
ziel = Path(sys.argv[1])
neu_datei = ziel / "NEUES.txt"
neu = [z.strip().lstrip("-* ").strip() for z in neu_datei.read_text(encoding="utf-8").splitlines()] if neu_datei.exists() else []
neu = [z for z in neu if z and not z.startswith("#")]
d = {"version": os.environ["VERSION"], "gebaut": os.environ["GEBAUT"], "neu": neu}
(ziel / "core/qulsi_core/version.json").write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
(ziel / "VERSION").write_text(f"QuLSI {d['version']} (gebaut {d['gebaut']})\n" + "".join(f"- {z}\n" for z in neu),
                              encoding="utf-8")
PY
find "$TMP/qulsi" -type f \( -name '*.sh' -o -path '*/scripts/*.py' \) -exec chmod 755 {} +
tar -czf "$ZIEL" -C "$TMP/qulsi" --owner=0 --group=0 --numeric-owner .
echo "Paket: $ZIEL ($(du -h "$ZIEL" | cut -f1)), Version $VERSION"
