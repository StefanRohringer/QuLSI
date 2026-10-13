# QuLSI – Quantized Local Superintelligence

QuLSI ist die Software einer KI-Box für zu Hause: ein persönlicher Assistent, der zu Hause läuft. Er hat ein Gedächtnis (Personen, Termine, Aufgaben, Projekte, Erinnerungen), sieht über Kameras, wo Dinge liegen, versteht Sprache und arbeitet mit Werkzeugen, deren Rechte er nicht selbst ändern kann. Kritische Aktionen wie „Mail senden“ oder „Tür öffnen“ laufen nur nach einem Tippen auf dem Handy.

Das Sprachmodell ist austauschbar: **Bonsai ist nicht QuLSI.** Bonsai-27B ist das Standardmodell, aber QuLSI kennt nur die Namen aus `config/litellm.yaml`. Ein anderes Modell oder optional ein Cloud-Modell einzutragen ändert nichts am Rest.

Zuerst ausprobieren lässt sich alles auf einem gemieteten GPU-Server: [runpod/ANLEITUNG.md](runpod/ANLEITUNG.md).

## Beispiel

![QuLSI-App im Gespräch, Freigabe und Gedächtnis](docs/demo.gif)

Die Aufnahme zeigt die echte App und den echten Core, aber **ohne Grafikkarte**: statt Bonsai antwortet das Testmodell aus `runtime/fake-llm` mit festen Regeln. Ablauf, Werkzeuge, Freigaben mit PIN und Gedächtnis sind echt, die Formulierungen der Antworten nicht. So lässt sich alles ausprobieren, bevor eine GPU da ist: [Tests ohne GPU](#tests-ohne-gpu). Als Video: [Laptop-Ansicht](docs/demo-laptop.mp4) und [Handy-Ansicht](docs/demo-handy.mp4), erzeugt mit `docs/demo-aufnahme-laptop.py` und `docs/demo-aufnahme.py`.

## Aufbau

```
 USER EXPERIENCE   QuLSI-App (Web/Handy, http://qulsi.local)   Stimme   Open WebUI   Home Assistant
                                          │
 QuLSI CORE        Agent Runtime ── Model Router ── Policy Engine ── Event Bus
 (core/)                 │                                │               │
                         │                         Connector Hub          │
 MEMORY            PostgreSQL (Fakten, Timeline)  Qdrant (Vektoren)  Object Storage (Bilder, PDFs)
                   + Wissensgraph (Tabelle beziehungen)
 CONNECTORS        home_assistant  calendar  mail  files  web  vision      ─► Egress-Proxy ─► Internet
                   google  microsoft  nas  browser
 (connectors/)     je ein MCP-Server mit manifest.yaml                         (nur Ziele aus dem Manifest)
 MODELS            LiteLLM: qulsi-klein, bonsai-fast, bonsai-think, bonsai-vision, embed
 AI RUNTIME        llama-server (PrismML-Fork für Bonsai), Vision (OWLv2/YOLO-World + SigLIP), Voice (Whisper, Piper)
 HARDWARE          RTX 5060 Ti 16 GB, Kameras (PoE), Mikrofon-Array, Bluetooth-Anhänger
```

| Ordner | Inhalt |
|---|---|
| `core/qulsi_core/` | `agent.py` Agent Runtime, `router.py` Model Router, `policy.py` Policy Engine, `hub.py` Connector Hub, `memory/` (Postgres, Qdrant, Object Storage, Graph), `tools.py` Kern-Werkzeuge, `main.py` API, `web/` QuLSI-App |
| `connectors/` | ein Ordner je Connector mit `manifest.yaml` und `server.py` (MCP), `_lib/` gemeinsame Helfer |
| `egress/` | Egress-Proxy: einziger Weg ins Internet |
| `vision/` | Kameras, Erkennung, Fingerabdrücke (Qdrant), Ausschnitte (Object Storage), Bluetooth |
| `voice/` | Spracherkennung und Sprachausgabe mit austauschbaren Anbietern |
| `runtime/` | `llm/` llama-server für Bonsai, kleines Modell und Embeddings; `fake-llm/` Testmodell |
| `config/` | `router.yaml`, `policy.yaml`, `litellm.yaml`, `cameras.yaml`, `agent_prompt.md` |
| `db/init/` | Datenbankschema und eigene Rollen je Dienst |
| `runpod/` | Start ohne Docker auf einem RunPod-GPU-Pod |

## Was getestet ist und was nicht

| Teil | Stand |
|---|---|
| Core: Router, Policy Engine mit Manifesten, Connector Hub, Freigaben, Event Bus, Offline-Modus, Web-App, API | getestet: 319 Unit-Tests, 57 Ende-zu-Ende-Tests im Docker-Teststack |
| Memory: Postgres, Qdrant (Erinnerungen, Dokumente, Objekt-Fingerabdrücke), Object Storage, Graph | getestet |
| Gedächtnis: Organisator (Termine, Mails, Fotos mit EXIF/GPS, Dateien), Suche nach Person/Zeit/Ort, Gewohnheiten, Memchip-Export/-Import | getestet (Unit-Tests mit echtem Postgres); echte Foto-Bibliothek und HEIC ungetestet |
| Connectoren: Kalender (ICS mit Serienterminen, über den Egress-Proxy), Mail (Entwurf, Freigabe mit Vorschau, SMTP), Dateien (Index, Suche), Home Assistant, Kameras | getestet mit Ersatzdiensten; IMAP gegen einen echten Server nicht |
| Connectoren Google (Kalender, Gmail, Drive), Microsoft (Outlook, Kalender, OneDrive), NAS (SMB), Browser | getestet mit nachgebauten Google- und Graph-Antworten, echtem SMB-Server über den Egress-Proxy und echtem Postgres; mit echten Google- und Microsoft-Konten noch nicht |
| Sicherheitszonen: Egress lässt nur Manifest-Ziele durch, Core und Vision haben kein Internet | getestet im Docker-Teststack |
| Härtung: eigene Container ohne root, read-only, ohne Capabilities, Seccomp aktiv | getestet im Docker-Teststack; Postgres, Qdrant, ntfy, SearXNG, Mosquitto mit eingeschränkten Capabilities laufen dort mit |
| Vision-Pipeline, Bluetooth-Räume, Privacy-Modus | getestet mit Testbildern, festen Erkennungen und Farbhistogramm statt OWLv2/SigLIP |
| Voice: Ablauf App → STT → QuLSI → TTS | getestet mit Test-Anbietern; Whisper und Piper selbst nicht |
| RunPod-Start (supervisord, alle 15 Dienste) | getestet ohne GPU mit Testmodell |
| Bonsai-27B, Qwen3-1.7B, Embedding-Modell, Qwen3-TTS, Chatterbox auf einem gemieteten GPU-Pod (RunPod, 24 GB) | im täglichen Gebrauch, mit der App vom Handy aus |
| Bild-Erkennung (OWLv2 bzw. YOLO-World, SigLIP), Bonsai mit Bildern (mmproj), Whisper, Piper auf einer GPU | nicht geprüft; Fragen zu Bildern meldet die App als „nicht verfügbar“, solange kein Bildteil geladen ist |
| Start auf einer eigenen Box mit Docker (Abschnitt „Erster Start auf der Box“) | nur im Teststack geprüft, noch auf keiner echten Box |

**Kurz und ehrlich:** Gespräch, Gedächtnis, Termine, Aufgaben, Freigaben mit PIN, Hintergrund-Agents, Schutz vor versteckten Anweisungen, eigene Stimme und die App funktionieren auf einem GPU-Pod. Kameras, Sprache ins Mikrofon, echte Google- und Microsoft-Konten, Home Assistant und der Betrieb auf eigener Hardware sind gebaut und mit Ersatzdiensten getestet, aber von niemandem im Alltag bewiesen. Wer das ausprobiert, bitte Rückmeldung als Issue.

## Model Router

`config/router.yaml` legt fest, wer antwortet:

| Anfrage | Weg |
|---|---|
| „Wie spät ist es?“, „Welches Datum haben wir?“ | QuLSI selbst, ohne Modell |
| kurze Befehle („Schreib Max eine Mail …“, „Licht aus“, „Neue Aufgabe …“) | kleines Modell `qulsi-klein` mit Werkzeugen; scheitert es, übernimmt Bonsai |
| alles andere | `bonsai-fast` (Bonsai ohne Denkmodus) |
| Analyse, Planung, lange Texte | `bonsai-think` (mit Denkmodus), per Stimme nie |
| Nachricht mit Bild | `bonsai-vision` |

Das Wake-Word („Hey QuLSI“) läuft auf dem Sprachsatelliten bzw. in Home Assistant (openWakeWord), nicht im Core.

## Policy Engine und Connector-Manifeste

Jeder Connector beschreibt in `manifest.yaml`, was er kann, wohin er darf und wie riskant jede Fähigkeit ist:

```yaml
name: mail
capabilities: [email.search, email.read, email.draft, email.send]
network:
  allowed: ["${IMAP_HOST}:993", "${SMTP_HOST}:${SMTP_PORT}"]
permissions: {search: low, read: low, draft: medium, send: high}
untrusted: [email.search, email.read]   # Inhalt ist fremder Text
offline: [email.draft]                  # geht ohne Internet
vorschau: {email.send: email_draft_vorschau}   # was das Handy bei der Freigabe zeigt
```

| Stufe | Regel |
|---|---|
| low | frei, wird protokolliert |
| medium | frei bei direkter Anfrage; steht fremder Text im Gespräch, nur mit Freigabe |
| high | immer Freigabe in der Handy-App, nie per Stimme oder Chat; bei fremdem Text im Gespräch gesperrt |
| deny | Werkzeug ist für das Modell unsichtbar (nur als Override in `config/policy.yaml`) |

Feste Regeln: Was nicht im Manifest steht, übernimmt der Hub nicht. Fähigkeiten ohne Stufe gelten als `high`. Nach Mail, Web oder Dateien gilt das Gespräch als belastet, auch in späteren Nachrichten. Freigaben laufen über einen Einmal-Link (5 Minuten, nur der Hash liegt in der Datenbank), ausgeführt werden genau die gespeicherten Argumente. Jeder Aufruf steht in `audit_log`, jede Aktion im Event Bus. Rate-Limits je Stufe und Werkzeug (Standard pro Stunde: low 600, medium 60, high 10) stehen in `config/policy.yaml > limits`; darüber lehnt die Policy Engine ab.

Beispiel „Schick Max eine Nachricht“: `persons.search` holt die Adresse, `email.draft` legt den Entwurf ab, `email.send` erzeugt die Freigabe. Auf dem Handy steht der Entwurf mit Empfänger und Text, erst nach „Freigeben“ geht die Mail raus.

Eigene Overrides in `config/policy.yaml`, z. B. `home.set_state: high` oder `browser.search: deny`.

## Gedächtnis

Vier Ebenen in Postgres (Text, Zeit, Personen, Ort, Projekt), Vektoren in Qdrant, Fotos und Dateien im Object Storage:

| Ebene | Was | Woher |
|---|---|---|
| episodisch | Timeline: was ist passiert | Termine, Mails, Fotos, Dateien, Gespräche, Tagesrückblick |
| semantisch | Wissen über Personen, Projekte, Dinge | „Merk dir …“, Personen- und Projektkarten |
| praeferenz | Vorlieben und Gewohnheiten | „Merk dir …“, erkannte Gewohnheiten |
| projekt | Projekte und Aufgaben | Projekte, Unterprojekte, Aufgaben |

**Organisator** (`memory/organisator.py`, alle `ORGANISATOR_MINUTEN`): macht aus vergangenen Terminen, neuen Mails (nur Absender, Betreff, Auszug), Dateien und Fotos Einträge in der Timeline. Dabei erkennt er bekannte Personen, Orte und Projekte im Text und verknüpft sie (auch im Wissensgraph: wer war wo, wer traf wen). Wer mindestens dreimal schreibt, wird als Person angelegt. Wiederkehrende Termine und Aktionen werden zu Gewohnheiten („Training meist dienstags um 19 Uhr“). Jeden Tag entsteht ein kurzer Rückblick. Fotos aus `FOTOS_DIR` bekommen Aufnahmezeit und Ort aus EXIF/GPS, das Album ist der Ordnername, im Object Storage liegt nur eine Vorschau ohne EXIF.

**Suche nach Person, Zeit, Ort**: `memory.find` versteht die Frage selbst, ohne Sprachmodell: „Was war letzte Woche mit Max in München?“, „Fotos vom Gardasee im Juli 2025“, „Mails von Anna seit März“, „vorgestern“, „am 3.10.“, „an Weihnachten“. `memory.timeline` zeigt einen Tag oder Zeitraum, auch nur für eine Person oder einen Ort. Orte mit Koordinaten legt man mit „Speicher Zuhause bei 48.13, 11.57“ (`places.save`) an, Fotos dort werden dann automatisch zugeordnet. App und Web-UI: `GET /api/gedaechtnis/finden?frage=…`, `/api/gedaechtnis/timeline`, `/api/gedaechtnis/status`.

**Fremder Text**: Treffer, die aus Mails, Dateien oder Kalendereinladungen stammen, markieren das Gespräch als belastet (wie `email.read`), damit ein präparierter Betreff keine Aktion auslösen kann. Eigene Notizen und Fotos belasten nicht. `memory.forget` löscht eine Erinnerung (Stufe high, nur per Handy-Freigabe).

**Memchip** (austauschbarer Speicher, `MEMCHIP_DIR`): `memchip.export` schreibt das ganze Gedächtnis als eine Datei auf einen USB-Stick, eine SSD oder SD-Karte, verschlüsselt mit `MEMCHIP_PASSWORT` (AES-256-GCM). Ohne Passwort verweigert QuLSI den Export; ein unverschlüsseltes tar gibt es nur mit `MEMCHIP_UNVERSCHLUESSELT=1`. Den Zustand zeigt `GET /api/gedaechtnis/status` unter `memchip.verschluesselung` (`an`, `unverschluesselt_erlaubt` oder `fehlt`). `memchip.import` lädt sie auf derselben oder einer neuen Box: „zusammenfuehren“ ergänzt, „ersetzen“ sichert erst das aktuelle Gedächtnis auf den Chip und tauscht es dann aus (anderes Profil). Vektoren werden beim Import neu berechnet, ein Chip funktioniert also auch nach einem Wechsel des Embedding-Modells. `MEMCHIP_AUTO_STUNDEN=24` sichert täglich automatisch und behält die letzten fünf.

## Hintergrund-Agents (proaktiv)

QuLSI arbeitet auch, wenn niemand fragt. `config/agenten.yaml` legt fest, welcher Agent wann läuft (Zeitplan wie „täglich 07:00“ oder „alle 15 min“, Ereignisse aus dem Event Bus) und welche Werkzeuge er bekommt. Code: `core/qulsi_core/proaktiv/`.

| Agent | Wann | Was |
|---|---|---|
| Morgenbriefing | täglich 07:00 | Termine, Aufgaben, wichtige Mails in sechs Zeilen aufs Handy |
| Gleich ein Termin | alle 5 min | Termin in 30 Minuten, dazu was QuLSI über die Personen weiß (Beziehung, Notiz, offene Aufgaben) |
| Fällige Aufgaben | werktags 08:00 | überfällige und heute fällige Aufgaben |
| Neue Mails | alle 15 min | jede neue Mail einmal ans Modell; Wichtiges als Hinweis, Aufgaben und Termine als Vorschlag zum Freigeben |
| Tagesrückblick | täglich 21:30 | fasst den Tag aus der Timeline zusammen und speichert ihn als episodische Erinnerung |
| Internet | Ereignis offline/online | meldet, wenn QuLSI offline weiterarbeitet |

Regeln, die kein Agent umgehen kann: Er sieht nur die Werkzeuge aus seiner Liste. Alles läuft durch die Policy Engine mit Kanal `automatik`: lesen (low) ist frei, ändern (medium) nur, wenn es unter `selbststaendig` steht, sonst kommt eine Freigabe aufs Handy; high (Mail senden, Tür) immer nur mit Freigabe. Hat der Agent Mails gelesen, gilt sein Lauf als belastet wie ein Gespräch: medium nur mit Freigabe, high gesperrt. Nachts (Ruhezeit 22–7 Uhr) laufen nur Agents mit `nachts: true`, und höchstens 6 Hinweise pro Stunde gehen aufs Handy; alles andere steht trotzdem in der Timeline.

Eigener Agent: in `config/agenten.yaml` einen Eintrag mit `typ: llm`, `zeitplan`, `werkzeuge` und `auftrag` anlegen und `docker compose restart core`. Übersicht und Steuerung: `GET /api/agenten`, `POST /api/agenten/<name>/starten`, `POST /api/agenten/<name>/aktiv` mit `{"aktiv": false}`. Jeder Lauf steht in der Tabelle `agent_laeufe`. Alle aus: `QULSI_AGENTEN=aus`.

## Sicherheitszonen

| Zone | Dienste | Netz |
|---|---|---|
| 1 KI | Bonsai, kleines Modell, Embeddings, Vision, Voice, LiteLLM | `zone-ai`, intern, kein Internet |
| 2 Agent | Core, Postgres, Qdrant, Kern-Werkzeuge | `zone-agent`, intern, kein Internet |
| 3 Connectoren | Home Assistant, Kalender, Mail, Dateien, Web, Kameras, Google, Microsoft, NAS, Browser | `zone-connectors`, intern; raus nur über `egress` |

Der Egress-Proxy erkennt jeden Connector an seinem Containernamen und lässt nur die Ziele aus dessen Manifest durch. Alles andere wird abgelehnt und geloggt (`docker compose logs egress | grep VERWEIGERT`). Jeder Connector hat eine eigene Datenbank-Rolle und sieht nur seine Tabellen (`db/init/02_rollen.sh`). Modelle und Agent sprechen nur intern mit den Connectoren (MCP über das interne Netz, Ereignisse über den Event Bus in Postgres); eine Verbindung nach draußen gibt es für sie nicht.

Vision lädt seine Modelle einmalig mit `docker compose --profile setup run --rm vision-modelle` (der einzige Container mit Internet in Zone 1) und läuft danach offline. Kameras im Heimnetz kommen mit `docker-compose.kameras.yml` dazu; `scripts/firewall.sh` lässt Vision dann nur zu den Kamera-Adressen und verhindert, dass Web-App und ntfy selbst Verbindungen nach draußen aufbauen.

### Connector Hub absichern

Der Connector Hub ist der einzige Teil mit Internet, deshalb gilt dort zusätzlich:

| Maßnahme | Umsetzung |
|---|---|
| Least Privilege im Manifest | `capabilities`, `network.allowed_domains` + `allowed_ports` (oder `allowed: host:port`), `permissions` je Fähigkeit mit `low/medium/high` oder `false` (verboten), `secrets`, `konto`, `limits`. Was nicht drinsteht, ist blockiert |
| Eigener Container je Connector | ja, mit Speicher-, CPU- und Prozess-Limit (512 MB, 1 CPU, 256 Prozesse; je Dienst änderbar) |
| Netzwerk | Connectoren hängen nur an internen Netzen; raus geht es nur über den Egress-Proxy |
| DNS | In den internen Netzen lösen Connectoren keine externen Namen auf (im Test geprüft). Nur der Egress-Proxy löst auf, und nur erlaubte Domains |
| Proxy | Prüft Absender, Domain und Port, schreibt jede Verbindung als JSON-Zeile nach `data/egress/egress.jsonl` (Ziel, Bytes, Dauer) |
| Host-Firewall | `scripts/firewall.sh`: der Egress-Container darf nur auf die Ports aus den Manifesten, alles andere verwirft der Host |
| Secrets | Tresor (`tresor/`, age-verschlüsselt). Ein Connector bekommt nur die Geheimnisse aus seinem Manifest. Bei OAuth bleiben Refresh-Token und Client-Secret im Tresor, der Connector bekommt Zugriffstokens mit kurzer Laufzeit (bei Google und Microsoft 1 Stunde). Jeder Zugriff steht in `data/tresor/zugriffe.jsonl` |
| Rate-Limits | je Connector Verbindungen und MB pro Minute (Egress), je Werkzeug Aufrufe pro Stunde (Policy Engine) |
| Automatisches Stoppen | Zehn abgelehnte Verbindungen in 10 Minuten (unerlaubte Domains, Limit überschritten): Quarantäne. Der Proxy lässt den Connector nicht mehr raus, der Core blendet seine Werkzeuge aus und schickt eine Nachricht aufs Handy. Freigeben in der App unter „Connectoren“ |
| An, nur Lesen, aus | je Connector in der App oder in `config/policy.yaml > connectoren`. „Nur Lesen“ lässt nur Werkzeuge der Stufe `low` zu |
| Integrität | `scripts/signieren.py` signiert alle Connector-Ordner (Ed25519). Core und Egress laden einen veränderten Connector nicht und lassen ihn nicht ins Internet |
| Domains prüfen | Der Egress-Proxy prüft täglich, ob die erlaubten Domains noch auflösbar sind; die App zeigt Fehler beim Connector an |
| Freigabe durch dich | `high` (senden, löschen, Türen) immer per Handy-Freigabe |

Grenzen: Der Tresor-Schlüssel liegt auf der Box (`data/tresor/schluessel.txt`). Die Verschlüsselung schützt Backups und kopierte Platten, nicht ein laufendes, übernommenes System. Die Zugriffstokens laufen nach der Frist des Anbieters ab; wie weit ihre Rechte gehen, legt die Anmeldung fest (Scopes), nicht der Tresor. Ein ausgebrochener Container wird nicht von selbst beendet, aber vom Proxy und der Host-Firewall eingesperrt. Den Docker-Socket bekommt bewusst kein Dienst, auch nicht zum Stoppen. Die internen Datenbank-Passwörter der Connector-Rollen stehen weiter in der `.env`.

### Daten verschlüsseln

Auf der Box liegt alles, was QuLSI speichert, in einem mit LUKS2 verschlüsselten Ordner (`/srv/qulsi`, AES-256): Datenbank, Qdrant, Fotos und Bildausschnitte im Object Storage, Tresor, `.env` mit den Datenbank-Passwörtern und die Konfiguration. `scripts/verschluesseln.sh einrichten` legt das an, auf einer eigenen Partition oder, ohne freie Partition, in einer Containerdatei. Eine bestehende Installation zieht `scripts/verschluesseln.sh umziehen` um, `status` zeigt, ob QuLSI verschlüsselt liegt.

| Was | Wie | Schlüssel |
|---|---|---|
| Alle Daten auf der Box | LUKS2 (AES-256-XTS, Argon2id) | TPM-Chip der Box entsperrt beim Start, nur auf dieser Box; dazu deine Wiederherstellungs-Passphrase. Ohne TPM fragt die Box beim Start nach der Passphrase. |
| Passwörter und Tokens der Connectoren | zusätzlich age im Tresor | `data/tresor/schluessel.txt`, selbst auf dem LUKS-Laufwerk |
| Gedächtnis-Export (Memchip) | AES-256-GCM, Schlüssel per scrypt aus `MEMCHIP_PASSWORT` | dein Passwort |
| Backups | restic (AES-256) | Passwort in `scripts/restic.env`; zusätzlich getrennt aufheben |
| Handy und Box | WireGuard über Tailscale, HTTPS mit `tailscale serve` | Tailscale |

Zwischen den Containern auf der Box gibt es kein TLS: der Verkehr verlässt den Rechner nicht und läuft nur über interne Docker-Netze. Im WLAN ist `http://qulsi.local` dagegen unverschlüsselt. Wer das nicht will, setzt `BIND_IP=127.0.0.1` in `.env` und erreicht QuLSI nur noch über Tailscale (auch zu Hause).

Grenzen: Solange die Box läuft und entsperrt ist, sind die Daten für die laufenden Dienste lesbar; die Verschlüsselung schützt bei Diebstahl der Box oder der SSD. Auf RunPod verschlüsselt QuLSI nichts selbst, weil Container dort kein LUKS dürfen: dort nur mit Testdaten arbeiten.

### Härtung der Container

| Maßnahme | Wo |
|---|---|
| Kein root: eigene Dienste laufen als Nutzer 10001 | Core, Connectoren, Egress, Vision, Voice, Modell-Server |
| Schreibgeschütztes Dateisystem, nur `/tmp` und die Daten-Ordner beschreibbar | alle eigenen Dienste und LiteLLM |
| `cap_drop: ALL`, `no-new-privileges` | alle eigenen Dienste; fremde Images behalten nur die Capabilities für den Start (Postgres, SearXNG, Mosquitto) |
| Seccomp | Dockers Standardprofil, aktiv für alle Container (im Test geprüft) |
| User-Namespaces | optional für den ganzen Docker-Dienst: `"userns-remap": "default"` in `/etc/docker/daemon.json`. Nicht voreingestellt, weil sich dann die Besitzer der Daten-Ordner verschieben und das NVIDIA-Toolkit extra eingerichtet werden muss |

## Offline-Modus

Ohne Internet laufen Chat, Stimme, lokale Dateien, Erinnerungen, Vision, Kalender (aus dem lokalen Cache), Aufgaben und Home Assistant weiter. Websuche, Mail abrufen und Kalender-Sync fallen weg, QuLSI bietet sie dem Modell dann nicht an. Erkennung automatisch (`QULSI_OFFLINE=auto`), oder fest mit `true`/`false`.

## QuLSI-App

`http://qulsi.local` (Port `QULSI_PORT`, Hostname der Box auf `qulsi` setzen) zeigt „Was möchtest du?“ mit Mikrofon-Knopf, darunter **Heute**: Termine, Aufgaben, neue Mails, offene Freigaben und die Timeline, die sich über den Event Bus live aktualisiert. Auf dem Handy über „Zum Startbildschirm hinzufügen“ wie eine App nutzen. Für das Mikrofon braucht der Browser HTTPS, z. B. mit `tailscale serve`.

Weitere Wege: `/v1/chat/completions` (OpenAI-kompatibel, Modelle `qulsi`, `qulsi-stimme`, `qulsi-gross`) für Open WebUI (`--profile webui`) und Home Assistant.

## Erster Start auf der Box

Voraussetzungen: Ubuntu Server 24.04, NVIDIA-Treiber ab 570, Docker mit Compose und NVIDIA Container Toolkit, etwa 30 GB frei.

```bash
sudo apt install cryptsetup rsync
git clone <dieses Repo> ~/qulsi && cd ~/qulsi
sudo scripts/verschluesseln.sh einrichten /dev/nvme0n1p3   # verschlüsselter Ordner /srv/qulsi (oder: einrichten --datei 200G)
sudo scripts/verschluesseln.sh umziehen && cd /srv/qulsi && rm -rf ~/qulsi
scripts/erststart.sh                    # .env mit eigenen, zufälligen Schlüsseln (jede Box neu); Kalender, Kameras danach in .env
sudo install -d -o 10001 -g 10001 data/objects data/memchip data/egress models config/stimme   # Container laufen als Nutzer 10001
sudo install -d -m 700 -o 10001 -g 10001 data/tresor
docker compose build tresor && docker compose run --rm tresor setzen HA_TOKEN   # Passwörter nur in den Tresor
python3 scripts/signieren.py           # Connectoren signieren (nach jedem Update erneut)
scripts/download-models.sh             # Bonsai, kleines Modell, Embeddings, Whisper, Piper (~12 GB)
docker compose --profile box build
docker compose --profile setup run --rm tts-modell       # Sprachausgabe, Standard Qwen3-TTS 0.6B (~2 GB)
docker compose --profile setup run --rm vision-modelle   # OWLv2, SigLIP, optional YOLO-World (~1-2 GB)
docker compose --profile box up -d
sudo scripts/firewall.sh               # Host-Firewall: Egress nur Manifest-Ports, Web-App und ntfy ohne Weg nach draußen
docker compose logs -f core            # "Werkzeuge geladen: ..."
```

Danach in dieser Reihenfolge prüfen: Web-App öffnen und „Wie spät ist es?“ (Router), „Was habe ich heute vor?“ (Kalender), „Merk dir …“ und „Was weißt du über …“ (Gedächtnis), Kamera und Anlernen, zuletzt Mail und Home Assistant.

### Mitteilungen aufs Handy und die Uhr (Web Push)

Termine ("Gleich ein Termin"), Hinweise der Hintergrund-Agents und offene Freigaben kommen als normale Mitteilung aufs Handy, ohne ntfy. In der App unter Einstellungen › Benachrichtigungen „Benachrichtigungen erlauben“ tippen; danach kommt eine Testnachricht. Eine gekoppelte Apple Watch oder Wear-OS-Uhr zeigt die Mitteilungen des Handys von selbst.

- iPhone: ab iOS 16.4 und nur, wenn QuLSI auf dem Home-Bildschirm liegt (Safari › Teilen › Zum Home-Bildschirm) und von dort geöffnet wird.
- Die App braucht eine **https-Adresse**. RunPod hat sie schon (`https://<POD-ID>-8100.proxy.runpod.net`). Auf der Box am einfachsten mit Tailscale: `tailscale serve --bg 8100` gibt `https://<box>.<tailnet>.ts.net`, nur für deine Geräte erreichbar. Über `http://192.168…` gehen Mitteilungen nicht (Browser-Regel).
- Der Sperrbildschirm zeigt standardmäßig nur den Titel. Inhalte pro Gerät einschalten geht nur mit der Freigabe-PIN. Freigeben selbst geht weiter nur in der App mit PIN.
- Ruhezeit (22–7) und `max_hinweise_pro_stunde` aus `config/agenten.yaml` gelten, dazu höchstens 30 Mitteilungen pro Stunde insgesamt. Ein neues Gerät wird allen anderen gemeldet.
- Gesendet wird nur an Apple, Google, Mozilla und Microsoft, auf der Box über den Egress-Proxy (Eintrag `webpush`). Der VAPID-Schlüssel entsteht beim ersten Start in Postgres (`system_state`), Abos stehen in `push_abos`. Code: `core/qulsi_core/push.py`, `web/sw.js`.
- Prüfen: `bash runpod/qulsi.sh push-test` (RunPod) oder `curl -X POST localhost:8100/api/push/test -H "Authorization: Bearer $QULSI_API_KEY"`.

### Handy-Freigaben (ntfy)

```bash
scripts/ntfy-einrichten.sh     # Benutzer qulsi, Token für den Core, zufälliges Thema; zeigt Passwort und Thema
```

ntfy-App installieren, den Server (`NTFY_PUBLIC_URL`) mit Benutzer `qulsi` und dem angezeigten Passwort hinzufügen und das angezeigte Thema abonnieren. `NTFY_PUBLIC_URL` muss eine Adresse sein, unter der das Handy ntfy erreicht (z. B. über Tailscale). ntfy lässt ohne Token bzw. Passwort niemanden lesen oder schreiben (deny-all).

### Freigaben in der App

Die App meldet sich für Live-Ereignisse und Bilder mit einem Sitzungs-Cookie an (`POST /api/sitzung`), der App-Schlüssel steht nie in einer URL. Schlüssel, Tokens und Passwörter werden in allen Logs des Core maskiert.

Offene Freigaben stehen in der QuLSI-App unter „Heute“ und im Gespräch, mit Vorschau und Ablaufzeit. **Ablehnen** geht dort immer. **Erlauben** nur mit einer eigenen Freigabe-PIN, damit ein gestohlener App-Schlüssel keine Aktion freigeben kann.

Die PIN legt man in der App unter Einstellungen fest. Die erste PIN (und eine vergessene) geht nur mit einem Einrichtungscode, den allein die Box zeigt: 15 Minuten gültig, nur einmal verwendbar. Ändern geht in der App mit der alten PIN.

```bash
scripts/freigabe-pin.sh            # Einrichtungscode zeigen (RunPod: bash runpod/qulsi.sh pin)
scripts/freigabe-pin.sh --aus      # PIN entfernen
```

Nach 5 falschen PINs ist Erlauben in der App 15 Minuten gesperrt, jede weitere Sperre dauert doppelt so lang (bis 16 Stunden), und das Handy bekommt eine Warnung. Über das Handy geht es weiter. Jede Entscheidung steht in `audit_log` (`app_erlaubt`, `app_abgelehnt`, `app_pin_falsch`, `handy_erlaubt`, …), nach dem Erlauben meldet ntfy das Ergebnis auch aufs Handy.

### Kalender

Die „geheime iCal-Adresse“ aus Google Kalender, iCloud, Nextcloud oder Outlook in `CALENDAR_ICS_URLS` eintragen (mehrere mit `;`). Der Connector spiegelt die Termine alle 15 Minuten nach Postgres, gesucht wird immer im Cache. Neue Termine von QuLSI bleiben lokal (ICS ist nur lesbar).

### Konten: Google, Microsoft, NAS, Browser

**In der App (ohne Terminal):** Einstellungen > Konten & Connectoren. Passwörter, Tokens und Client-IDs
eingeben bzw. „Verbinden“ tippen; jede Änderung braucht eine Freigabe (Freigabe-PIN in der App oder Handy).
Die Werte gehen direkt in den Tresor und lassen sich über die App nie wieder auslesen; Google- und
Microsoft-Anmeldungen löst der Tresor selbst ein (core/qulsi_core/konten.py, `/verwaltung` im Tresor,
nur für den Core). Auf RunPod landen sie unverschlüsselt in der env-Datei des Pods (nur Test-Konten).
Hosts und Benutzernamen (IMAP_HOST, NAS_HOST, HA_URL, ...) stehen weiter in der `.env`.
Die Befehle unten sind der Weg auf der Kommandozeile.

Alle vier sind optional. Ohne Einrichtung laufen sie trotzdem und sagen dem Modell „nicht verbunden“.
Lesen ist frei, Termine anlegen und Entwürfe schreiben ist `medium`, Mails senden ist `high` und geht nur
mit Freigabe auf dem Handy (mit Vorschau des Entwurfs). Mails, Dateien und Webseiten gelten als fremder Text.
Termine aus Google und Outlook landen im lokalen Termin-Cache: `calendar.search`, „Heute“ und das
Morgenbriefing sehen sie, auch offline. Die Anmeldung (Refresh-Token) liegt verschlüsselt im Tresor, nicht in der
`.env` und nicht im Connector; der Connector bekommt nur kurzlebige Zugriffstokens.

| Connector | Fähigkeiten | darf ins Internet zu |
|---|---|---|
| `google` | `google.calendar.sync/create`, `google.mail.search/read/draft/send`, `google.drive.search/read` | oauth2.googleapis.com, www.googleapis.com, gmail.googleapis.com |
| `microsoft` | `microsoft.calendar.sync/create`, `microsoft.mail.search/read/draft/send`, `microsoft.onedrive.search/read` | login.microsoftonline.com, graph.microsoft.com, OneDrive-Downloadserver |
| `nas` | `nas.list/search/read/index` (nur lesen) | nur `NAS_HOST:445` im Heimnetz |
| `browser` | `browser.open` (Text und Links einer Seite, kein JavaScript) | `BROWSER_ALLOWED_DOMAINS`; bei `*` jede öffentliche Seite, nie Heimnetz oder Box |

**Google** (einmalig, etwa 10 Minuten): in der [Google Cloud Console](https://console.cloud.google.com) ein Projekt anlegen,
Google Calendar API, Gmail API und Google Drive API aktivieren, unter „OAuth-Zustimmungsbildschirm“ Typ „Extern“ wählen
und dich als Testnutzer eintragen, dann unter „Anmeldedaten“ eine OAuth-Client-ID vom Typ **Desktop-App** erstellen.
Die ID als `GOOGLE_CLIENT_ID` in die `.env`, das Secret in den Tresor, dann:

    docker compose run --rm tresor setzen GOOGLE_CLIENT_SECRET
    docker compose up -d connector-google egress tresor
    docker compose exec connector-google python google/server.py anmelden

Den Link auf Handy oder Laptop öffnen, zustimmen, die Adresse der Seite, die danach nicht lädt (127.0.0.1), kopieren und einfügen.
Wichtig: Solange die App im Status „Testen“ ist, läuft die Google-Anmeldung nach 7 Tagen ab. Für Dauerbetrieb den
Zustimmungsbildschirm auf „In Produktion“ stellen (ohne Prüfung durch Google, du siehst dann einmal eine Warnung „nicht überprüft“).

**Microsoft**: im [Entra-Portal](https://entra.microsoft.com) unter „App-Registrierungen“ eine neue App anlegen
(Kontotyp „Konten in einem beliebigen Organisationsverzeichnis und persönliche Microsoft-Konten“), unter
„Authentifizierung“ **Öffentliche Clientflows zulassen** einschalten. Die Anwendungs-ID als `MS_CLIENT_ID` in die `.env`, dann:

    docker compose exec connector-microsoft python microsoft/server.py anmelden

QuLSI zeigt einen Code, den du auf microsoft.com/devicelogin eingibst. Kein Secret nötig.

**NAS**: auf dem NAS einen Benutzer `qulsi` anlegen, der die Freigabe nur lesen darf. `NAS_HOST`, `NAS_SHARE`,
`NAS_USER` in die `.env`, das Passwort mit `docker compose run --rm tresor setzen NAS_PASSWORD` in den Tresor. Ordner, die ins Gedächtnis sollen (Volltext in Qdrant, Kopie im
Object Storage, auffindbar mit `memory.search` auch wenn das NAS aus ist), mit `;` getrennt in `NAS_INDEX_ORDNER`.

**Browser**: `BROWSER_ALLOWED_DOMAINS=*` erlaubt jede öffentliche Seite. Enger geht z. B. `*.wikipedia.org;*.bahn.de`.
Geräte im Heimnetz (Router, NAS, Home Assistant) sperrt der Egress-Proxy für den Browser immer, auch wenn ein Link dorthin zeigt.

Auf einer schon eingerichteten Box die neuen Datenbank-Rollen einmal anlegen (neue Installationen machen das selbst):
`DB_PW_GOOGLE`, `DB_PW_MICROSOFT`, `DB_PW_NAS` in die `.env`, dann
`docker compose up -d postgres && docker compose exec postgres bash /docker-entrypoint-initdb.d/10_connector_konten.sh`.
Auf RunPod: `bash runpod/qulsi.sh anmelden google` (bzw. `microsoft`); das NAS ist von RunPod aus nur über Tailscale erreichbar.


### Kameras und Gegenstände

Live-Kameras sind vorerst ausgeschaltet; Vision arbeitet mit Fotos, die du hochlädst. Zum Einschalten: Kamera-URLs in `.env`, Zonen in `config/cameras.yaml` (Polygone mit Werten 0 bis 1). Mit Kameras startest du mit `docker compose -f docker-compose.yml -f docker-compose.kameras.yml --profile box up -d` und setzt danach `KAMERA_NETZ=192.168.1.0/24 sudo scripts/firewall.sh`.

```bash
scripts/anlernen.sh "Schlüsselbund" keys "Schlüssel,Hausschlüssel" foto1.jpg foto2.jpg foto3.jpg
BLE_TAG=tile:abcd1234 scripts/anlernen.sh "Schlüsselbund" keys "Schlüssel" foto1.jpg
```

Gespeichert werden Fakten („Schlüsselbund jetzt: Küchentisch“), kein Video. Bildausschnitte liegen 14 Tage im Object Storage, die Fingerabdrücke in Qdrant.

### Stimme

Der Voice-Dienst macht Spracherkennung (Whisper) und reicht die Sprachausgabe an den Dienst `tts` weiter. Dort läuft eines von drei Modellen, wählbar mit `TTS_MODELL` in `.env`: `qwen3-tts` (Qwen3-TTS 0.6B mit eingebauten Sprechern, Standard), `cosyvoice3` (Fun-CosyVoice3 0.5B) oder `chatterbox` (Chatterbox Multilingual). Jedes Modell hat ein eigenes Image, weil sie verschiedene Bibliotheken brauchen. Für eine eigene Stimme legst du eine saubere Aufnahme von 5 bis 15 Sekunden als `config/stimme/stimme.wav` ab und den gesprochenen Text als `config/stimme/stimme.txt`, oder du nimmst sie in der App auf (Einstellungen > Stimme > Stimmprobe). Aus der App wird sie erst nach einer Freigabe übernommen (Handy oder App mit Freigabe-PIN), die alte bleibt als `stimme.vorher.wav`. Qwen3-TTS nimmt dafür sein Base-Modell, das `tts-modell` gleich mit herunterlädt. Piper kann keine Stimme nachsprechen; ohne Aufnahme spricht Qwen3-TTS mit einem eingebauten Sprecher (`TTS_SPRECHER`), CosyVoice mit einer Beispielstimme mit leichtem Akzent. Fällt der Dienst aus, spricht Piper. Die Web-App liest Satz für Satz vor: der erste Satz spielt, während der nächste schon erzeugt wird. Alle drei Modelle sind auf echter Hardware noch nicht ausprobiert. Beamforming und Echounterdrückung macht das Mikrofon-Array (z. B. XMOS XVF3800), Rauschunterdrückung der Browser bzw. das Array, die Sprachpausen-Erkennung (VAD) Whisper. Für Sprachsatelliten über Home Assistant: `docker compose --profile ha-voice up -d` (Wyoming Whisper, Piper, openWakeWord) und in HA „Extended OpenAI Conversation“ mit Base URL `http://<box>/v1`, Modell `qulsi-stimme`, API-Key `QULSI_API_KEY`.

## Grafikspeicher (RTX 5060 Ti, 16 GB)

Die Modelle laufen nicht alle gleichzeitig. Nur Bonsai bleibt geladen; die anderen kommen bei Bedarf in den Grafikspeicher und gehen nach 10 Minuten ohne Nutzung wieder raus (`*_SCHLAF_SEKUNDEN`, `*_LEERLAUF_SEKUNDEN` in `.env`). Der erste Aufruf nach einer Pause dauert dadurch ein bis drei Sekunden länger.

| Dienst | geschätzt | geladen |
|---|---|---|
| Bonsai-27B PQ2_0 + KV-Cache (48K, q4_0) | ≈ 8,8 GB | immer |
| Qwen3-1.7B Q8 (kleines Modell) | 0 (CPU), auf der GPU ≈ 2,2 GB | bei kurzen Befehlen |
| Qwen3-Embedding 0,6B | ≈ 0,8 GB | beim Speichern und Suchen im Gedächtnis |
| OWLv2-Base + SigLIP-Base (YOLO-World-S wahlweise) | ≈ 1,5 GB | bei Fotos (mit Live-Kameras immer) |
| Whisper large-v3-turbo | ≈ 1,5 GB | bei Sprache |
| Sprachausgabe: Qwen3-TTS 0.6B (Standard) / CosyVoice 3 / Chatterbox | ≈ 3 / 4 / 4 GB | beim Vorlesen |

Das kleine Modell läuft auf der Box standardmäßig auf der CPU (`KLEIN_GPU_LAYERS=0`), damit die Sprachausgabe Platz hat. Bonsai allein belegt etwa 8,8 GB, es bleiben gut 7 GB. Ein Sprachbefehl braucht zusätzlich Whisper und die Sprachausgabe: mit Qwen3-TTS etwa 13,3 GB (mit CosyVoice wären es 14,3 GB). Ist alles gleichzeitig geladen (Sprache, Fotoanalyse und Gedächtnis im selben Moment), sind es etwa 15,6 GB, dazu ein paar hundert MB je Dienst für CUDA selbst; dann bekommt das zuletzt startende Modell keinen Platz, die Sprachausgabe fällt auf Piper zurück. Die Werte sind geschätzt; mit `nvidia-smi` nachsehen. Wird es eng: `STT_DEVICE=cpu` (Whisper auf der CPU).

## Backup

```bash
cp scripts/restic.env.example scripts/restic.env && nano scripts/restic.env
source scripts/restic.env && restic init && scripts/backup.sh
sudo cp scripts/systemd/qulsi-backup.* /etc/systemd/system/ && sudo systemctl enable --now qulsi-backup.timer
```

Gesichert werden Datenbank-Dump, Qdrant, Object Storage, `.env` und Konfiguration, keine Modelle.

## Tests ohne GPU

```bash
scripts/test-stack.sh          # baut alles, startet den Teststack und führt alle Tests aus
scripts/test-stack.sh --down   # danach wieder abbauen
```

Im Teststack ersetzt `fake-llm` alle Modelle durch feste Regeln, `tests/integration/fake_ha.py` spielt Home Assistant, ICS-Kalender und Mailserver „im Internet“ (nur über den Egress-Proxy erreichbar), Vision und Voice laufen mit Test-Anbietern.

## Nächste Schritte

- Auf RunPod bzw. der Box: Bonsai-Tempo und Werkzeugaufrufe mit echten Fragen prüfen, dann das kleine Modell gegen Bonsai vergleichen.
- Dateinamen der Modelle auf den Hugging-Face-Seiten prüfen (Bonsai, mmproj, Qwen3-1.7B-GGUF).
- `LLAMA_REF` in `runtime/llm/Dockerfile` auf einen festen Commit setzen.
- CalDAV statt ICS, damit QuLSI Termine auch im eigenen Kalender anlegen kann.
- Qwen3-TTS als zweiten Sprachausgabe-Anbieter einrichten, sobald ein Server dafür läuft.

## Erweitern

Alles, was QuLSI kann, ist ein Werkzeug. Werkzeuge kommen aus zwei Quellen: Kern-Werkzeuge in `core/qulsi_core/tools.py` (Gedächtnis, Personen, Aufgaben, Nutzer) und Connectoren, je einer pro Ordner unter `connectors/`. Für Neues ist ein Connector der richtige Weg: Er läuft getrennt vom Core, bekommt nur die Rechte aus seinem Manifest und darf nur zu den Adressen, die dort stehen.

### Neuer Connector in sechs Schritten

Beispiel: ein Connector `wetter` mit der Fähigkeit `wetter.abfragen`.

1. **Ordner und Manifest** `connectors/wetter/manifest.yaml`:
   ```yaml
   name: wetter
   titel: Wetter
   url: http://connector-wetter:8000/mcp
   dienst: connector-wetter
   capabilities: [wetter.abfragen]
   network:
     allowed: ["api.open-meteo.com:443"]   # nur dorthin lässt der Egress-Proxy den Connector
   permissions: {abfragen: low}            # low, medium, high (siehe Policy Engine)
   untrusted: true                         # Antworten von außen sind fremder Text
   offline: false
   ```
2. **Server** `connectors/wetter/server.py` nach dem Muster von `connectors/web/server.py`: ein `MCPServer`, je Fähigkeit eine Funktion mit `@mcp.tool()`, deren Name die Fähigkeit mit `_` statt `.` ist (`wetter_abfragen`). Ins Internet nur mit `intern()` bzw. dem Client aus `qulsi_connector`, der über den Egress-Proxy geht. Der Docstring ist die Beschreibung, die das Modell sieht.
3. **Dienst eintragen**: in `docker-compose.yml` einen Block `connector-wetter` wie `connector-web` (Anker `*connector`, `command: ["python", "wetter/server.py"]`) und den Namen in `MCP_ALLOWED_HOSTS` ergänzen. Für RunPod einen Port in `runpod/paths.sh` (`CONNECTOR_PORT`) und bei Bedarf einen `case`-Zweig in `runpod/run.sh`.
4. **Signieren**: `python3 scripts/signieren.py`. Ohne gültige Signatur lädt der Core den Connector nicht und der Egress lässt ihn nicht ins Internet.
5. **Starten und prüfen**: `docker compose up -d --build connector-wetter && docker compose restart core`, im Log muss `Werkzeuge geladen: ... wetter.abfragen` stehen. Dann in der App fragen: „Wie wird das Wetter morgen?“
6. **Test**: einen Unit-Test unter `tests/unit/` (Muster `test_connectoren_konten.py`) oder einen Ende-zu-Ende-Test in `tests/integration/test_stack.py`; `scripts/test-stack.sh` muss grün bleiben.

Was der Hub nicht übernimmt: Fähigkeiten, die nicht im Manifest stehen, und Fähigkeiten ohne Stufe werden als `high` behandelt (immer Freigabe). Die Freigabe-Vorschau fürs Handy ist ein zweites Werkzeug (`vorschau:` im Manifest, Beispiel Mail).

### Andere Erweiterungen

| Was | Wo |
|---|---|
| Anderes Sprachmodell (auch Cloud) | Eintrag in `config/litellm.yaml`, Namen in `config/router.yaml` zuordnen, Datei in `.env` (`BONSAI_FILE`, `KLEIN_FILE`). QuLSI selbst ändert sich nicht. |
| Neues Kern-Werkzeug | `core/qulsi_core/tools.py`: `KernWerkzeug` mit Schema und Funktion im Konstruktor von `Kern` registrieren, Stufe in `config/policy.yaml`. Nur für Dinge, die ohne Internet und direkt am Gedächtnis arbeiten. |
| Hintergrund-Agent | Eintrag in `config/agenten.yaml` (`typ: llm`, `zeitplan`, `werkzeuge`, `auftrag`), `docker compose restart core`. |
| Einstellung in der App | Feld in `core/qulsi_core/einstellungen.py` (wird in Postgres `system_state` gespeichert) und die Seite in `core/qulsi_core/web/index.html`. Sicherheitseinstellungen können die App nur strenger machen, nie lockerer. |
| Freigabe-Stufe ändern | `config/policy.yaml`, z. B. `home.set_state: high` oder `browser.search: deny`. |
| Neue Sprache der Oberfläche | alle Texte stehen in `core/qulsi_core/web/index.html` und `config/agent_prompt.md`; es gibt noch keine Übersetzungsdatei. |
| Name des Produkts | `scripts/umbenennen.sh` (Abschnitt „Name ändern“). |

Regeln für jede Erweiterung: keine Daten, Namen oder Schlüssel im Code (ein Test prüft das bei jedem Paket), fremde Inhalte als fremd kennzeichnen (`untrusted`), nichts an der Policy Engine vorbei, Antworten an den Nutzer in einfacher Sprache.

## Name ändern

Der Name steckt an vielen Stellen (Ordner, Container, Umgebungsvariablen, App). `scripts/umbenennen.sh qulsi neuername QuLSI NeuerName` tauscht ihn überall auf einmal aus.

## Lizenz

MIT, siehe [LICENSE](LICENSE). Das gilt für den Code in diesem Repo. Die Bausteine, die QuLSI lädt oder als eigene Dienste startet, behalten ihre eigenen Lizenzen:

| Baustein | Lizenz |
|---|---|
| FastAPI, uvicorn, httpx, mcp, LiteLLM, pyrage, smbprotocol, faster-whisper, Chatterbox, Pillow, pypdf, icalendar | MIT bzw. BSD |
| Bonsai-27B (PrismML), Qwen3-1.7B, Qwen3-Embedding, Qwen3-TTS, Fun-CosyVoice3, transformers, OWLv2, SigLIP, OpenCV, cryptography, Qdrant, Home Assistant, ntfy, openWakeWord (Code) | Apache-2.0 |
| psycopg, recurring-ical-events | LGPL (als Bibliothek genutzt, unverändert) |
| paho-mqtt | EPL-2.0 oder BSD-3-Clause |
| Piper (`piper-tts` ab 1.3, Projekt `piper1-gpl`) | **GPL-3.0-or-later**: Der Voice-Dienst bindet Piper als Bibliothek ein. Der Code hier bleibt MIT, aber wer das fertig gebaute Voice-Image weitergibt, muss die GPL beachten. |
| openWakeWord, vortrainierte Wake-Word-Modelle (z. B. `ok_nabu`) | **CC BY-NC-SA 4.0**: nur nicht kommerziell. Für den kommerziellen Einsatz ein eigenes Wake-Word trainieren (`data/wakeword`). |
| SearXNG | AGPL-3.0, läuft unverändert als getrennter Dienst |
| Open WebUI | Open WebUI License (BSD-3 mit Branding-Klausel ab Version 0.6.6: das Open-WebUI-Logo darf nur bis 50 Nutzer entfernt werden), läuft unverändert als getrennter Dienst |
| PostgreSQL, Mosquitto, Tailscale | eigene freie Lizenzen, laufen als getrennte Dienste |
| ultralytics (YOLO-World, nur bei `DETECTOR=yolo-world`) | **AGPL-3.0**: Wer QuLSI mit YOLO-World weitergibt oder als Dienst anbietet, muss die AGPL beachten. Mit dem Standard `DETECTOR=owlv2` wird ultralytics gar nicht installiert (eigene Datei `vision/requirements-yolo.txt`). |

Modelle und Container-Images liegen nicht in diesem Repo, sie werden beim Einrichten geladen.
