"""Einstellungen aus der App: sofort wirksam, Sicherheit nur strenger als policy.yaml."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from qulsi_core.einstellungen import Einstellungen
from qulsi_core.policy import Entscheid, Policy, Stufe
from qulsi_core.proaktiv.laeufer import Einstellungen as AgentEinst
from qulsi_core.router import Router

ROOT = Path(__file__).resolve().parents[2]


def system() -> Einstellungen:
    policy = Policy.load(ROOT / "config" / "policy.yaml")
    policy.registrieren("email.send", Stufe.HIGH)
    policy.registrieren("calendar.search", Stufe.LOW)
    hub = SimpleNamespace(policy=policy, offline_modus="auto", _offline_cache=(5.0, False), werkzeuge={},
                          kern=SimpleNamespace(organisator=SimpleNamespace(minuten=10.0),
                                               memchip=SimpleNamespace(auto_stunden=0.0)))
    state = SimpleNamespace(hub=hub, agent=SimpleNamespace(router=Router.load(ROOT / "config" / "router.yaml")),
                            hintergrund=SimpleNamespace(einst=AgentEinst(), an=True),
                            freigaben=SimpleNamespace(gueltig=300))
    e = Einstellungen(state)
    e.grund_merken()
    return e


def test_offline_modus_wirkt_sofort():
    e = system()
    e.anwenden("system", {"offline": "true"})
    assert e.hub.offline_modus == "true"
    with pytest.raises(ValueError):
        e.anwenden("system", {"offline": "vielleicht"})
    e.anwenden("system", {})
    assert e.hub.offline_modus == "auto"


def test_modelle_tauschen_und_zuruecksetzen():
    e = system()
    e.anwenden("modelle", {"klein": "aus", "gross": "cloud", "gross_ab_zeichen": 900})
    assert e.router.modelle["klein"] is None and e.router.modelle["gross"] == "cloud"
    assert e.router.gross_ab == 900
    with pytest.raises(ValueError):
        e.anwenden("modelle", {"standard": "aus"})
    assert e.router.modelle["gross"] == "cloud"  # Fehler ändert nichts
    e.anwenden("modelle", {})
    assert e.router.modelle["klein"] == "qulsi-klein"


def test_sicherheit_nur_strenger():
    e = system()
    e.anwenden("sicherheit", {"belastet_medium": "verboten", "limit_high": 3, "bestaetigung_sekunden": 120,
                              "stufen": {"calendar.search": "medium", "email.send": "deny"}})
    p = e.policy
    assert p.belastet_medium is Entscheid.ABGELEHNT and p.limits_stufe[Stufe.HIGH] == 3
    assert p.stufe("calendar.search") is Stufe.MEDIUM and p.stufe("email.send") is Stufe.DENY
    assert e.state.freigaben.gueltig == 120
    for lockerer in ({"belastet_medium": "erlaubt"}, {"belastet_high": "bestaetigung"},
                     {"limit_high": 50}, {"bestaetigung_sekunden": 3600}, {"stufen": {"email.send": "low"}}):
        with pytest.raises(ValueError):
            e.anwenden("sicherheit", lockerer)
    assert p.stufe("email.send") is Stufe.DENY  # unverändert nach den Fehlversuchen


def test_agenten_ruhezeit_und_gedaechtnis():
    e = system()
    e.anwenden("agenten", {"ruhezeit": "23:00-06:30", "max_hinweise_pro_stunde": 2})
    assert e.hintergrund.einst.ruhezeit.von.hour == 23 and e.hintergrund.einst.max_hinweise_pro_stunde == 2
    e.anwenden("agenten", {"ruhezeit": ""})
    assert e.hintergrund.einst.ruhezeit.von is None
    e.anwenden("gedaechtnis", {"memchip_stunden": 24})
    assert e.kern.memchip.auto_stunden == 24
    with pytest.raises(ValueError):
        e.anwenden("gedaechtnis", {"organisator_minuten": -1})


def test_unbekanntes_wird_abgelehnt():
    e = system()
    with pytest.raises(ValueError):
        e.anwenden("system", {"ntfy_thema": "fremd"})
    with pytest.raises(ValueError):
        e.anwenden("benachrichtigungen", {})


def test_stimme_als_auswahl():
    from qulsi_core.einstellungen import stimm_auswahl

    e = system()
    e.anwenden("stimme", {"stimme": "eigene"})
    e.anwenden("stimme", {"stimme": "uncle_fu", "vorlesen": False})
    with pytest.raises(ValueError):
        e.anwenden("stimme", {"stimme": "Ryan; rm -rf"})
    stimmen = {"eigene": True, "sprecher": ["ryan", "vivian", "neu_x"], "standard": "eigene"}
    a = stimm_auswahl(stimmen, "")
    assert [x["id"] for x in a["auswahl"]] == ["eigene", "ryan", "vivian", "neu_x"]
    assert a["aktiv"] == "eigene" and a["eigene_vorhanden"] is True
    assert a["auswahl"][1] == {"id": "ryan", "name": "Ryan", "hinweis": "männlich, Englisch"}
    assert a["auswahl"][3]["name"] == "Neu X"
    assert stimm_auswahl(stimmen, "vivian")["aktiv"] == "vivian"
    ohne = stimm_auswahl({"eigene": False, "sprecher": ["ryan"], "standard": "ryan"}, "eigene")
    assert ohne["aktiv"] == "ryan" and [x["id"] for x in ohne["auswahl"]] == ["ryan"]  # Probe fehlt: Standard
    assert stimm_auswahl(None, "ryan") == {"auswahl": [], "aktiv": "ryan", "eigene_vorhanden": False}


def test_chatterbox_in_der_auswahl():
    from qulsi_core.einstellungen import stimm_auswahl

    stimmen = {"eigene": True, "sprecher": ["ryan", "chatterbox", "chatterbox_eigene"], "standard": "eigene"}
    a = stimm_auswahl(stimmen, "")
    assert a["aktiv"] == "eigene"  # Qwen3 bleibt Standard
    assert [x["name"] for x in a["auswahl"]] == ["Eigene Stimme", "Ryan", "Chatterbox", "Eigene Stimme mit Chatterbox"]
    assert stimm_auswahl(stimmen, "chatterbox")["aktiv"] == "chatterbox"


def test_17b_stimmen_heissen_hochwertig():
    from qulsi_core.einstellungen import stimm_auswahl

    a = stimm_auswahl({"eigene": True, "sprecher": ["ryan", "eigene_17b", "ryan_17b"], "standard": "eigene"}, "")
    assert [x["name"] for x in a["auswahl"]] == ["Eigene Stimme", "Ryan", "Eigene Stimme (hochwertig)", "Ryan (hochwertig)"]
    assert a["aktiv"] == "eigene"
