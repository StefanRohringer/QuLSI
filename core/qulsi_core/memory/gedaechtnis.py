"""Das Gedächtnis von QuLSI in vier Ebenen.

  episodisch   Was ist passiert? (Timeline: Termine, Mails, Fotos, Dateien, Gespräche)
  semantisch   Was weiß QuLSI über Personen, Projekte, Dinge?
  praeferenz   Vorlieben und Gewohnheiten (daten.gewohnheit = true, vom Organisator erkannt)
  projekt      Projekte und Aufgaben

Jede Erinnerung hat Zeit, Personen, Ort und Projekt. Text liegt in Postgres, der Vektor in
Qdrant (gleiche id), Fotos und Dateien im Object Storage. Alles, was geschrieben wird, läuft
über speichern(): dort werden bekannte Personen, Orte und Projekte im Text erkannt und
verknüpft, und der Wissensgraph bekommt die Kanten.
"""

from __future__ import annotations

import json
import logging
import re
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .graph import Graph
from .store import Store
from .vectors import Vektoren
from .zeitraum import Frage, Zeitraum, frage_lesen, namen_finden

log = logging.getLogger("gedaechtnis")
ARTEN = ["episodisch", "semantisch", "praeferenz", "projekt"]
# Quellen, deren Text von außen kommt: Treffer daraus belasten das Gespräch (Policy Engine)
FREMDE_QUELLEN = {"mail", "datei"}  # eigener Kalender, Aufgaben, Gespräch sind eigene Daten
FREMD_MARKER = "fremder_inhalt"


# Timeline: was ins zugeklappte Protokoll gehört
PROTOKOLL_ARTEN = {"freigabe", "sicherheit", "konto", "memchip", "offline", "online", "privacy", "system", "vergessen",
                   "warnung"}
LESE_WERKZEUGE = {"read", "search", "list", "find", "timeline", "get_state", "analyze", "capture", "sync", "open",
                  "status"}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w ]", " ", text.lower()).split())[:120]


def _passt(frage: str, text: str) -> bool:
    """Teilt der Text mindestens ein Inhaltswort mit der Frage? (Wortanfang, 5 Zeichen, auch in Zusammensetzungen)"""
    from ..policy import _woerter
    flach = re.sub(r"\W", "", text.lower())
    return any(w[:5] in flach for w in _woerter(frage) if not w.isdigit() and len(w) >= 4)


