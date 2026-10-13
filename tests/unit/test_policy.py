from pathlib import Path

from qulsi_core.hub import Manifest, manifeste_laden
from qulsi_core.policy import Entscheid, Kontext, Policy, Stufe, stufe_aus_manifest

ROOT = Path(__file__).resolve().parents[2]


def policy() -> Policy:
    """Policy wie im Betrieb: Stufen aus den echten Manifesten registriert."""
    p = Policy.load(ROOT / "config" / "policy.yaml")
    for m in manifeste_laden(ROOT / "connectors").values():
        for cap in m.capabilities:
            p.registrieren(cap, stufe_aus_manifest(cap, m.permissions), m.belastend(cap))
    p.registrieren("objects.where", Stufe.LOW)
    return p


def test_stufe_aus_manifest_verb_oder_ganze_faehigkeit():
    perms = {"read": "low", "send": "high", "door.unlock": "high"}
    assert stufe_aus_manifest("email.read", perms) is Stufe.LOW
    assert stufe_aus_manifest("email.send", perms) is Stufe.HIGH
    assert stufe_aus_manifest("door.unlock", perms) is Stufe.HIGH
    assert stufe_aus_manifest("email.delete", perms) is Stufe.HIGH  # nicht genannt -> high


def test_manifeste_sind_vollstaendig():
    ms = manifeste_laden(ROOT / "connectors")
    assert set(ms) == {"home_assistant", "calendar", "mail", "files", "web", "vision",
                       "google", "microsoft", "nas", "browser"}
    for m in ms.values():
        for cap in m.capabilities:
            assert "." in cap, cap
            # jede Fähigkeit hat eine ausdrückliche Stufe (Verb oder ganzer Name)
            assert cap in m.permissions or cap.split(".", 1)[1] in m.permissions, cap


def test_lesen_ist_frei():
    assert policy().entscheide("objects.where", Kontext())[1] is Entscheid.ERLAUBT
    assert policy().entscheide("calendar.search", Kontext(kanal="stimme"))[1] is Entscheid.ERLAUBT


def test_medium_frei_bei_direkter_anfrage():
    assert policy().entscheide("home.set_state", Kontext(kanal="stimme"))[1] is Entscheid.ERLAUBT
    assert policy().entscheide("email.draft", Kontext())[1] is Entscheid.ERLAUBT


def test_high_braucht_immer_handy():
    p = policy()
    for kanal in ("chat", "stimme", "app", "automatik"):
        assert p.entscheide("email.send", Kontext(kanal=kanal))[1] is Entscheid.BESTAETIGUNG
        assert p.entscheide("door.unlock", Kontext(kanal=kanal))[1] is Entscheid.BESTAETIGUNG


def test_unbekanntes_werkzeug_gilt_als_high():
    stufe, entscheid, _ = policy().entscheide("irgendwas.neues", Kontext())
    assert stufe is Stufe.HIGH and entscheid is Entscheid.BESTAETIGUNG


def test_belasteter_kontext_sperrt_high_und_bremst_medium():
    p = policy()
    ctx = Kontext(belastet=True, belastet_durch=["email.read"])
    assert p.entscheide("email.send", ctx)[1] is Entscheid.ABGELEHNT
    assert p.entscheide("door.unlock", ctx)[1] is Entscheid.ABGELEHNT
    assert p.entscheide("home.set_state", ctx)[1] is Entscheid.BESTAETIGUNG
    assert p.entscheide("objects.where", ctx)[1] is Entscheid.ERLAUBT


def test_untrusted_aus_manifest_belastet():
    p = policy()
    assert p.belastet_nach("email.read")
    assert p.belastet_nach("browser.search")
    assert p.belastet_nach("files.read")
    assert not p.belastet_nach("email.draft")
    assert not p.belastet_nach("calendar.search")


def test_override_deny_ist_unsichtbar(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("stufen:\n  door.unlock: deny\n  home.set_state: high\n")
    p = Policy.load(f)
    p.registrieren("door.unlock", Stufe.HIGH)
    p.registrieren("home.set_state", Stufe.MEDIUM)
    assert not p.sichtbar("door.unlock")
    assert p.entscheide("door.unlock", Kontext())[1] is Entscheid.ABGELEHNT
    assert p.entscheide("home.set_state", Kontext())[1] is Entscheid.BESTAETIGUNG


def test_offline_und_untrusted_als_liste_oder_bool():
    m = Manifest("x", "X", "", ["a.b", "a.c"], untrusted=["a.b"], offline=True)
    assert m.belastend("a.b") and not m.belastend("a.c")
    assert m.offline_faehig("a.c")


def test_rate_limit_je_stufe_und_werkzeug():
    p = policy()
    p.limits_stufe = {Stufe.HIGH: 2}
    p.limits_werkzeug = {"calendar.search": 1}
    assert p.zaehlen("door.unlock", jetzt=0) is None
    assert p.zaehlen("door.unlock", jetzt=1) is None
    assert "Rate-Limit" in p.zaehlen("door.unlock", jetzt=2)
    assert p.zaehlen("door.unlock", jetzt=3601) is None  # nach einer Stunde wieder frei
    assert p.zaehlen("calendar.search", jetzt=0) is None
    assert p.zaehlen("calendar.search", jetzt=1) is not None


def test_limits_aus_policy_yaml():
    p = Policy.load(ROOT / "config" / "policy.yaml")
    assert p.limits_stufe[Stufe.HIGH] == 10 and p.limits_stufe[Stufe.LOW] == 600


def test_manifest_false_heisst_verboten():
    perms = {"read": "low", "send_email": False, "calendar.delete": False}
    assert stufe_aus_manifest("google.read", perms) is Stufe.LOW
    assert stufe_aus_manifest("x.send.email", perms) is Stufe.DENY
    assert stufe_aus_manifest("calendar.delete", perms) is Stufe.DENY
