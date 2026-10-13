#!/usr/bin/env bash
# Zusätzliche Firewall-Regeln für die Sicherheitszonen (auf der Box, als root).
#
# Docker trennt die Zonen über interne Netze. Zwei Netze brauchen trotzdem Regeln:
#   lan       (nur mit docker-compose.kameras.yml) Vision erreicht die Kameras, sonst nichts
#   internet  Egress-Proxy nur auf den Ports aus den Manifesten, SearXNG und ntfy nur 80/443.
#             Selbst wenn ein Container ausbricht, lässt der Host nichts anderes raus.
#   edge  veröffentlichte Ports (Web-App, ntfy) ohne Weg nach draußen
#
#   KAMERA_NETZ=192.168.1.0/24 sudo scripts/firewall.sh        Regeln setzen
#   sudo scripts/firewall.sh --aus                              Regeln entfernen
#
# Nach dem Start der Container ausführen. Die Regeln gelten bis zum Neustart;
# dauerhaft z. B. über einen systemd-Dienst, der dieses Skript nach docker.service aufruft.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJEKT=${COMPOSE_PROJECT_NAME:-$(basename "$(pwd)")}
KETTE=QuLSI-ZONEN

subnetz() { docker network inspect "${PROJEKT}_$1" -f '{{(index .IPAM.Config 0).Subnet}}'; }

iptables -D DOCKER-USER -j "$KETTE" 2>/dev/null || true
iptables -F "$KETTE" 2>/dev/null || true
iptables -X "$KETTE" 2>/dev/null || true
[ "${1:-}" = "--aus" ] && { echo "Regeln entfernt."; exit 0; }

LAN=$(subnetz lan 2>/dev/null || true)
EDGE=$(subnetz edge)
NETZ=$(subnetz internet)
EGRESS_IP=$(docker inspect -f "{{(index .NetworkSettings.Networks \"${PROJEKT}_internet\").IPAddress}}" "$(docker compose ps -q egress)")
# Ports aus allen Manifesten (allowed_ports und host:port), so wie der Egress-Proxy sie sieht
EGRESS_PORTS=$(docker compose exec -T egress python -c "
from qulsi_egress import connectoren_laden
print(','.join(sorted({str(p) for c in connectoren_laden('/connectors').values() for r in c.regeln for p in r.ports})))")
[ -n "$LAN" ] && : "${KAMERA_NETZ:?KAMERA_NETZ setzen, z. B. 192.168.1.0/24 oder einzelne Kamera-IPs mit Komma}"

iptables -N "$KETTE"
iptables -A "$KETTE" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
# Vision (Netz lan): nur Kameras (RTSP 554, HTTP 80/443 für Schnappschüsse)
if [ -n "$LAN" ]; then
  IFS=, read -ra NETZE <<< "$KAMERA_NETZ"
  for n in "${NETZE[@]}"; do
    iptables -A "$KETTE" -s "$LAN" -d "$n" -p tcp -m multiport --dports 554,80,443,8554 -j RETURN
  done
  iptables -A "$KETTE" -s "$LAN" -j DROP
fi
# Netz internet: Egress nur Manifest-Ports (höchstens 15 je Regel, sonst in Gruppen), Rest nur 80/443
IFS=, read -ra PORTS <<< "${EGRESS_PORTS:-443}"
for ((i = 0; i < ${#PORTS[@]}; i += 15)); do
  gruppe=$(IFS=,; echo "${PORTS[*]:i:15}")
  iptables -A "$KETTE" -s "$EGRESS_IP" ! -d "$NETZ" -p tcp -m multiport --dports "$gruppe" -j RETURN
done
iptables -A "$KETTE" -s "$EGRESS_IP" ! -d "$NETZ" -j DROP
iptables -A "$KETTE" -s "$NETZ" ! -d "$NETZ" -p tcp -m multiport --dports 80,443 -j RETURN
iptables -A "$KETTE" -s "$NETZ" ! -d "$NETZ" -j DROP
# Web-App und ntfy (Netz edge): antworten ja, selbst Verbindungen aufbauen nein
iptables -A "$KETTE" -s "$EDGE" ! -d "$EDGE" -j DROP
iptables -I DOCKER-USER -j "$KETTE"
echo "Regeln gesetzt: Egress ($EGRESS_IP) nur Ports $EGRESS_PORTS, edge ($EDGE) ohne Verbindungen nach draußen${LAN:+, lan ($LAN) nur zu $KAMERA_NETZ}."
echo "Nach Änderungen an Manifesten oder Neustart der Container erneut ausführen."
