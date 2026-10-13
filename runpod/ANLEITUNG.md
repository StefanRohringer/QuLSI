# QuLSI auf RunPod testen

Auf RunPod läuft alles in einem einzigen Container, Docker Compose gibt es dort nicht. Deshalb startet `runpod/start.sh` alle Dienste direkt mit supervisord: Postgres, Qdrant, Bonsai (Fork mit CUDA), das kleine Modell, das Embedding-Modell, LiteLLM, Voice (Whisper, Qwen3-TTS mit Piper als Rückfall), Vision, den Egress-Proxy, die Connectoren, QuLSI Core mit der Web-App und ntfy. Alles Dauerhafte liegt auf dem Volume `/workspace`.

Nicht dabei sind Home Assistant, echte Kameras und Bluetooth, weil der Pod nicht bei dir zu Hause steht. Die Erkennung testest du mit hochgeladenen Fotos, Sprache über das Mikrofon in der Web-App.

## 1. Pod anlegen

| Einstellung | Wert |
|---|---|
| GPU | **RTX A4000 (16 GB, 448 GB/s)**: gleicher Speicher und ähnliche Bandbreite wie die RTX 5060 Ti der Box. Alternativ RTX A5000 oder RTX 4090 für mehr Tempo. |
| Template | **RunPod-PyTorch mit CUDA 12.8**, z. B. `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` (Ubuntu 24.04, `nvcc` liegt unter `/usr/local/cuda/bin/nvcc`; der Bonsai-Fork wird damit gebaut). |
| Container Disk | 30 GB |
| Speicher | **Volume Disk**, 60 GB, eingehängt unter `/workspace`. Ist `/workspace` Netzspeicher ohne Dateibesitzer (in manchen Regionen auch die Volume Disk), legt `start.sh` die Datenbank auf die Container-Disk und sichert sie alle 10 Minuten auf `/workspace` (siehe Abschnitt Anhalten). |
| HTTP-Ports | `8888` (JupyterLab), `8100` (QuLSI-App und API), `8090` (ntfy, vorerst nicht nötig). Optional `3000` für Open WebUI. |

Alle anderen Ports (Bonsai 8080, LiteLLM 4000, Qdrant 6333, Vision 8200, Voice 8300, Egress 3128, Connectoren 18001–18010) **nicht** freigeben. Sie lauschen nur im Pod.

## 2. Projekt hochladen

JupyterLab öffnen (Connect > Port 8888), `qulsi.tar.gz` per Drag & Drop nach `/workspace` hochladen. Dann im Terminal:

```bash
mkdir -p /workspace/qulsi && tar -xzf /workspace/qulsi.tar.gz -C /workspace/qulsi --no-same-owner
```

## 3. Starten

```bash
bash /workspace/qulsi/runpod/start.sh
```

Beim ersten Mal dauert das grob 30 Minuten: Pakete installieren, den Bonsai-Fork bauen, Qdrant und ntfy holen, Bonsai (7,2 GB), das kleine Modell, das Embedding-Modell, Whisper und die Piper-Stimme laden. Am Ende stehen die Adressen und der Schlüssel im Terminal. Die Einstellungen mit zufälligen Schlüsseln liegen in `/workspace/qulsi-data/env`. Die Python-Pakete installiert `start.sh` mit uv auf die Container-Disk (auf dem Netzspeicher von RunPod wäre das sehr langsam) und legt danach je Umgebung ein Archiv auf dem Volume ab. Ein alter Ordner `/workspace/venvs` aus früheren Versionen wird nicht mehr gebraucht und kann gelöscht werden.

Falls Hugging Face eine Anmeldung verlangt, vorher `export HF_TOKEN=hf_...` setzen.

Schalter beim Start:

- `QULSI_ENABLE=open-webui bash .../start.sh` startet zusätzlich Open WebUI auf Port 3000.
- `QULSI_DISABLE=vision,voice bash .../start.sh` lässt Dienste weg.
- Das kleine Modell abschalten: `MODEL_SMALL=aus` in der env-Datei, dann übernimmt Bonsai alles.

