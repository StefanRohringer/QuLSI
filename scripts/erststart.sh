#!/usr/bin/env bash
# Erster Start einer neuen Box: .env aus .env.example mit eigenen, zufälligen Schlüsseln anlegen.
# Jede Box bekommt ihre eigenen Werte (App-Schlüssel, Datenbank-Passwörter, LiteLLM, SearXNG, Open WebUI,
# ntfy-Thema). Tresor-Schlüssel, Web-Push-Schlüssel und der Einrichtungscode für die Freigabe-PIN
# entstehen beim ersten Start der Dienste von selbst. Name, Bundesland und PIN fragt die App ab.
#   scripts/erststart.sh            legt .env an (bricht ab, wenn es sie schon gibt)
set -euo pipefail
cd "$(dirname "$0")/.."
# Ordner für Hochladen aus der App: der Core (Benutzer 10001) schreibt nur in diese Unterordner
wert() { if [ -f .env ]; then sed -n "s/^$1=//p" .env | tail -1; fi; }
FOTOS=$(wert FOTOS_DIR); FILES=$(wert FILES_DIR)
for d in "${FOTOS:-./data/fotos}/Hochgeladen" "${FILES:-./data/files}/Hochgeladen"; do
  mkdir -p "$d"
  if [ "$(id -u)" = 0 ]; then chown 10001 "$d"; else chmod 777 "$d" 2>/dev/null || true; fi
done
[ -e .env ] && { echo ".env gibt es schon, nichts geändert (Ordner für Hochladen sind angelegt)."; exit 0; }
umask 077
python3 - <<'PY'
import re, secrets
from pathlib import Path
zeilen = []
for z in Path(".env.example").read_text(encoding="utf-8").splitlines():
    m = re.match(r"^([A-Z0-9_]+)=(.*)$", z)
    if m and m[2] in ("bitte-aendern", "sk-bitte-aendern"):
        wert = secrets.token_hex(24)
        z = f"{m[1]}={'sk-' + wert if m[2].startswith('sk-') else wert}"
    elif m and m[1] == "NTFY_TOPIC":
        z = f"NTFY_TOPIC=qulsi-{secrets.token_hex(8)}"
    zeilen.append(z)
Path(".env").write_text("\n".join(zeilen) + "\n", encoding="utf-8")
PY
chmod 600 .env
echo ".env mit eigenen Schlüsseln angelegt. Kalender, Kameras usw. bei Bedarf in .env ergänzen."
