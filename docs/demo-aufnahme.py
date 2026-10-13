"""Nimmt die laufende QuLSI-App (Testmodell, RunPod-Startweg mit QULSI_FAKE_LLM=1) als Video auf.
Aufruf: python3 docs/demo-aufnahme.py <ausgabeordner>; danach ffmpeg für docs/demo.gif und docs/demo.mp4.
Erwartet die App unter http://localhost:8100 und die env-Datei in /var/tmp/ws/qulsi-data (WORKSPACE=/var/tmp/ws)."""
import os, re, subprocess, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
ENV = Path("/var/tmp/ws/qulsi-data/env").read_text()
KEY = re.search(r"^QULSI_API_KEY=(.+)$", ENV, re.M)[1].strip()
env = dict(os.environ, WORKSPACE="/var/tmp/ws", QULSI_VENVS="/var/tmp/ws/venvs", LC_ALL="C.UTF-8")
code = subprocess.run(["bash", "/var/tmp/ws/qulsi/runpod/run.sh", "core", "pin-code"], capture_output=True, text=True, env=env).stdout
CODE = re.search(r"([A-Z0-9]{4}-[A-Z0-9]{4})", code)[1]
PIN = "248163"

def pause(page, s): page.wait_for_timeout(int(s * 1000))

def tippen(page, text, warten=3.5):
    page.click("#text")
    page.type("#text", text, delay=45)
    pause(page, 0.4)
    page.press("#text", "Enter")
    pause(page, warten)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
    ctx = b.new_context(viewport={"width": 390, "height": 760}, device_scale_factor=2, is_mobile=True, has_touch=True,
                        locale="de-DE", color_scheme="dark", record_video_dir=str(OUT), record_video_size={"width": 780, "height": 1520})
    page = ctx.new_page()
    page.goto("http://localhost:8100/", wait_until="networkidle")
    pause(page, 1.5)
    page.fill("#schluessel", KEY); page.press("#schluessel", "Enter")
    page.wait_for_selector("#e-name", timeout=15000); pause(page, 1.2)
    page.type("#e-name", "Anna", delay=60); pause(page, 0.5)
    page.select_option("#e-land", "BY"); pause(page, 0.6)
    page.type("#pin-code", CODE, delay=40); page.type("#pin-neu", PIN, delay=60); page.type("#pin-neu2", PIN, delay=60)
    pause(page, 0.6); page.click("#e-fertig"); pause(page, 2.5)
    page.screenshot(path=str(OUT / "01-heute.png"))
    page.goto("http://localhost:8100/#/chat"); page.wait_for_selector("#text"); pause(page, 1.2)
    tippen(page, "Wie spät ist es?", 2.5)
    tippen(page, "Merk dir, dass mein Auto ein blauer Golf ist.", 3)
    tippen(page, "Heute 18:00 Uhr Zahnarzt", 3)
    tippen(page, "Was habe ich heute vor?", 3)
    page.screenshot(path=str(OUT / "02-chat.png"))
    tippen(page, "Max ist mein Bruder, seine Mail ist max@example.org.", 3)
    tippen(page, "Schick Max eine Mail, dass ich morgen später komme.", 4)
    page.screenshot(path=str(OUT / "03-freigabe.png"))
    try:
        page.get_by_role("button", name="Erlauben").first.click(timeout=4000); pause(page, 1.5)
        page.screenshot(path=str(OUT / "04-pin.png"))
        page.type("#pin", "12", delay=200); pause(page, 1.0)
        page.click("#pin-abbruch"); pause(page, 0.8)
        page.get_by_role("button", name="Ablehnen").first.click(timeout=4000); pause(page, 3)
        page.screenshot(path=str(OUT / "05-nach-freigabe.png"))
    except Exception as e:
        print("Freigabe:", e); page.screenshot(path=str(OUT / "04-fehler.png"))
    page.goto("http://localhost:8100/#/gedaechtnis"); page.reload(wait_until="networkidle"); pause(page, 3); page.screenshot(path=str(OUT / "06-gedaechtnis.png"))
    page.goto("http://localhost:8100/#/heute"); pause(page, 3); page.screenshot(path=str(OUT / "07-heute.png"))
    page.goto("http://localhost:8100/#/einstellungen"); pause(page, 3); page.screenshot(path=str(OUT / "08-einstellungen.png"))
    ctx.close(); b.close()
for v in OUT.glob("*.webm"): print("Video:", v, v.stat().st_size)
