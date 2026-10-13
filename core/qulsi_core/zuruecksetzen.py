"""Persönliche Daten anzeigen und löschen (z. B. Einträge aus Testgesprächen).

    python -m qulsi_core.zuruecksetzen zeigen            was gespeichert ist (Anzahl und die neuesten Einträge)
    python -m qulsi_core.zuruecksetzen suchen "<text>"   Einträge löschen, die den Text enthalten (fragt nach)
    python -m qulsi_core.zuruecksetzen leeren            alles Persönliche löschen (fragt nach "JA")

Gelöscht werden Erinnerungen (auch im Vektorspeicher), lokale Termine, Aufgaben, Personen, Projekte, Orte,
Beziehungen, Mail-Entwürfe, die Timeline (Ereignisse) und der gespeicherte Nutzername. Termine aus
verbundenen Kalendern, Dokumente aus Ordnern und Fotos kommen beim nächsten Abgleich wieder; das ist gewollt.
Einstellungen, Freigabe-PIN, Protokoll (audit_log) und Kamera-Gegenstände bleiben.
Danach den Core neu starten, damit er den Nutzernamen neu lädt.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import Settings
from .memory import Store, Vektoren

# (Tabelle, Bedingung für "persönlich", Textspalte für Suche und Anzeige)
TABELLEN = [
    ("erinnerungen", "true", "text"),
    ("termine", "quelle = 'lokal'", "titel"),
    ("aufgaben", "true", "titel"),
    ("personen", "true", "name"),
    ("projekte", "true", "name"),
    ("orte", "true", "name"),
    ("beziehungen", "true", "von || ' ' || relation || ' ' || zu"),
    ("mail_entwuerfe", "true", "betreff"),
    ("ereignisse", "true", "text"),
    ("gespraeche", "true", "frage || ' ' || antwort"),
    ("hochgeladen", "true", "name"),
]
SUCHBAR = ("erinnerungen", "termine", "aufgaben", "gespraeche")


async def _vorhanden(store: Store) -> set[str]:
    rows = await store.alle("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
    return {r["table_name"] for r in rows}


async def zeigen(store: Store, anzahl: int = 5) -> list[str]:
    zeilen, da = [], await _vorhanden(store)
    for tab, wo, text in TABELLEN:
        if tab not in da:
            continue
        n = (await store.eine(f"SELECT count(*) AS n FROM {tab} WHERE {wo}"))["n"]
        zeilen.append(f"{tab}: {n}")
        if n and tab != "ereignisse":
            for r in await store.alle(f"SELECT id, {text} AS t FROM {tab} WHERE {wo} ORDER BY id DESC LIMIT %s", (anzahl,)):
                zeilen.append(f"  #{r['id']} {str(r['t'] or '')[:90]}")
    name = await store.eine("SELECT value FROM system_state WHERE key = 'nutzer'")
    zeilen.append(f"Nutzername: {((name or {}).get('value') or {}).get('name') or '(keiner)'}")
    return zeilen


async def treffer(store: Store, suche: str) -> dict[str, list[dict]]:
    out = {}
    for tab, wo, text in TABELLEN:
        if tab in SUCHBAR:
            rows = await store.alle(f"SELECT id, {text} AS t FROM {tab} WHERE {wo} AND {text} ILIKE %s ORDER BY id",
                                    (f"%{suche}%",))
            if rows:
                out[tab] = rows
    return out


async def _vektoren_loeschen(vek: Vektoren | None, ids: list[int]) -> None:
    if vek and ids:
        try:
            await vek.loeschen_ids("erinnerungen", ids)
        except Exception as exc:  # noqa: BLE001  Qdrant aus: die Suche filtert gelöschte Zeilen ohnehin über Postgres
            print(f"Hinweis: Vektorspeicher nicht erreichbar ({exc})", file=sys.stderr)


async def suchen_loeschen(store: Store, vek: Vektoren | None, gefunden: dict[str, list[dict]]) -> int:
    n = 0
    async with await store.conn() as conn:
        for tab, rows in gefunden.items():
            ids = [r["id"] for r in rows]
            await conn.execute(f"DELETE FROM {tab} WHERE id = ANY(%s)", (ids,))
            n += len(ids)
    await _vektoren_loeschen(vek, [r["id"] for r in gefunden.get("erinnerungen", [])])
    return n


async def leeren(store: Store, vek: Vektoren | None) -> int:
    da = await _vorhanden(store)
    ids = [r["id"] for r in await store.alle("SELECT id FROM erinnerungen")]
    n = 0
    async with await store.conn() as conn:
        if "fotos" in da:
            await conn.execute("UPDATE fotos SET erinnerung_id = NULL, ort_id = NULL")
        for tab, wo, _ in TABELLEN:
            if tab in da:
                cur = await conn.execute(f"DELETE FROM {tab} WHERE {wo}")
                n += cur.rowcount
        await conn.execute("DELETE FROM system_state WHERE key = 'nutzer'")
    await _vektoren_loeschen(vek, ids)
    return n


def _ja(frage: str, wort: str, ja: bool) -> bool:
    if ja:
        return True
    try:
        return input(f"{frage} Zum Bestätigen {wort} eintippen: ").strip() == wort
    except EOFError:
        return False


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="qulsi_core.zuruecksetzen", description=__doc__.splitlines()[0])
    ap.add_argument("befehl", choices=["zeigen", "suchen", "leeren"])
    ap.add_argument("text", nargs="?")
    ap.add_argument("--ja", action="store_true", help="ohne Rückfrage (für Skripte)")
    a = ap.parse_args(argv)
    s = Settings()
    store = Store(s.database_url)
    vek = Vektoren(s.qdrant_url, s.llm_base_url, s.llm_api_key, s.embed_dim)
    if a.befehl == "zeigen":
        print("\n".join(await zeigen(store)))
        return 0
    if a.befehl == "suchen":
        if not a.text or len(a.text.strip()) < 3:
            print("suchen braucht einen Text mit mindestens 3 Zeichen, z. B. suchen \"Kaffee\"")
            return 2
        gefunden = await treffer(store, a.text.strip())
        if not gefunden:
            print("Nichts gefunden.")
            return 0
        for tab, rows in gefunden.items():
            for r in rows:
                print(f"{tab} #{r['id']}: {str(r['t'])[:90]}")
        if not _ja(f"{sum(map(len, gefunden.values()))} Einträge löschen?", "JA", a.ja):
            print("Abgebrochen.")
            return 1
        print(f"{await suchen_loeschen(store, vek, gefunden)} Einträge gelöscht.")
        return 0
    print("\n".join(await zeigen(store, 0)))
    if not _ja("Alle oben gezählten persönlichen Daten endgültig löschen?", "JA", a.ja):
        print("Abgebrochen.")
        return 1
    print(f"{await leeren(store, vek)} Einträge gelöscht. Core neu starten, damit er den Nutzernamen vergisst.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