## 4. Prüfen

```bash
cd /workspace/qulsi
bash runpod/qulsi.sh status        # alle Dienste RUNNING?
bash runpod/qulsi.sh test          # Gesundheit, Tokens/s, ein Werkzeugaufruf
bash runpod/qulsi.sh pruefen       # Abnahme: echte Testsätze, Tabelle OK/FEHLER, räumt danach auf
bash runpod/qulsi.sh frage "Wann ist der nächste Feiertag?"
bash runpod/qulsi.sh daten zeigen  # was QuLSI über dich gespeichert hat (Gedächtnis, Termine, Aufgaben, Name)
bash runpod/qulsi.sh daten suchen "Kaffee"   # passende Einträge löschen (fragt nach)
bash runpod/qulsi.sh daten leeren  # alles Persönliche löschen, z. B. nach Tests (fragt nach "JA")
bash runpod/qulsi.sh logs core     # Logs eines Dienstes (llm, vision, egress, connector-calendar, ...)
bash runpod/qulsi.sh restart core
```

Bonsai braucht nach dem Start etwa eine Minute. Bis dahin antwortet QuLSI mit einem freundlichen Hinweis.

## 5. QuLSI-App

`https://<POD-ID>-8100.proxy.runpod.net` öffnen und den Schlüssel (`QULSI_API_KEY` aus der env-Datei) eingeben. Die App zeigt „Heute“ mit Terminen, Aufgaben, neuen Mails und offenen Freigaben, dazu den Chat mit Mikrofon-Knopf und die Timeline. Auf dem Handy lässt sie sich als App auf den Startbildschirm legen.

Die Schnittstelle ist OpenAI-kompatibel: Basis-URL `https://<POD-ID>-8100.proxy.runpod.net/v1`, Modell `qulsi` (oder `qulsi-gross`, `qulsi-stimme`), Schlüssel wie oben. Open WebUI nutzt sie automatisch, wenn du es mit `QULSI_ENABLE=open-webui` startest. Dort **sofort ein Konto anlegen**, das erste wird Admin.

## 6. Kalender, Mail, Dateien

In `/workspace/qulsi-data/env` eintragen und `bash runpod/start.sh` erneut ausführen:

- `CALENDAR_ICS_URLS=Privat=https://...basic.ics` (geheime iCal-Adresse, mehrere mit `;`)
- `IMAP_*`, `SMTP_*`, `MAIL_FROM` für Mail. Besser ein Test-Postfach, keine echten Zugangsdaten auf einem gemieteten Pod.
- Dateien zum Durchsuchen nach `/workspace/qulsi-data/files` legen, sie landen in Qdrant.

Der Egress-Proxy lässt jeden Connector nur zu den Zielen aus seinem `manifest.yaml` (`network.allowed`). Die Kalender-Adressen werden daraus automatisch übernommen.

## 7. Freigaben

Freigaben laufen nur in der QuLSI-App: Offene Freigaben stehen unter Heute und werden mit der Freigabe-PIN erlaubt oder abgelehnt. Die PIN legst du in der App unter Einstellungen fest; beim ersten Mal (oder wenn sie vergessen ist) mit dem Einrichtungscode, den `start.sh` am Ende zeigt oder `bash runpod/qulsi.sh pin` neu erzeugt (15 Minuten gültig, nur einmal). Test: „Schreib Anna eine Mail, dass ich später komme“. QuLSI legt einen Entwurf an, die Freigabe mit Vorschau erscheint in der App, erst nach „Freigeben“ wird gesendet. Benachrichtigungen aufs Handy und die Uhr kommen später über Web-Push.

## 8. Vision mit Fotos testen

Fotos per JupyterLab hochladen, dann:

