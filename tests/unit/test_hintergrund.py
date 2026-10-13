"""Hintergrund-Agents ohne Datenbank: Zeitpläne, Ruhezeit, Konfiguration, Policy, Werkzeugkasten."""

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from qulsi_core.policy import Entscheid, Kontext, Policy, Stufe
from qulsi_core.proaktiv import AgentConfig, Ruhezeit, Werkzeugkasten, Zeitplan, config_laden

ROOT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("Europe/Berlin")


def t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def test_zeitplan_intervall():
    assert Zeitplan.lesen("alle 15 min").sekunden == 900
    assert Zeitplan.lesen("alle 2 std").sekunden == 7200
    assert Zeitplan.lesen("Stündlich").sekunden == 3600
    assert Zeitplan.lesen("alle 5 min").naechster(t("2026-10-07 10:00")) == t("2026-10-07 10:05")


def test_zeitplan_uhrzeit_und_tage():
    taeglich = Zeitplan.lesen("täglich 07:00")
    assert taeglich.naechster(t("2026-10-07 06:59")) == t("2026-10-07 07:00")
    assert taeglich.naechster(t("2026-10-07 07:00")) == t("2026-10-08 07:00")
    werktags = Zeitplan.lesen("werktags 08:00")
    assert werktags.naechster(t("2026-10-09 09:00")) == t("2026-10-12 08:00")  # Freitag -> Montag
    assert Zeitplan.lesen("mo,mi,fr 18:00").naechster(t("2026-10-07 18:30")) == t("2026-10-09 18:00")
    assert Zeitplan.lesen("wochenende 09:00").naechster(t("2026-10-07 12:00")) == t("2026-10-10 09:00")


@pytest.mark.parametrize("falsch", ["jede stunde", "alle 0 min", "täglich 7 uhr", "blau 08:00", "alle 3 jahre"])
def test_zeitplan_fehler(falsch):
    with pytest.raises(ValueError):
        Zeitplan.lesen(falsch)


def test_ruhezeit_ueber_mitternacht():
    r = Ruhezeit.lesen("22:00-07:00")
    assert r.aktiv(t("2026-10-07 23:30")) and r.aktiv(t("2026-10-07 03:00"))
    assert not r.aktiv(t("2026-10-07 07:00")) and not r.aktiv(t("2026-10-07 21:59"))
    assert not Ruhezeit.lesen(None).aktiv(t("2026-10-07 03:00"))


def test_mitgelieferte_konfiguration_ist_gueltig():
    einst, agenten = config_laden(ROOT / "config" / "agenten.yaml")
    assert {"morgenbriefing", "termin_erinnerung", "aufgaben_faellig", "mail_waechter", "tagesrueckblick",
            "offline_hinweis"} <= set(agenten)
    assert einst.ruhezeit.aktiv(t("2026-10-07 23:00"))
    for a in agenten.values():
        assert a.selbststaendig <= a.werkzeuge
        assert "email.send" not in a.werkzeuge  # Mails verschickt kein Hintergrund-Agent


def test_konfiguration_lehnt_selbststaendig_ohne_werkzeug_ab():
    with pytest.raises(ValueError):
        AgentConfig.lesen("x", {"typ": "llm", "auftrag": "a", "werkzeuge": ["tasks.list"],
                                "selbststaendig": ["tasks.create"]})
    with pytest.raises(ValueError):
        AgentConfig.lesen("x", {"typ": "zaubern"})
    with pytest.raises(ValueError):
        AgentConfig.lesen("x", {"typ": "llm"})  # ohne Auftrag


def test_ungueltiger_agent_stoppt_die_anderen_nicht(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text("agenten:\n  gut: {typ: aufgaben, zeitplan: 'täglich 08:00'}\n  kaputt: {typ: llm, zeitplan: nie}\n")
    _, agenten = config_laden(f)
    assert set(agenten) == {"gut"}


def _policy() -> Policy:
    p = Policy.load(ROOT / "config" / "policy.yaml")
    p.registrieren("tasks.list", Stufe.LOW)
    p.registrieren("tasks.create", Stufe.MEDIUM)
    p.registrieren("memory.remember", Stufe.MEDIUM)
    p.registrieren("email.send", Stufe.HIGH)
    return p


def test_automatik_medium_braucht_freigabe_ausser_selbststaendig():
    p = _policy()
    ctx = Kontext(kanal="automatik", selbststaendig={"memory.remember"})
    assert p.entscheide("tasks.list", ctx)[1] is Entscheid.ERLAUBT
    assert p.entscheide("tasks.create", ctx)[1] is Entscheid.BESTAETIGUNG
    assert p.entscheide("memory.remember", ctx)[1] is Entscheid.ERLAUBT
    assert p.entscheide("email.send", Kontext(kanal="automatik", selbststaendig={"email.send"}))[1] \
        is Entscheid.BESTAETIGUNG  # high bleibt high
    # belastet schlägt selbststaendig
    ctx_b = Kontext(kanal="automatik", selbststaendig={"memory.remember"}, belastet=True)
    assert p.entscheide("memory.remember", ctx_b)[1] is Entscheid.BESTAETIGUNG
    assert p.entscheide("email.send", ctx_b)[1] is Entscheid.ABGELEHNT
    # direkte Anfragen ändern sich nicht
    assert p.entscheide("tasks.create", Kontext(kanal="chat"))[1] is Entscheid.ERLAUBT


class _Hub:
    def __init__(self):
        self.aufrufe, self.audit = [], []

    def openai_tools(self, offline=False):
        return [{"type": "function", "function": {"name": n}} for n in ("tasks_list", "tasks_create", "email_send")]

    def finden(self, name):
        from qulsi_core.hub import Werkzeug
        cap = name.replace("_", ".", 1)
        return Werkzeug(cap, "kern", "", {}) if cap in ("tasks.list", "tasks.create", "email.send") else None

    async def aufrufen(self, name, args, ctx):
        self.aufrufe.append(name)
        return "ok"

    async def _audit(self, *a):
        self.audit.append(a)

    async def offline(self):
        return False


def test_werkzeugkasten_zeigt_und_erlaubt_nur_die_liste():
    hub = _Hub()
    k = Werkzeugkasten(hub, {"tasks.list"})
    assert [x["function"]["name"] for x in k.openai_tools()] == ["tasks_list"]
    assert asyncio.run(k.aufrufen("tasks_list", {}, Kontext(kanal="automatik"))) == "ok"
    antwort = asyncio.run(k.aufrufen("email_send", {"entwurf_id": 1}, Kontext(kanal="automatik")))
    assert antwort.startswith("Fehler") and hub.aufrufe == ["tasks.list"] and len(hub.audit) == 1
