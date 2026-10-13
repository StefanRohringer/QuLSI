#!/usr/bin/env bash
# Lernt einen persönlichen Gegenstand an.
#
#   scripts/anlernen.sh "Schlüsselbund" keys "Schlüssel,Hausschlüssel" foto1.jpg foto2.jpg ...
#   KAMERA=flur scripts/anlernen.sh "Schlüsselbund" keys "Schlüssel"     (aktuelles Bild der Flurkamera)
#   BLE_TAG=tile_1234 scripts/anlernen.sh ...                            (Bluetooth-Anhänger zuordnen)
#
# typ muss ein Begriff aus config/cameras.yaml > vokabular sein (z. B. keys, wallet, glasses).
# Tipp: 3 bis 5 Fotos aus verschiedenen Blickwinkeln, Gegenstand füllt einen guten Teil des Bildes.
set -euo pipefail
NAME=$1; TYP=$2; ALIASES=${3:-}; shift 3 || shift $#
ARGS=(-F "name=$NAME" -F "typ=$TYP" -F "aliases=$ALIASES" -F "besitzer=${BESITZER:-}" -F "ble_tag=${BLE_TAG:-}" -F "kamera=${KAMERA:-}")
for f in "$@"; do ARGS+=(-F "bilder=@$f"); done
curl -sS -X POST "${VISION_URL:-http://localhost:8200}/anlernen" "${ARGS[@]}"
echo
