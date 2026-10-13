#!/usr/bin/env bash
# Verschlüsselt alle Daten von QuLSI auf der Box mit LUKS2 (AES-256-XTS): Datenbank, Qdrant,
# Object Storage mit Fotos und Bildausschnitten, Tresor, Gedächtnis-Exporte, .env und Konfiguration.
# QuLSI läuft danach aus einem verschlüsselten Ordner (Standard /srv/qulsi).
#
#   sudo scripts/verschluesseln.sh einrichten /dev/nvme0n1p3     eigene Partition (wird gelöscht!)
#   sudo scripts/verschluesseln.sh einrichten --datei 200G       ohne freie Partition: Containerdatei
#   sudo scripts/verschluesseln.sh umziehen                      QuLSI in den verschlüsselten Ordner kopieren
#   sudo scripts/verschluesseln.sh status                        liegt QuLSI verschlüsselt?
#
# Schlüssel: beim Einrichten vergibst du eine Passphrase (Wiederherstellung, gut aufheben).
# Hat die Box einen TPM-Chip, entsperrt er das Laufwerk beim Start automatisch, aber nur auf
# genau dieser Box mit unverändertem Secure Boot (PCR 7). Wird die SSD ausgebaut oder gestohlen,
# ist ohne Passphrase nichts lesbar. Ohne TPM fragt die Box beim Start nach der Passphrase.
set -euo pipefail

ZIEL="${QULSI_SICHER:-/srv/qulsi}"
NAME="qulsi-daten"
CONTAINER="${QULSI_CONTAINER:-/var/lib/qulsi-daten.luks}"
QUELLE="$(cd "$(dirname "$0")/.." && pwd)"

fehler() { echo "Fehler: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fehler "bitte mit sudo starten"

verschluesselt() {  # liegt der Ordner $1 auf einem dm-crypt-Gerät?
  local quelle
  quelle=$(findmnt -n -o SOURCE --target "$1" 2>/dev/null || true)
  [ -n "$quelle" ] && lsblk -s -n -o TYPE "$quelle" 2>/dev/null | grep -q crypt
}

case "${1:-}" in
  einrichten)
    command -v cryptsetup >/dev/null || fehler "cryptsetup fehlt: sudo apt install cryptsetup"
    if [ "${2:-}" = "--datei" ]; then
      groesse="${3:?Größe fehlt, z. B. 200G}"
      [ -e "$CONTAINER" ] && fehler "$CONTAINER gibt es schon"
      fallocate -l "$groesse" "$CONTAINER"
      chmod 600 "$CONTAINER"
      geraet="$CONTAINER"
    else
      geraet="${2:?Partition fehlt, z. B. /dev/nvme0n1p3}"
      [ -b "$geraet" ] || fehler "$geraet ist keine Partition"
      echo "ACHTUNG: alle Daten auf $geraet werden gelöscht."
      read -r -p "Zum Fortfahren JA eingeben: " ok
      [ "$ok" = "JA" ] || exit 1
    fi
    echo "== LUKS2 anlegen. Jetzt die Wiederherstellungs-Passphrase vergeben (lang, gut aufheben):"
    cryptsetup luksFormat --type luks2 --cipher aes-xts-plain64 --key-size 512 --pbkdf argon2id "$geraet"
    cryptsetup open "$geraet" "$NAME"
    mkfs.ext4 -q -L qulsi "/dev/mapper/$NAME"
    mkdir -p "$ZIEL"
    mount "/dev/mapper/$NAME" "$ZIEL"
    chmod 750 "$ZIEL"

    optionen="luks,discard"
    if [ -e /dev/tpmrm0 ] && command -v systemd-cryptenroll >/dev/null; then
      echo "== TPM gefunden: automatisches Entsperren beim Start (nur auf dieser Box)"
      systemd-cryptenroll --tpm2-device=auto --tpm2-pcrs=7 "$geraet" && optionen="$optionen,tpm2-device=auto"
    else
      echo "== Kein TPM: die Box fragt beim Start nach der Passphrase"
    fi
    uuid=$(cryptsetup luksUUID "$geraet")
    quelle_crypttab="UUID=$uuid"
    [ "$geraet" = "$CONTAINER" ] && quelle_crypttab="$CONTAINER"
    grep -q "^$NAME " /etc/crypttab 2>/dev/null || \
      echo "$NAME $quelle_crypttab none $optionen" >> /etc/crypttab
    grep -q " $ZIEL " /etc/fstab || echo "/dev/mapper/$NAME $ZIEL ext4 defaults,nofail 0 2" >> /etc/fstab
    # Docker erst starten, wenn der verschlüsselte Ordner da ist (sonst legt er leere Ordner an)
    mkdir -p /etc/systemd/system/docker.service.d
    printf '[Unit]\nRequiresMountsFor=%s\n' "$ZIEL" > /etc/systemd/system/docker.service.d/qulsi-daten.conf
    systemctl daemon-reload 2>/dev/null || true
    echo "Fertig: $ZIEL ist verschlüsselt. Weiter mit: sudo scripts/verschluesseln.sh umziehen"
    ;;

  umziehen)
    verschluesselt "$ZIEL" || fehler "$ZIEL ist nicht verschlüsselt (erst: einrichten)"
    [ "$QUELLE" = "$ZIEL" ] && fehler "QuLSI liegt schon in $ZIEL"
    echo "== QuLSI anhalten und nach $ZIEL kopieren"
    (cd "$QUELLE" && docker compose --profile box down) || true
    rsync -aHAX --info=progress2 "$QUELLE"/ "$ZIEL"/
    echo "Kopiert. Prüfen, dann starten mit:  cd $ZIEL && docker compose --profile box up -d"
    echo "Erst danach den alten unverschlüsselten Ordner löschen: sudo shred -u ... bzw. sudo rm -rf $QUELLE"
    echo "(Auf SSDs ist Überschreiben nicht sicher; am saubersten ist danach ein TRIM: sudo fstrim -av)"
    ;;

  status)
    if verschluesselt "$QUELLE"; then
      echo "verschlüsselt: $QUELLE liegt auf LUKS ($(findmnt -n -o SOURCE --target "$QUELLE"))"
    else
      echo "NICHT verschlüsselt: $QUELLE"; exit 1
    fi
    ;;

  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
