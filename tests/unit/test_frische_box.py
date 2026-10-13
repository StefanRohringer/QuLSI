"""Jede Box startet bei null: keine Demo-Daten, kein Name, eigene Schlüssel, nichts Persönliches im Paket."""

import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEMO = ("Kaffee schwarz", "Blauer Golf", "Müll", "Max Muster", "Anna Beispiel", "150 Jahre")


def test_datenbank_startet_leer():
    for f in (ROOT / "db" / "init").iterdir():
        text = f.read_text(encoding="utf-8")
        inserts = re.findall(r"INSERT INTO (\w+)", text)
        assert set(inserts) <= {"system_state"}, f"{f.name}: {inserts}"
        assert not [d for d in DEMO if d in text], f.name


def test_kein_name_in_prompts_und_konfiguration():
    for p in [*(ROOT / "config").glob("*.md"), *(ROOT / "config").glob("*.yaml"), ROOT / ".env.example",
              *(ROOT / "runpod").glob("*.sh")]:
        assert "Anna Beispiel" not in p.read_text(encoding="utf-8"), p.name


def test_paket_ohne_daten_schluessel_und_stimme(tmp_path):
    ziel = tmp_path / "p.tar.gz"
    subprocess.run(["bash", str(ROOT / "scripts" / "paket.sh"), str(ziel)], check=True, capture_output=True)
    with tarfile.open(ziel) as t:
        namen = [n.removeprefix("./") for n in t.getnames()]
    verboten = [n for n in namen if n == ".env" or n.startswith(("data/", ".git/")) and n != "data/"
                or re.search(r"(^|/)(stimme\.(wav|txt)|schluessel\.txt|restic\.env|signatur\.pub|SIGNATUR\.json)$", n)
                or n.endswith((".gguf", ".onnx", ".key", ".pem"))]
    assert not verboten, verboten
    assert "VERSION" in namen and "core/qulsi_core/version.json" in namen


def test_erststart_erzeugt_eigene_schluessel(tmp_path):
    werte = []
    for i in range(2):
        d = tmp_path / str(i)
        (d / "scripts").mkdir(parents=True)
        (d / ".env.example").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
        (d / "scripts" / "erststart.sh").write_text((ROOT / "scripts" / "erststart.sh").read_text(), encoding="utf-8")
        subprocess.run(["bash", str(d / "scripts" / "erststart.sh")], check=True, capture_output=True)
        env = (d / ".env").read_text(encoding="utf-8")
        assert "bitte-aendern" not in env and (d / ".env").stat().st_mode & 0o777 == 0o600
        werte.append(dict(re.findall(r"^(QULSI_API_KEY|POSTGRES_PASSWORD|DB_PW_MAIL|NTFY_TOPIC)=(.+)$", env, re.M)))
    assert len(werte[0]) == 4 and all(werte[0][k] != werte[1][k] for k in werte[0])
    assert werte[0]["NTFY_TOPIC"] != "qulsi"


def test_runpod_erzeugt_schluessel_beim_ersten_start():
    sh = (ROOT / "runpod" / "start.sh").read_text(encoding="utf-8")
    assert "QULSI_API_KEY=$(rnd)" in sh and "DB_PW_MAIL=$(rnd)" in sh and "pin-code --wenn-leer" in sh
