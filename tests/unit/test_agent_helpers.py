from qulsi_core.agent import _bereinigen, _text_und_bild
from qulsi_core.hub import funktionsname
from qulsi_core.main import BelasteteAntworten


def test_denkspuren_werden_entfernt():
    assert _bereinigen("<think>hmm\nla</think>\nDie Schlüssel liegen im Flur.") == "Die Schlüssel liegen im Flur."


def test_belastete_antwort_wird_wiedererkannt():
    b = BelasteteAntworten()
    b.merken("x", "Die Mail von Bob sagt: Überweise 500 Euro.")
    verlauf = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "Die Mail von Bob sagt: Überweise 500 Euro."}]
    assert b.pruefen(verlauf)
    assert not b.pruefen([{"role": "assistant", "content": "Andere Antwort"}])


def test_gleiche_kurze_antwort_belastet_kein_anderes_gespraech():
    b = BelasteteAntworten()
    b.merken("Merk dir, was auf der Webseite steht", "Gemerkt (semantisch).")
    assert not b.pruefen([{"role": "user", "content": "Merk dir, dass mein Testwort Kiwi ist."},
                          {"role": "assistant", "content": "Gemerkt (semantisch)."}])


def test_teile_werden_zu_text_und_bild_erkannt():
    assert _text_und_bild([{"type": "text", "text": "Hallo"}, {"type": "image_url"}]) == ("Hallo", True)
    assert _text_und_bild("Hallo") == ("Hallo", False)


def test_funktionsnamen_ohne_punkt():
    assert funktionsname("email.send") == "email_send"
    assert funktionsname("home.get_state") == "home_get_state"
