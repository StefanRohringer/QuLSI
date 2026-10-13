"""Keine Schlüssel, Tokens oder Passwörter in Logs."""

import logging

from qulsi_core.logschutz import Maske, maskieren


def test_maskieren():
    assert maskieren('"GET /api/events?key=abc123 HTTP/1.1" 200') == '"GET /api/events?key=*** HTTP/1.1" 200'
    assert maskieren("GET /api/events?t=Ab-12_x HTTP/1.1") == "GET /api/events?t=*** HTTP/1.1"
    assert maskieren("/bestaetigung/1?token=xyz&entscheid=ja") == "/bestaetigung/1?token=***&entscheid=ja"
    assert maskieren("Authorization: Bearer sk-geheim.123") == "Authorization: Bearer ***"
    assert maskieren("password=hunter2 user=anna") == "password=*** user=anna"
    assert maskieren('{"refresh_token": "1//0abcdef"}') == '{"refresh_token": "***"}'
    assert maskieren("HTTP 200 code: 200, Schlüssel geladen") == "HTTP 200 code: 200, Schlüssel geladen"


def test_filter_greift_auch_bei_args(caplog):
    lg = logging.getLogger("uvicorn.access.test")
    lg.addFilter(Maske())
    with caplog.at_level(logging.INFO):
        lg.info('%s - "%s %s"', "1.2.3.4", "GET", "/api/heute?key=qulsi-test")
    assert "qulsi-test" not in caplog.text and "key=***" in caplog.text