```bash
cd /workspace/qulsi
# Schlüsselbund anlernen (3–5 Fotos aus verschiedenen Blickwinkeln)
scripts/anlernen.sh "Schlüsselbund" keys "Schlüssel" /workspace/fotos/schluessel1.jpg /workspace/fotos/schluessel2.jpg
# Ein Foto vom Küchentisch auswerten, als käme es von der Küchenkamera
curl -F bild=@/workspace/fotos/kuechentisch.jpg localhost:8200/analyse/kueche
bash runpod/qulsi.sh frage "Wo sind meine Schlüssel?"
```

Gespeichert werden nur Fakten (Objekt, Ort, Zeit) und ein kleiner Ausschnitt im Object Storage, kein Video. Die Zonen der Küchenkamera stehen in `config/cameras.yaml`. Beim ersten Bild lädt Vision YOLO-World und SigLIP (etwa 1 GB nach `/workspace/models`).

## 9. Eigene Stimme

`start.sh` richtet Qwen3-TTS (0.6B) in einer eigenen Umgebung ein und lädt die zwei Modelle (eingebaute Sprecher und das Modell zum Stimmklonen, zusammen etwa 4 GB). Danach zeigt die App unter Einstellungen > Stimme > Eigene Stimme den Aufnahme-Knopf. Die Aufnahme (5 bis 15 Sekunden) gilt erst, wenn du sie unter Freigaben mit der PIN bestätigst.

Qwen3-TTS lädt gleich nach dem Start des Dienstes (etwa 20 bis 30 Sekunden) und bleibt im Grafikspeicher (etwa 2 bis 3 GB zusätzlich zu Bonsai). Bis es geladen ist, spricht Piper. Hochwertiger klingt Qwen3-TTS 1.7B: `setup.sh` lädt es einmal dazu (etwa 8 GB), in der App heißen die Stimmen „… (hochwertig)“. Es rechnet etwa doppelt so lange wie 0.6B; von Qwen3 bleiben höchstens zwei Modelle zugleich im Grafikspeicher. Ohne 1.7B: `TTS_QWEN_17B=aus`. Zum Vergleich gibt es Chatterbox (natürlicheres Deutsch, MIT-Lizenz): `start.sh` richtet es beim nächsten Start ein (eigene Umgebung, etwa 3 GB Modell, ein paar Minuten). In der App unter Einstellungen > Stimme heißen die Stimmen „Chatterbox“ und „Eigene Stimme mit Chatterbox“. Es lädt erst, wenn du es auswählst, und braucht dann etwa 3 bis 4 GB Grafikspeicher zusätzlich. Logs: `bash runpod/qulsi.sh logs tts-chatterbox`. Ohne Chatterbox: `QULSI_DISABLE=tts-chatterbox` in die env-Datei. Wird der Grafikspeicher knapp: `TTS_LEERLAUF_SEKUNDEN=1800` in `/workspace/qulsi-data/env`, dann geht es nach 30 Minuten ohne Nutzung wieder raus. Wie schnell es ist, steht im Log je Satz („… s Audio in … s“). Schneller, aber etwas weniger ähnlich: `TTS_KLON=merkmale`. Der Code-Predictor läuft als CUDA-Graph (Log: „als CUDA-Graph aufgenommen“); macht das Ärger: `TTS_CUDA_GRAPH=aus`. Jede Log-Zeile zeigt auch „ms je Rahmen, davon Code-Predictor“; unter 83 ms je Rahmen ist schneller als Abspielen. Klappt etwas nicht (Modell fehlt, Grafikspeicher voll), spricht Piper. Nur Piper: `TTS_PROVIDER=piper` in die env-Datei und `start.sh` neu. Logs: `bash runpod/qulsi.sh logs tts`.

## Nach einem Neustart des Pods

Die Container-Disk wird beim Stoppen geleert, das Volume bleibt. Einfach wieder `bash /workspace/qulsi/runpod/start.sh` ausführen. Postgres wird dann in etwa einer Minute neu installiert, die Python-Umgebungen werden aus ihren Archiven in `/workspace/qulsi-venvs` ausgepackt (Sekunden statt einer halben Stunde), alles andere liegt schon auf dem Volume. Nach Änderungen an der Konfiguration genügt derselbe Befehl.

