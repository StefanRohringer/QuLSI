from pathlib import Path

from qulsi_core.router import Router

ROUTER = Router.load(Path(__file__).resolve().parents[2] / "config" / "router.yaml")


def test_uhrzeit_ohne_modell():
    r = ROUTER.waehlen("Wie spät ist es?")
    assert r.art == "system" and r.modell is None and r.antwort.startswith("Es ist ")
    assert ROUTER.waehlen("Welches Datum haben wir heute?").antwort.startswith("Heute ist ")


def test_uhrzeit_anderswo_geht_ans_modell():
    assert ROUTER.waehlen("Wie spät ist es gerade in Tokio?").art != "system"


def test_kurzer_befehl_kleines_modell_mit_ersatz():
    r = ROUTER.waehlen("Schreib Max eine Mail, dass ich später komme")
    assert r.art == "klein" and r.modell == "qulsi-klein" and r.ersatz == "bonsai-fast" and r.werkzeuge


def test_analyse_grosses_modell_aber_nie_per_stimme():
    assert ROUTER.waehlen("Analysiere das Angebot der Küche und vergleiche es mit dem Budget").modell == "bonsai-think"
    assert ROUTER.waehlen("Analysiere das Angebot", kanal="stimme").modell == "bonsai-fast"


def test_standard_und_bild():
    assert ROUTER.waehlen("Was habe ich heute Nachmittag vor?").modell == "bonsai-fast"
    r = ROUTER.waehlen("Was ist das?", hat_bild=True)
    assert r.art == "vision" and not r.werkzeuge


def test_kleines_modell_abschaltbar(tmp_path):
    r = Router.load(Path(__file__).resolve().parents[2] / "config" / "router.yaml", overrides={"klein": "aus"})
    assert r.waehlen("Schreib Max eine Mail").art == "standard"


def test_deutsche_begriffe_finden_vision_labels():
    from qulsi_core.tools import labels_fuer

    vok = {"keys": "Schlüssel", "cup": "Tasse", "cell phone": "Handy"}
    assert "keys" in labels_fuer("Schlüssel", vok)
    assert "cup" in labels_fuer("Tassen", vok)
    assert "cell phone" in labels_fuer("handy", vok)
    assert labels_fuer("Brille", vok) == ["brille"]


def test_bild_ohne_bildteil_ehrliche_antwort(monkeypatch):
    monkeypatch.setenv("QULSI_BILDER", "")
    r = ROUTER.waehlen("Was ist das?", hat_bild=True)
    assert r.art == "system" and r.modell is None and "mmproj" in r.antwort
    monkeypatch.setenv("QULSI_BILDER", "an")
    assert ROUTER.waehlen("Was ist das?", hat_bild=True).art == "vision"


def test_modell_titel_lesbar():
    from qulsi_core.einstellungen import modell_titel
    assert modell_titel("bonsai-fast") == "Bonsai schnell"
    assert modell_titel("bonsai-think") == "Bonsai mit Denkmodus"
    assert modell_titel(None) == "Aus" and modell_titel("eigenes") == "eigenes"
