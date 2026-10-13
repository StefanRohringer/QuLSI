import numpy as np

from qulsi_vision.ble import RaumSchaetzer
from qulsi_vision.geometry import Detection, bester_treffer, bewegung, nachbarn, punkt_in_polygon, zone_finden
from qulsi_vision.models import HistogrammEmbedder

TISCH = [[0.1, 0.5], [0.6, 0.5], [0.6, 0.95], [0.1, 0.95]]


def test_punkt_in_polygon():
    assert punkt_in_polygon((0.3, 0.7), TISCH)
    assert not punkt_in_polygon((0.8, 0.7), TISCH)
    assert punkt_in_polygon((0.1, 0.7), TISCH)  # Kante zählt


def test_zone_nach_fusspunkt():
    zonen = [{"name": "Küchentisch", "polygon": TISCH}]
    # Box ragt oben aus dem Tisch heraus, Unterkante liegt darauf
    assert zone_finden(Detection("keys", 0.5, (0.2, 0.3, 0.3, 0.6)), zonen)["name"] == "Küchentisch"
    assert zone_finden(Detection("keys", 0.5, (0.7, 0.1, 0.8, 0.2)), zonen) is None


def test_nachbarn():
    a = Detection("keys", 0.5, (0.2, 0.7, 0.3, 0.8))
    b = Detection("cup", 0.8, (0.32, 0.68, 0.38, 0.82))
    c = Detection("laptop", 0.8, (0.8, 0.1, 0.95, 0.3))
    assert nachbarn(a, [a, b, c]) == ["cup"]


def test_bester_treffer_mit_schwelle():
    fp = np.array([1.0, 0.0, 0.0])
    kand = [(1, np.array([0.9, 0.1, 0.0])), (2, np.array([0.0, 1.0, 0.0]))]
    assert bester_treffer(fp, kand, 0.8)[0] == 1
    assert bester_treffer(np.array([0.5, 0.5, 0.7]), kand, 0.95)[0] is None


def test_histogramm_erkennt_gleichen_gegenstand_wieder():
    rng = np.random.default_rng(0)
    a = rng.integers(0, 255, (60, 80, 3), dtype=np.uint8)
    b = np.clip(a.astype(int) + rng.integers(-8, 8, a.shape), 0, 255).astype(np.uint8)  # leichtes Rauschen
    c = rng.integers(0, 255, (60, 80, 3), dtype=np.uint8)
    e = HistogrammEmbedder()
    va, vb, vc = e.vektor(a), e.vektor(b), e.vektor(c)
    assert va.shape == (768,)
    assert float(va @ vb) > float(va @ vc)


def test_bewegung():
    a = np.zeros((90, 160), np.uint8)
    b = a.copy(); b[10:40, 10:40] = 200
    assert bewegung(None, a, 0.01)
    assert not bewegung(a, a.copy(), 0.01)
    assert bewegung(a, b, 0.01)


def test_ble_naechster_raum():
    s = RaumSchaetzer({"kueche": "Küche", "flur": "Flur"})
    s.messung("tag1", "kueche", 4.0, jetzt=100)
    s.messung("tag1", "flur", 1.2, jetzt=100)
    s.messung("tag1", "bad", 0.1, jetzt=100)  # unbekannter Raum wird ignoriert
    assert s.raum("tag1", jetzt=101) == ("Flur", 1.2)
    assert s.raum("tag1", jetzt=200) is None  # zu alt
