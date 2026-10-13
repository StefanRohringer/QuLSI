#!/usr/bin/env bash
# Startet QuLSI im Testbetrieb (ohne GPU, mit fake-llm und Ersatz für Home Assistant,
# Kalender und Mailserver) und führt Unit- und Ende-zu-Ende-Tests aus.
#   scripts/test-stack.sh          Tests laufen lassen, Stack bleibt danach an
#   scripts/test-stack.sh --down   danach alles wieder stoppen und Testdaten löschen
# NO_BUILD=1 nutzt vorhandene Images.
set -euo pipefail
cd "$(dirname "$0")/.."
F="-f docker-compose.yml -f docker-compose.dev.yml -f tests/integration/docker-compose.test.yml"

if [ ! -f .env ]; then
  echo "Keine .env gefunden, lege Test-.env an"
  cat > .env <<'ENV'
POSTGRES_PASSWORD=testpw
LITELLM_MASTER_KEY=sk-test
QULSI_API_KEY=qulsi-test
WEBUI_SECRET_KEY=test
SEARXNG_SECRET=test
DB_PW_VISION=vision-test
DB_PW_CALENDAR=calendar-test
DB_PW_MAIL=mail-test
DB_PW_FILES=files-test
DB_PW_GOOGLE=google-test
DB_PW_MICROSOFT=microsoft-test
DB_PW_NAS=nas-test
QULSI_PORT=8100
PUBLIC_URL=http://localhost:8100
NTFY_PUBLIC_URL=http://localhost:8090
HA_TOKEN=ha-test
ENV
fi

python3 -m pip install -q pytest httpx pyyaml numpy opencv-python-headless fastapi psycopg[binary] mcp==2.2.0 qdrant-client==1.19.1 smbprotocol==1.17.0 pyrage==1.4.0 2>/dev/null || true
echo "== Unit-Tests"
python3 -m pytest -q tests/unit

echo "== Stack starten (frische Testdaten)"
docker compose $F down -v --remove-orphans >/dev/null 2>&1 || true
rm -rf data/postgres data/qdrant data/objects 2>/dev/null || sudo rm -rf data/postgres data/qdrant data/objects
mkdir -p data/objects data/memchip data/fotos data/egress data/tresor && chmod 777 data/objects data/memchip data/egress data/tresor
rm -rf data/test-stimme 2>/dev/null || sudo rm -rf data/test-stimme
mkdir -p data/test-stimme && chmod 777 data/test-stimme   # Stimmprobe im Test, nicht config/stimme
rm -rf data/test-dateien data/fotos/Hochgeladen 2>/dev/null || sudo rm -rf data/test-dateien data/fotos/Hochgeladen
cp -r tests/integration/dateien data/test-dateien && mkdir -p data/test-dateien/Hochgeladen data/fotos/Hochgeladen \
  && chmod 777 data/test-dateien/Hochgeladen data/fotos/Hochgeladen   # Hochladen aus der App im Test
rm -f data/egress/* data/tresor/* 2>/dev/null || sudo rm -f data/egress/* data/tresor/*
# Connectoren mit einem Testschlüssel signieren (Core und Egress prüfen die Signatur)
QULSI_SIGNATUR_KEY=data/test-signatur.key python3 scripts/signieren.py
# Test-Geheimnisse in den Tresor statt in die .env
docker compose $F run --rm -T --no-deps tresor setzen HA_TOKEN <<< "ha-test"
docker compose $F up -d ${NO_BUILD:+--no-build}
for i in $(seq 1 90); do
  if curl -sf localhost:8100/health | grep -q '"vision":true' && curl -sf localhost:8200/health >/dev/null; then break; fi
  sleep 2
done

echo "== Ende-zu-Ende-Tests"
python3 -m pytest -v tests/integration

if [ "${1:-}" = "--down" ]; then docker compose $F down -v; fi
