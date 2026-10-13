#!/usr/bin/env bash
# Kleine Steuerung für den Pod:
#   qulsi.sh status | logs <dienst> | restart [dienst] | stop | sichern | test | frage "<text>"
#   qulsi.sh anmelden google|microsoft   einmalige Anmeldung für die Konten-Connectoren
#   qulsi.sh schluessel                  App-Schlüssel anzeigen
#   qulsi.sh pin [--aus]                 Einrichtungscode für die Freigabe-PIN in der App (oder PIN entfernen)
#   qulsi.sh geheimnis NAME [dienst]     Passwort/Token in den Tresor (ohne Tresor: env-Datei), Dienst neu starten
#   qulsi.sh pruefen [--ohne-web]       Abnahme: echte Testsätze gegen Bonsai, Tabelle OK/FEHLER
#   qulsi.sh daten zeigen|suchen "<text>"|leeren   gespeicherte persönliche Daten anzeigen oder löschen
set -euo pipefail
source "$(dirname "$0")/paths.sh"
set -a; source "$ENV_FILE"; set +a
CTL=("$VENV/bin/python" -m supervisor.supervisorctl -c "$DATA/supervisord.conf")

case "${1:-status}" in
  status)  head -1 "$APP/VERSION" 2>/dev/null || echo "QuLSI (Version unbekannt, Paket ohne VERSION)"
           "${CTL[@]}" status || true
           bash "$APP/runpod/run.sh" core pin-code --wenn-leer 2>/dev/null || true ;;
  logs)    tail -n "${3:-80}" -f "$DATA/logs/${2:-core}.log" ;;
  restart) "${CTL[@]}" restart "${2:-all}" ;;
  stop)
    if [ -f "$DATA/pg-lokal" ]; then
      echo "== Datenbank sichern (liegt auf der Container-Disk)"
      bash "$APP/runpod/pg-sicherung.sh" || { echo "Sicherung fehlgeschlagen, Dienste laufen weiter" >&2; exit 1; }
    fi
    "${CTL[@]}" shutdown 2>/dev/null || true
    # supervisorctl kehrt sofort zurück; warten, bis supervisord und alle Dienste wirklich weg sind
    SV=$(cat "$DATA/supervisord.pid" 2>/dev/null || true)
    for _ in $(seq 60); do
      { [ -n "$SV" ] && [ -d "/proc/$SV" ]; } || break
      sleep 1
    done
    qulsi_reste_beenden
    if [ -n "$(qulsi_reste)" ]; then echo "Nicht alles beendet:" >&2; ps -o pid,cmd -p "$(qulsi_reste | paste -sd,)" >&2; exit 1; fi
    echo "QuLSI ist gestoppt." ;;
  sichern) bash "$APP/runpod/pg-sicherung.sh" ;;
  schluessel) echo "$QULSI_API_KEY" ;;
  anmelden)
    [[ "${2:-}" =~ ^(google|microsoft)$ ]] || { echo "qulsi.sh anmelden google|microsoft"; exit 1; }
    bash "$APP/runpod/run.sh" "connector-$2" anmelden && "${CTL[@]}" restart "connector-$2" ;;
  pin)     # Einrichtungscode für die Freigabe-PIN (erste PIN oder vergessene PIN, in der App eingeben)
    bash "$APP/runpod/run.sh" core pin-code ${2:+"$2"}
    [ "${2:-}" = "--aus" ] && "${CTL[@]}" restart core ;;
  geheimnis)
    name="${2:-}"
    [[ "$name" =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo "qulsi.sh geheimnis NAME [dienst], z. B. HA_TOKEN connector-home_assistant"; exit 1; }
    if [ -f "$DATA/tresor-an" ]; then   # verschlüsselt in den Tresor (geht auch in der App)
      DATA_DIR="$DATA/tresor" CONNECTORS_DIR="$APP/connectors" "$VENV/bin/python" "$APP/tresor/qulsi_tresor.py" setzen "$name"
    else
      read -r -s -p "Wert für $name (wird nicht angezeigt): " wert; echo
      [[ "$wert" != *$'\n'* && "$wert" != *"'"* ]] || { echo "Fehler: Zeilenumbruch oder ' im Wert" >&2; exit 1; }
      tmp=$(mktemp "$ENV_FILE.XXXXXX")
      grep -v "^$name=" "$ENV_FILE" > "$tmp" || true
      printf "%s='%s'\n" "$name" "$wert" >> "$tmp"
      chmod 600 "$tmp" && mv "$tmp" "$ENV_FILE"
      echo "$name gespeichert (unverschlüsselt in $ENV_FILE, nur für Test-Konten)."
    fi
    if [ -n "${3:-}" ]; then "${CTL[@]}" restart "$3"; fi ;;
  frage)
    curl -sS localhost:8100/api/chat -H "Authorization: Bearer $QULSI_API_KEY" -H "Content-Type: application/json" \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"nachricht": sys.argv[1]}))' "$2")" \
      | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["antwort"]); print("Route:", r["route"], r["modell"], "Werkzeuge:", r["werkzeuge"])' ;;
  push-test)   # Testmitteilung an alle Handys mit erlaubten Benachrichtigungen
    curl -sS -X POST localhost:8100/api/push/test -H "Authorization: Bearer $QULSI_API_KEY" -H "Content-Type: application/json" -d '{}'; echo ;;
  test)
    echo "== Dienste";  "${CTL[@]}" status || true
    echo "== Bonsai";   curl -sf "localhost:$PORT_BONSAI/health" && echo
    echo "== Core";     curl -sf localhost:8100/health && echo
    echo "== Vision";   curl -sf localhost:8200/health && echo
    echo "== Voice";    curl -sf localhost:8300/health && echo
    echo "== Tempo (100 Tokens, Denkmodus aus)"
    python3 - "$LITELLM_MASTER_KEY" <<'PY'
