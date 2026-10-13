#!/usr/bin/env bash
# ntfy auf der Box einrichten: Benutzer "qulsi" mit zufälligem Passwort, Token für den Core, zufälliges Thema.
# ntfy läuft mit deny-all: ohne Token kann der Core keine Freigaben schicken, ohne Passwort liest niemand mit.
#   scripts/ntfy-einrichten.sh            einmalig nach dem ersten "docker compose up -d"
#   scripts/ntfy-einrichten.sh --neu      neues Passwort und neuer Token (alte gelten weiter, bis gelöscht)
# Zeigt am Ende Server, Benutzer, Passwort und Thema für die ntfy-App (nicht in Chats kopieren).
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=.env
[ -f "$ENV" ] || { echo "Keine .env gefunden (cp .env.example .env)"; exit 1; }
wert() { grep -E "^$1=" "$ENV" | tail -1 | cut -d= -f2- | sed -E "s/^['\"]|['\"]$//g"; }
setzen() {
  local tmp; tmp=$(mktemp "$ENV.XXXXXX")
  grep -v "^$1=" "$ENV" > "$tmp" || true
  printf "%s=%s\n" "$1" "$2" >> "$tmp"; chmod 600 "$tmp"; mv "$tmp" "$ENV"
}
if [ -n "$(wert NTFY_TOKEN)" ] && [ "${1:-}" != "--neu" ]; then
  echo "ntfy ist schon eingerichtet (NTFY_TOKEN gesetzt). Neu: scripts/ntfy-einrichten.sh --neu"; exit 0
fi
N=(docker compose exec -T ntfy ntfy)
for _ in $(seq 30); do docker compose exec -T ntfy test -f /var/lib/ntfy/auth.db 2>/dev/null && break; sleep 1; done
if [ "$(wert NTFY_TOPIC)" = "qulsi" ] || [ -z "$(wert NTFY_TOPIC)" ]; then
  setzen NTFY_TOPIC "qulsi-$(openssl rand -hex 8)"
fi
PW=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-24)
if "${N[@]}" user list 2>&1 | grep -q "^user qulsi "; then
  docker compose exec -T -e NTFY_PASSWORD="$PW" ntfy ntfy user change-pass qulsi >/dev/null
else
  docker compose exec -T -e NTFY_PASSWORD="$PW" ntfy ntfy user add --role=admin qulsi >/dev/null
fi
T=$("${N[@]}" token add qulsi 2>&1 | grep -o 'tk_[A-Za-z0-9]*' | head -1)
[ -n "$T" ] || { echo "Token konnte nicht angelegt werden" >&2; exit 1; }
setzen NTFY_TOKEN "$T"
docker compose up -d core >/dev/null
cat <<EOF
ntfy ist eingerichtet. In der ntfy-App auf dem Handy:
  Server:   $(wert NTFY_PUBLIC_URL)
  Benutzer: qulsi
  Passwort: $PW      (wird nirgends gespeichert, jetzt im Handy eintragen)
  Thema:    $(wert NTFY_TOPIC)
EOF
