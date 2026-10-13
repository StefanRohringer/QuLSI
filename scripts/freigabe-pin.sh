#!/usr/bin/env bash
# Freigabe-PIN der QuLSI-App: Einrichtungscode zeigen oder PIN entfernen (Box mit Docker Compose).
# Die PIN selbst legt man in der App unter Einstellungen fest; erste oder vergessene PIN nur mit diesem Code.
#
#   scripts/freigabe-pin.sh          neuer Einrichtungscode (15 Minuten gültig, nur einmal)
#   scripts/freigabe-pin.sh --aus    PIN entfernen
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T core python -m qulsi_core.pin_code "$@"
[ "${1:-}" = "--aus" ] && docker compose restart core >/dev/null
exit 0
