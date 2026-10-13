#!/usr/bin/env bash
# Benennt das Produkt um: in allen Texten, Dateinamen und Ordnernamen.
#   scripts/umbenennen.sh ALT_TECHNISCH NEU_TECHNISCH ALT_ANZEIGE NEU_ANZEIGE
#   z. B. scripts/umbenennen.sh altname neuname AltName NeuName
# Technischer Name: klein geschrieben (Ordner, Pakete, Container, Hostnamen);
# daraus entstehen auch die Großschreibung (Umgebungsvariablen wie QULSI_API_KEY).
# Anzeigename: so, wie er in App, Texten und Dokumentation erscheint.
set -euo pipefail
if [ $# -ne 4 ]; then sed -n '2,7p' "$0"; exit 1; fi
ALT="$1"; NEU="$2"; ALT_ANZ="$3"; NEU_ANZ="$4"
ALT_GROSS="${ALT^^}"; NEU_GROSS="${NEU^^}"
cd "$(dirname "$0")/.."

# 1. Texte (keine Bilder, keine Git-Daten, keine Caches)
grep -rIl --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=.pytest_cache --exclude-dir=data \
  --exclude-dir=models -e "$ALT_ANZ" -e "$ALT_GROSS" -e "$ALT" . | while read -r f; do
  # Erst Bezeichner wie ALT_API_KEY (bleiben groß), dann der Anzeigename, dann der Rest
  sed -i -e "s/${ALT_GROSS}_/${NEU_GROSS}_/g" -e "s/${ALT_ANZ}/${NEU_ANZ}/g" -e "s/${ALT_GROSS}/${NEU_GROSS}/g" -e "s/${ALT}/${NEU}/g" "$f" || echo "Nicht geändert: $f" >&2
done

# 2. Datei- und Ordnernamen (tiefste zuerst, damit Pfade gültig bleiben)
find . -depth -not -path './.git/*' \( -name "*${ALT}*" -o -name "*${ALT_GROSS}*" -o -name "*${ALT_ANZ}*" \) | while read -r p; do
  d=$(dirname "$p"); b=$(basename "$p")
  n="${b//${ALT_GROSS}_/${NEU_GROSS}_}"; n="${n//${ALT_ANZ}/${NEU_ANZ}}"; n="${n//${ALT_GROSS}/${NEU_GROSS}}"; n="${n//${ALT}/${NEU}}"
  if [ "$b" != "$n" ]; then mv "$p" "$d/$n"; fi
done

echo "Umbenannt: ${ALT_ANZ} -> ${NEU_ANZ}, ${ALT} -> ${NEU}, ${ALT_GROSS} -> ${NEU_GROSS}"
echo "Rest:"; grep -rIni --exclude-dir=.git -e "$ALT" . | head || true