Pod stoppen, wenn du nicht testest. Das Volume kostet weiter, aber deutlich weniger.

## Sicherheit auf RunPod

RunPod macht jeden freigegebenen Port über `https://<POD-ID>-<PORT>.proxy.runpod.net` öffentlich erreichbar. Deshalb:

- QuLSI-App und API brauchen den `QULSI_API_KEY`, die Bestätigungsseite das Einmal-Token aus der Benachrichtigung.
- Freigaben in der App erlauben nur mit Freigabe-PIN (Ablehnen geht immer). Erste oder vergessene PIN nur mit Einrichtungscode von der Box: `bash runpod/qulsi.sh pin`.
- Passwörter und Konten liegen im Tresor (`$WS/qulsi-data/tresor`, age-verschlüsselt). Beim ersten Start nach dem Update übernimmt er die Werte aus der env-Datei und die Konto-Dateien (vorher einmal `bash runpod/setup.sh`, damit pyrage installiert ist). Der Schlüssel liegt auf demselben Volume: nur Test-Konten verbinden. Setzen geht in der App (Einstellungen > Konten, mit Freigabe) oder mit `bash runpod/qulsi.sh geheimnis NAME`.
- Den App-Schlüssel zeigt `bash runpod/qulsi.sh schluessel`. Bitte nicht in Chats oder Tickets kopieren; Logs maskieren ihn.
- ntfy: ohne Benutzer und Token kein Zugriff.
- Alles andere lauscht nur auf `127.0.0.1`.
- Die Sicherheitszonen 1–3 sind auf RunPod **nicht erzwungen**: alles läuft in einem Container, ein Prozess im Pod könnte den Egress-Proxy umgehen. Die echte Trennung mit getrennten Netzen macht `docker-compose.yml` auf der Box (plus `scripts/firewall.sh`).
- Ebenso gilt die Container-Härtung (kein root, schreibgeschützt, ohne Capabilities) nur mit Docker auf der Box. Die Policy Engine mit Freigaben, Rate-Limits und Protokoll gilt dagegen auch auf RunPod.

## Was getestet ist

Die Startskripte (`setup.sh`, `start.sh`, `run.sh`, `qulsi.sh`) liefen in der Testumgebung ohne GPU mit einem Testmodell statt Bonsai (`QULSI_FAKE_LLM=1`): alle 15 Dienste unter supervisord, Model Router, Gedächtnis mit Qdrant, Vision mit Foto, Kalender über den Egress-Proxy, Aufgaben, Sprache. Nicht getestet sind der CUDA-Build des Forks im Pod, die echten Modelle auf der GPU (Bonsai, Qwen, Whisper, YOLO-World, SigLIP), die Hugging-Face-Downloads, ein echter Mailserver und Open WebUI. Wenn etwas hakt, schick mir die Ausgabe von `bash runpod/qulsi.sh logs <dienst>`.

## Anhalten und Datenbank-Sicherung

Den Pod immer so anhalten:

```bash
bash /workspace/qulsi/runpod/qulsi.sh stop
```

Meldet `start.sh` beim Start „Netzspeicher ohne Dateibesitzer“, liegt die Datenbank auf der Container-Disk, die RunPod beim Anhalten löscht. Dann sichert QuLSI sie alle 10 Minuten nach `/workspace/qulsi-data/pg-sicherung` und `qulsi.sh stop` macht vorher eine letzte Sicherung. Beim nächsten Start wird sie automatisch wiederhergestellt. Wird der Pod ohne `qulsi.sh stop` beendet, fehlen die Änderungen seit der letzten Sicherung (höchstens 10 Minuten). Zwischendurch von Hand sichern: `bash /workspace/qulsi/runpod/qulsi.sh sichern`.