def entfernung_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class Gedaechtnis:
    def __init__(self, store: Store, vektoren: Vektoren, graph: Graph, tz: str = "Europe/Berlin"):
        self.store = store
        self.vek = vektoren
        self.graph = graph
        self.tz = ZoneInfo(tz)
        self._namen: tuple[float, dict, dict, dict] | None = None
        self._bereit = False

    async def bereit(self) -> None:
        """Schema einmalig nachziehen (main.py macht das beim Start, Tests und Werkzeuge verlassen sich nicht darauf)."""
        if not self._bereit:
            from .schema import migrieren

            await migrieren(self.store)
            self._bereit = True

    def zeit(self, ts: datetime | None, mit_datum: bool = True) -> str | None:
        if not ts:
            return None
        return ts.astimezone(self.tz).strftime("%d.%m.%Y %H:%M" if mit_datum else "%H:%M")

    # ------------------------------------------------------------------ Namen (Personen, Orte, Projekte)

    async def namen(self, frisch: bool = False) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        """Alias (klein) -> Name für Personen, Orte, Projekte. 60 s gecacht."""
        await self.bereit()
        if self._namen and not frisch and time.monotonic() - self._namen[0] < 60:
            return self._namen[1], self._namen[2], self._namen[3]
        out = []
        for tabelle in ("personen", "orte"):
            d: dict[str, str] = {}
            for r in await self.store.alle(f"SELECT name, aliase FROM {tabelle}"):
                for a in [r["name"], *(r["aliase"] or [])]:
                    d[a.lower()] = r["name"]
                # "Max Muster" wird auch über "Max" gefunden, wenn der Vorname eindeutig ist
                if tabelle == "personen" and " " in r["name"]:
                    vor = r["name"].split()[0].lower()
                    d.setdefault(vor, r["name"])
            out.append(d)
        out.append({r["name"].lower(): r["name"] for r in await self.store.alle("SELECT name FROM projekte")})
        self._namen = (time.monotonic(), *out)
        return out[0], out[1], out[2]

    def _vergessen_cache(self) -> None:
        self._namen = None

    async def person_id(self, name: str, anlegen: bool = True) -> int | None:
        row = await self.store.eine(
            "SELECT id FROM personen WHERE lower(name) = lower(%(n)s) OR lower(%(n)s) = ANY(SELECT lower(a) FROM unnest(aliase) a)",
            {"n": name})
        if row or not anlegen:
            return row and row["id"]
        self._vergessen_cache()
        return (await self.store.eine(
            "INSERT INTO personen (name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            (name,)))["id"]

    async def person_per_mail(self, mail: str) -> dict | None:
        return await self.store.eine("SELECT id, name FROM personen WHERE lower(kontakt->>'mail') = lower(%s)", (mail,))

    async def projekt_id(self, name: str) -> int:
        row = await self.store.eine("SELECT id FROM projekte WHERE lower(name) = lower(%s)", (name,))
        if row:
            return row["id"]
        self._vergessen_cache()
        return (await self.store.eine("INSERT INTO projekte (name) VALUES (%s) RETURNING id", (name,)))["id"]

    async def ort_id(self, name: str, lat: float | None = None, lon: float | None = None, art: str | None = None,
                     aliase: list[str] | None = None) -> int:
        row = await self.store.eine(
            "SELECT id FROM orte WHERE lower(name) = lower(%(n)s) OR lower(%(n)s) = ANY(SELECT lower(a) FROM unnest(aliase) a)",
            {"n": name})
        if row:
            if lat is not None or art or aliase:
                await self.store.eine(
                    """UPDATE orte SET lat = coalesce(%s, lat), lon = coalesce(%s, lon), art = coalesce(%s, art),
                       aliase = (SELECT array(SELECT DISTINCT unnest(aliase || %s::text[]))) WHERE id = %s RETURNING id""",
                    (lat, lon, art, aliase or [], row["id"]))
                self._vergessen_cache()
            return row["id"]
        self._vergessen_cache()
        return (await self.store.eine(
            "INSERT INTO orte (name, lat, lon, art, aliase) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (name, lat, lon, art, aliase or [])))["id"]

    async def ort_bei(self, lat: float, lon: float) -> dict | None:
        """Nächster bekannter Ort, in dessen Radius die Koordinaten liegen."""
        beste = None
        for o in await self.store.alle("SELECT id, name, lat, lon, radius_m FROM orte WHERE lat IS NOT NULL"):
            d = entfernung_m(lat, lon, o["lat"], o["lon"])
            if d <= o["radius_m"] and (beste is None or d < beste[0]):
                beste = (d, o)
        return beste and beste[1]

    # ------------------------------------------------------------------ Schreiben

    async def speichern(self, text: str, art: str = "episodisch", zeit: datetime | None = None,
                        personen: list[str] | None = None, projekt: str | None = None, ort: str | int | None = None,
                        quelle: str = "gespraech", quelle_ref: str | None = None, objekt_key: str | None = None,
                        daten: dict | None = None, fremd: bool | None = None, erkennen: bool = True,
                        person_ids: list[int] | None = None) -> int | None:
        """Legt eine Erinnerung an. Gibt die id zurück, None wenn quelle_ref schon da war."""
        if art not in ARTEN:
            raise ValueError(f"art muss eine von {ARTEN} sein")
        fremd = quelle in FREMDE_QUELLEN if fremd is None else fremd
        p_namen, o_namen, pr_namen = await self.namen()
        namen = list(personen or [])
        orte_im_text: list[str] = []
        if erkennen:
            gefunden, _ = namen_finden(text, p_namen)
            namen += [n for n in gefunden if n not in namen]
            orte_im_text, _ = namen_finden(text, o_namen)
            if not projekt:
                pr, _ = namen_finden(text, pr_namen)
                projekt = pr[0] if pr else None
        pids = list(dict.fromkeys([*(person_ids or []), *[await self.person_id(n) for n in namen]]))
        prid = await self.projekt_id(projekt) if projekt else None
        oid = ort if isinstance(ort, int) else (await self.ort_id(ort) if ort else None)
        if oid is None and orte_im_text:
            oid = await self.ort_id(orte_im_text[0])
        row = await self.store.eine(
            """INSERT INTO erinnerungen (art, text, person_id, projekt_id, ort_id, zeit, quelle, quelle_ref, objekt_key, daten, fremd)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (quelle_ref) DO NOTHING RETURNING id""",
            (art, text, pids[0] if pids else None, prid, oid, zeit or datetime.now(timezone.utc), quelle, quelle_ref,
             objekt_key, json.dumps(daten or {}, default=str), fremd))
        if not row:
            return None
        eid = row["id"]
        if pids:
            async with await self.store.conn() as c:
                for pid in pids:
                    await c.execute("INSERT INTO erinnerung_personen VALUES (%s, %s) ON CONFLICT DO NOTHING", (eid, pid))
        await self._graph(eid, art, pids, oid, prid)
        try:
            await self.einbetten([eid])
        except Exception as exc:  # noqa: BLE001  Embedding-Modell weg: der Organisator holt es nach
            log.warning("Erinnerung %s nicht eingebettet: %s", eid, exc)
        return eid

    async def _graph(self, eid: int, art: str, pids: list[int], oid: int | None, prid: int | None) -> None:
        if not (pids and (oid or prid or len(pids) > 1)):
            return
        rows = await self.store.alle("SELECT id, name FROM personen WHERE id = ANY(%s)", (pids,))
        namen = [r["name"] for r in rows]
        ort = oid and (await self.store.eine("SELECT name FROM orte WHERE id = %s", (oid,)))["name"]
        pr = prid and (await self.store.eine("SELECT name FROM projekte WHERE id = %s", (prid,)))["name"]
        for n in namen:
            if ort and art == "episodisch":
                await self.graph.verbinden("person", n, "war_in", "ort", ort)
            if pr:
                await self.graph.verbinden("person", n, "beteiligt_an", "projekt", pr)
            if art == "episodisch":
                for m in namen:
                    if m != n:
                        await self.graph.verbinden("person", n, "traf", "person", m)

    async def einbetten(self, ids: list[int]) -> int:
        rows = await self.store.alle(
            """SELECT e.id, e.art, e.text, e.zeit, e.quelle, e.ort_id, e.fremd, p.name AS person, pr.name AS projekt,
                      array(SELECT person_id FROM erinnerung_personen ep WHERE ep.erinnerung_id = e.id) AS personen
               FROM erinnerungen e LEFT JOIN personen p ON p.id = e.person_id LEFT JOIN projekte pr ON pr.id = e.projekt_id
               WHERE e.id = ANY(%s)""", (ids,))
        for r in rows:
            await self.vek.speichern("erinnerungen", r["id"], r["text"], {
                "art": r["art"], "person": r["person"], "projekt": r["projekt"], "personen": r["personen"],
                "ort_id": r["ort_id"], "quelle": r["quelle"], "ts": int(r["zeit"].timestamp())})
        if rows:
            await self.store.eine("UPDATE erinnerungen SET eingebettet = true WHERE id = ANY(%s) RETURNING 1", ([r["id"] for r in rows],))
        return len(rows)

    async def nachholen(self, anzahl: int = 100) -> int:
        """Erinnerungen ohne Vektor einbetten (nach Ausfall des Embedding-Modells oder Import)."""
        await self.bereit()
        ids = [r["id"] for r in await self.store.alle(
            "SELECT id FROM erinnerungen WHERE NOT eingebettet ORDER BY id LIMIT %s", (anzahl,))]
        return await self.einbetten(ids) if ids else 0

    async def vergessen(self, id: int) -> str:
        await self.bereit()
        row = await self.store.eine("DELETE FROM erinnerungen WHERE id = %s RETURNING text", (id,))
        if not row:
            return "Erinnerung nicht gefunden."
        await self.store.eine("UPDATE fotos SET erinnerung_id = NULL WHERE erinnerung_id = %s RETURNING 1", (id,))
        try:
            await self.vek.loeschen_ids("erinnerungen", [id])
        except Exception as exc:  # noqa: BLE001
            log.warning("Vektor %s nicht gelöscht: %s", id, exc)
        await self.store.ereignis("vergessen", f"Erinnerung gelöscht: {row['text'][:80]}")
        return f"Vergessen: {row['text'][:120]}"

    # ------------------------------------------------------------------ Finden (Person, Zeit, Ort)

    async def _aufloesen(self, wert: str | None, namen: dict[str, str], tabelle: str) -> str | None:
        if not wert:
            return None
        if wert.lower() in namen:
            return namen[wert.lower()]
        row = await self.store.eine(
            f"""SELECT name FROM {tabelle} WHERE similarity(lower(name), lower(%(n)s)) > 0.4
                ORDER BY similarity(lower(name), lower(%(n)s)) DESC LIMIT 1""", {"n": wert})
        return row and row["name"]

    async def frage(self, frage: str = "", person: str | None = None, ort: str | None = None,
                    von: str | None = None, bis: str | None = None, projekt: str | None = None) -> tuple[Frage, list[str]]:
        """Frage + ausdrückliche Filter zu einer Frage zusammenführen. Zweiter Wert: unbekannte Orte (Textsuche)."""
        p_namen, o_namen, pr_namen = await self.namen()
        f = frage_lesen(frage or "", datetime.now(self.tz), p_namen, o_namen, pr_namen)
        unbekannt = []
        if person and (n := await self._aufloesen(person, p_namen, "personen")) and n not in f.personen:
            f.personen.append(n)
        elif person and not f.personen:
            f.personen.append(person)  # unbekannte Person: findet dann nichts, statt alles
        if ort:
            n = await self._aufloesen(ort, o_namen, "orte")
            if n:
                f.orte.append(n)
            else:
                unbekannt.append(ort)
        if projekt and (n := await self._aufloesen(projekt, pr_namen, "projekte")):
            f.projekte.append(n)
        if von or bis:
            v = datetime.combine(date.fromisoformat(von), datetime.min.time(), self.tz) if von else datetime(1970, 1, 1, tzinfo=self.tz)
            b = (datetime.combine(date.fromisoformat(bis) + timedelta(days=1), datetime.min.time(), self.tz)
                 if bis else datetime.now(self.tz) + timedelta(days=1))
            f.zeitraum = Zeitraum(v, b, f"{von or '…'} bis {bis or 'heute'}")
        return f, unbekannt

    async def _kandidaten(self, f: Frage, unbekannte_orte: list[str], art: str | None, limit: int = 300) -> list[dict]:
        orte = await self.store.alle("SELECT id FROM orte WHERE name = ANY(%s)", (f.orte,)) if f.orte else []
        muster = [f"%{o}%" for o in [*f.orte, *unbekannte_orte]]
        return await self.store.alle(
            """SELECT e.id, e.art, e.text, e.zeit, e.quelle, e.fremd, e.objekt_key, o.name AS ort, pr.name AS projekt,
                      array(SELECT pp.name FROM erinnerung_personen ep JOIN personen pp ON pp.id = ep.person_id
                            WHERE ep.erinnerung_id = e.id) AS personen, p.name AS person
               FROM erinnerungen e
               LEFT JOIN orte o ON o.id = e.ort_id
               LEFT JOIN personen p ON p.id = e.person_id
               LEFT JOIN projekte pr ON pr.id = e.projekt_id
               WHERE (%(von)s::timestamptz IS NULL OR e.zeit >= %(von)s)
                 AND (%(bis)s::timestamptz IS NULL OR e.zeit < %(bis)s)
                 AND (%(art)s::text IS NULL OR e.art = %(art)s)
                 AND NOT EXISTS (  -- jede gesuchte Person muss vorkommen
                     SELECT lower(x) FROM unnest(%(personen)s::text[]) x
                     EXCEPT
                     SELECT lower(pp.name) FROM personen pp
                     WHERE pp.id = e.person_id OR pp.id IN (SELECT person_id FROM erinnerung_personen WHERE erinnerung_id = e.id))
                 AND (cardinality(%(muster)s::text[]) = 0 OR e.ort_id = ANY(%(oids)s) OR e.text ILIKE ANY(%(muster)s))
                 AND (cardinality(%(projekte)s::text[]) = 0 OR lower(pr.name) = ANY(%(projekte)s))
               ORDER BY e.zeit DESC LIMIT %(limit)s""",
            {"von": f.zeitraum and f.zeitraum.von, "bis": f.zeitraum and f.zeitraum.bis, "art": art,
             "personen": f.personen, "muster": muster, "oids": [o["id"] for o in orte],
             "projekte": [p.lower() for p in f.projekte], "limit": limit})

    async def _rangfolge(self, text: str, ids: list[int] | None, art: str | None, anzahl: int,
                         scores: dict[int, float] | None = None) -> list[int]:
        """Vektor- und Stichwortsuche, per Reciprocal Rank Fusion zusammengeführt. scores bekommt die Vektor-Ähnlichkeiten."""
        try:
            vek = await self.vek.suchen("erinnerungen", text, anzahl * 2, {"art": art} if art else None, ids)
        except Exception as exc:  # noqa: BLE001  ohne Embedding-Modell bleibt die Stichwortsuche
            log.warning("Vektorsuche nicht möglich: %s", exc)
            vek = []
        if scores is not None:
            scores.update({int(t["id"]): t.get("score", 0.0) for t in vek})
        voll = await self.store.alle(
            """SELECT id FROM erinnerungen WHERE tsv @@ websearch_to_tsquery('german', %(q)s)
               AND (%(art)s::text IS NULL OR art = %(art)s) AND (%(ids)s::bigint[] IS NULL OR id = ANY(%(ids)s))
               ORDER BY ts_rank(tsv, websearch_to_tsquery('german', %(q)s)) DESC LIMIT %(n)s""",
            {"q": text, "art": art, "ids": ids, "n": anzahl * 2})
        rrf: dict[int, float] = {}
        for liste in ([int(t["id"]) for t in vek], [v["id"] for v in voll]):
            for rang, i in enumerate(liste):
                rrf[i] = rrf.get(i, 0) + 1 / (60 + rang)
        return sorted(rrf, key=rrf.get, reverse=True)[:anzahl]

    def _treffer(self, r: dict) -> dict:
        personen = list(dict.fromkeys([*(r.get("personen") or []), *([r["person"]] if r.get("person") else [])]))
        out = {"id": r["id"], "zeit": self.zeit(r["zeit"]), "art": r["art"], "text": r["text"]}
        for k, v in (("personen", personen), ("ort", r.get("ort")), ("projekt", r.get("projekt")), ("quelle", r.get("quelle"))):
            if v:
                out[k] = v
        if r.get("objekt_key"):
            out["bild"] = f"/api/objekt/{r['objekt_key']}"
        return out

    async def finden(self, frage: str = "", person: str | None = None, ort: str | None = None, von: str | None = None,
                     bis: str | None = None, art: str | None = None, projekt: str | None = None, anzahl: int = 10) -> dict:
        await self.bereit()
        f, unbekannt = await self.frage(frage, person, ort, von, bis, projekt)
        anzahl = max(1, min(int(anzahl or 10), 50))
        gefiltert = bool(f.zeitraum or f.personen or f.orte or unbekannt or f.projekte)
        suchtext = " ".join(f.stichworte)
        if gefiltert:
            kand = await self._kandidaten(f, unbekannt, art)
            if suchtext and len(kand) > anzahl:
                reihen = await self._rangfolge(suchtext, [k["id"] for k in kand], art, anzahl)
                nach_id = {k["id"]: k for k in kand}
                # was die Suche nicht trifft, kommt chronologisch dahinter
                rows = [nach_id[i] for i in reihen] + [k for k in kand if k["id"] not in reihen]
            else:
                rows = kand
            gesamt = len(kand)
            rows = rows[:anzahl]
        else:
            ids = await self._rangfolge(suchtext or frage, None, art, anzahl) if (suchtext or frage) else []
            rows = await self._zeilen(ids) if ids else await self._kandidaten(f, [], art, anzahl)
            gesamt = len(rows)
        out: dict[str, Any] = {}
        if any(r["fremd"] for r in rows):
            out[FREMD_MARKER] = True  # Policy: Text aus Mail/Datei/Kalender steht jetzt im Gespräch
        out.update({
            "verstanden": {k: v for k, v in {
                "zeitraum": f.zeitraum and f.zeitraum.text, "personen": f.personen, "orte": f.orte + unbekannt,
                "projekte": f.projekte, "stichworte": f.stichworte} .items() if v},
            "anzahl": gesamt,
            "treffer": [self._treffer(r) for r in rows],
        })
        if not rows:
            out["hinweis"] = "Nichts gefunden. Eventuell den Zeitraum weiter fassen oder memory.search nutzen."
        return out

    async def _zeilen(self, ids: list[int]) -> list[dict]:
        rows = await self.store.alle(
            """SELECT e.id, e.art, e.text, e.zeit, e.quelle, e.fremd, e.objekt_key, o.name AS ort, pr.name AS projekt, p.name AS person,
                      array(SELECT pp.name FROM erinnerung_personen ep JOIN personen pp ON pp.id = ep.person_id
                            WHERE ep.erinnerung_id = e.id) AS personen
               FROM erinnerungen e LEFT JOIN orte o ON o.id = e.ort_id LEFT JOIN personen p ON p.id = e.person_id
               LEFT JOIN projekte pr ON pr.id = e.projekt_id WHERE e.id = ANY(%s)""", (ids,))
        nach_id = {r["id"]: r for r in rows}
        return [nach_id[i] for i in ids if i in nach_id]

    async def suchen(self, suche: str, art: str | None = None, anzahl: int = 5) -> list[dict] | dict:
        """memory.search: Bedeutung + Stichworte über alle Erinnerungen, dazu Dokumente aus dem Dateien-Connector."""
        await self.bereit()
        scores: dict[int, float] = {}
        rows = await self._zeilen(await self._rangfolge(suche, None, art, anzahl, scores))
        # Erinnerungen aus Mails/Dateien nur, wenn sie wirklich zur Frage passen: sonst stünde bei jeder
        # allgemeinen Frage („was weißt du über mich?“) fremder Text im Gespräch
        rows = [r for r in rows if not r["fremd"] or _passt(suche, r["text"])]
        out = [{**self._treffer(r), "zeit": self.zeit(r["zeit"])} for r in rows]
        fremd = any(r["fremd"] for r in rows)
        if not art:
            try:
                beste = max(scores.values(), default=0.0)
                vorne = 0
                for d in await self.vek.suchen("dokumente", suche, 3):
                    # Nur Dokumente, die wirklich zur Frage passen: sonst stünde bei jeder Suche Dateitext im
                    # Gespräch, und alles gälte als fremder Inhalt
                    if not _passt(suche, f"{d.get('titel') or ''} {d.get('text') or ''}"):
                        continue
                    treffer = {"art": "dokument", "titel": d.get("titel"), "quelle": d.get("quelle"), "text": d.get("text", "")[:500]}
                    # Ein Dokument, das besser passt als jede Erinnerung, kommt nach vorne (Reihenfolge = Relevanz)
                    if d.get("score", 0.0) > beste:
                        out.insert(vorne, treffer)
                        vorne += 1
                    else:
                        out.append(treffer)
                    fremd = True
            except Exception as exc:  # noqa: BLE001
                log.warning("Dokumentsuche nicht möglich: %s", exc)
        return {FREMD_MARKER: True, "treffer": out} if fremd else out

    # ------------------------------------------------------------------ Timeline

    async def timeline(self, tag: str | None = None, von: str | None = None, bis: str | None = None,
                       person: str | None = None, ort: str | None = None) -> list[dict] | dict:
        """Chronologisch: Ereignisse, Termine und Erinnerungen. Ohne Angaben: heute."""
        await self.bereit()
        if person or ort:
            r = await self.finden("", person=person, ort=ort, von=von or tag, bis=bis or tag, anzahl=50)
            r["treffer"].sort(key=lambda t: datetime.strptime(t["zeit"], "%d.%m.%Y %H:%M"))
            return r
        if von or bis:
            start = datetime.combine(date.fromisoformat(von or bis), datetime.min.time(), self.tz)
            ende = datetime.combine(date.fromisoformat(bis or von) + timedelta(days=1), datetime.min.time(), self.tz)
            ende = min(ende, start + timedelta(days=31))
        else:
            d = date.fromisoformat(tag) if tag else datetime.now(self.tz).date()
            start = datetime.combine(d, datetime.min.time(), self.tz)
            ende = start + timedelta(days=1)
        ein_tag = ende - start <= timedelta(days=1)
        ev = await self.store.alle(
            """SELECT zeit, art, text, daten, 'ereignis' AS herkunft FROM ereignisse WHERE zeit >= %s AND zeit < %s
               AND art NOT IN ('werkzeug_lesen', 'kalender_sync', 'organisiert') ORDER BY zeit LIMIT 500""", (start, ende))
        te = await self.store.alle(
            """SELECT start AS zeit, 'termin' AS art, titel AS text, '{}'::jsonb AS daten, 'termin' AS herkunft
               FROM termine WHERE start >= %s AND start < %s LIMIT 200""", (start, ende))
        # Erinnerungen aus Terminen stehen schon als Termin drin; der Rückblick des Organisators ist fürs
        # Suchen da, in der Timeline steht der Tagesrückblick des Agents (sonst stünde der Tag dreimal da)
        er = await self.store.alle(
            """SELECT zeit, art, text, daten, 'erinnerung' AS herkunft FROM erinnerungen WHERE zeit >= %s AND zeit < %s
               AND art = 'episodisch' AND quelle NOT IN ('kalender', 'rueckblick') ORDER BY zeit LIMIT 300""", (start, ende))
        alle = sorted(ev + te + er, key=lambda r: r["zeit"])
        return [{"zeit": self.zeit(r["zeit"], not ein_tag), "art": r["art"], "text": r["text"], **self._einordnen(r)}
                for r in alle]

    def _einordnen(self, r: dict) -> dict:
        """werkzeug, gruppe (inhalt|protokoll), schluessel (gleich = dasselbe Ereignis) und datum für die App."""
        daten = r.get("daten") or {}
        zeit = r["zeit"].astimezone(self.tz)
        werkzeug = daten.get("werkzeug") if isinstance(daten, dict) else None
        art, text = r["art"], r["text"] or ""
        if art in PROTOKOLL_ARTEN or (art == "agent" and text.endswith(("eingeschaltet", "ausgeschaltet"))):
            gruppe = "protokoll"
        elif werkzeug and werkzeug.rsplit(".", 1)[-1] in LESE_WERKZEUGE:
            gruppe = "protokoll"
        else:
            gruppe = "inhalt"
        schluessel = None
        if r["herkunft"] == "termin":
            schluessel = f"termin:{text.strip().lower()}:{zeit:%d.%m}"
        elif art == "termin" and (m := re.match(r"Termin eingetragen: (.+) am (\d\d\.\d\d)\.", text)):
            schluessel = f"termin:{m[1].strip().lower()}:{m[2]}"
        elif r["herkunft"] == "erinnerung":
            schluessel = f"notiz:{_norm(text)}"
        elif werkzeug == "memory.remember" and daten.get("inhalt"):
            schluessel = f"notiz:{_norm(daten['inhalt'])}"
        elif art == "agent" and str(r.get("quelle", "")).endswith("tagesrueckblick") or text.startswith("Tagesrückblick:"):
            schluessel = f"rueckblick:{zeit:%Y-%m-%d}"
        return {"werkzeug": werkzeug, "gruppe": gruppe, "schluessel": schluessel, "datum": zeit.date().isoformat()}

    # ------------------------------------------------------------------ Orte

    async def orte_speichern(self, name: str, lat: float | None = None, lon: float | None = None, art: str | None = None,
                             aliase: list[str] | None = None, radius_m: int | None = None) -> str:
        await self.bereit()
        oid = await self.ort_id(name, lat, lon, art, aliase)
        if radius_m:
            await self.store.eine("UPDATE orte SET radius_m = %s WHERE id = %s RETURNING 1", (radius_m, oid))
        if lat is not None and lon is not None:
            # Fotos ohne Ort, die in den Radius fallen, bekommen den Ort nachträglich
            fotos = await self.store.alle("SELECT id, lat, lon, erinnerung_id FROM fotos WHERE ort_id IS NULL AND lat IS NOT NULL")
            r = radius_m or (await self.store.eine("SELECT radius_m FROM orte WHERE id = %s", (oid,)))["radius_m"]
            treffer = [f for f in fotos if entfernung_m(lat, lon, f["lat"], f["lon"]) <= r]
            for f in treffer:
                await self.store.eine("UPDATE fotos SET ort_id = %s WHERE id = %s RETURNING 1", (oid, f["id"]))
                if f["erinnerung_id"]:
                    await self.store.eine("UPDATE erinnerungen SET ort_id = %s, eingebettet = false WHERE id = %s RETURNING 1",
                                          (oid, f["erinnerung_id"]))
            if treffer:
                return f"Ort {name} gespeichert, {len(treffer)} Fotos zugeordnet."
        return f"Ort {name} gespeichert."

    async def orte_liste(self) -> list[dict]:
        await self.bereit()
        return await self.store.alle(
            """SELECT o.name, o.art, o.aliase, (SELECT count(*) FROM erinnerungen e WHERE e.ort_id = o.id) AS erinnerungen
               FROM orte o ORDER BY 4 DESC, o.name LIMIT 100""")
