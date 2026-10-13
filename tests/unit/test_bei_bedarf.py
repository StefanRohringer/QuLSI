import time
from pathlib import Path

from qulsi_vision.bei_bedarf import BeiBedarf

ROOT = Path(__file__).resolve().parents[2]


class Modell:
    geladen = 0

    def __init__(self):
        Modell.geladen += 1

    def rechnen(self, x):
        return x * 2


def test_laedt_erst_bei_bedarf_und_gibt_nach_leerlauf_frei():
    Modell.geladen = 0
    m = BeiBedarf("Test", Modell, 0.05)
    assert not m.geladen and Modell.geladen == 0
    assert m.rechnen(3) == 6 and m.geladen
    assert not m.freigeben_wenn_leerlauf()  # gerade erst benutzt
    time.sleep(0.06)
    assert m.freigeben_wenn_leerlauf() and not m.geladen
    assert m.rechnen(1) == 2 and Modell.geladen == 2


def test_ohne_leerlauf_immer_geladen():
    m = BeiBedarf("Test", Modell, 0)
    assert m.geladen
    time.sleep(0.01)
    assert not m.freigeben_wenn_leerlauf() and m.geladen


def test_freigeben_sofort_auch_im_dauerbetrieb():
    Modell.geladen = 0
    m = BeiBedarf("Test", Modell, 0)
    assert m.freigeben() and not m.geladen
    assert m.rechnen(2) == 4 and Modell.geladen == 2  # nächste Anfrage lädt neu
    assert not BeiBedarf("Test", Modell, 600).freigeben()  # nichts geladen


def test_vision_voice_und_tts_haben_dieselbe_datei():
    a = ROOT / "vision" / "qulsi_vision" / "bei_bedarf.py"
    for b in (ROOT / "voice" / "qulsi_voice" / "bei_bedarf.py", ROOT / "tts" / "bei_bedarf.py"):
        assert a.read_bytes() == b.read_bytes()