import json, sys, time, urllib.request
req = urllib.request.Request("http://127.0.0.1:4000/v1/chat/completions",
    data=json.dumps({"model": "bonsai-fast", "max_tokens": 100,
                     "messages": [{"role": "user", "content": "Erzähl mir in drei Sätzen etwas über Bonsai-Bäume."}]}).encode(),
    headers={"Authorization": f"Bearer {sys.argv[1]}", "Content-Type": "application/json"})
# Aufwärmen: der erste Aufruf nach dem Start lädt das Modell in den Grafikspeicher und zählt nicht
warm = urllib.request.Request(req.full_url, data=json.dumps({"model": "bonsai-fast", "max_tokens": 5,
    "messages": [{"role": "user", "content": "Hallo"}]}).encode(), headers=dict(req.header_items()))
urllib.request.urlopen(warm, timeout=600).read()
t = time.time(); r = json.load(urllib.request.urlopen(req, timeout=300)); dt = time.time() - t
n = r["usage"]["completion_tokens"]
print(r["choices"][0]["message"]["content"].strip()[:300]); print(f"{n} Tokens in {dt:.1f} s, etwa {n/dt:.0f} Tokens/s inkl. Prompt")
PY
    echo "== Router und Werkzeuge"
    bash "$0" frage "Wie spät ist es?"
    bash "$0" frage "Wann ist der nächste Feiertag?" ;;   # nur lesend: der Test speichert nichts
  pruefen) # Abnahme gegen das echte Modell (OK/FEHLER je Testsatz), räumt danach auf. Vor jeder Auslieferung.
    bash "$APP/runpod/run.sh" core pruefen "${@:2}" ;;
  daten)   # daten zeigen | daten suchen "<text>" | daten leeren   (persönliche Daten, z. B. aus Testgesprächen)
    [[ "${2:-}" =~ ^(zeigen|suchen|leeren)$ ]] || { echo 'qulsi.sh daten zeigen | suchen "<text>" | leeren'; exit 1; }
    bash "$APP/runpod/run.sh" core daten "${@:2}"
    [ "$2" = zeigen ] || "${CTL[@]}" restart core ;;
  *) echo "Unbekannt: $1"; exit 1 ;;
esac
