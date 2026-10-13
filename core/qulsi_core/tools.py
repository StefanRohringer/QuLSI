"""Kern-Werkzeuge von QuLSI: Memory, Personen, Projekte, Aufgaben, Objekte, Timeline.

Sie laufen im Core selbst (kein Connector, kein Internet) und gehen trotzdem durch die
Policy Engine wie jedes andere Werkzeug.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from . import feiertage
from .memory import Graph, ObjectStore, Store, Vektoren
from .memory.gedaechtnis import Gedaechtnis
from .memory.memchip import Memchip
from .memory.organisator import Organisator
from .memory.werkzeuge import registrieren as gedaechtnis_werkzeuge
from .policy import Stufe

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Berlin"))


def _vokabular() -> dict[str, str]:
    """Erkennungsbegriffe von Vision (englisch -> deutsch), damit "Schlüssel" auch "keys" findet."""
    import yaml
    from pathlib import Path

    pfad = Path(os.environ.get("CAMERAS_CONFIG", "/config/cameras.yaml"))
    try:
        return dict((yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}).get("vokabular") or {})
    except (OSError, ValueError):
        return {}


def labels_fuer(objekt: str, vokabular: dict[str, str]) -> list[str]:
    """Mögliche Vision-Labels für einen deutschen oder englischen Begriff ("Tassen" -> cup)."""
    q = objekt.lower().strip()
    out = [q]
    for label, deutsch in vokabular.items():
        d = deutsch.lower()
        if q in (label.lower(), d) or q.startswith(d) or d.startswith(q):
            out.append(label.lower())
    return list(dict.fromkeys(out))
STALE_MINUTEN = int(os.environ.get("STALE_MINUTES", "30"))
ARTEN = ["episodisch", "semantisch", "praeferenz", "projekt"]


@dataclass
class KernWerkzeug:
    name: str  # Fähigkeit mit Punkt, z. B. memory.search
    stufe: Stufe
    beschreibung: str
    schema: dict
    fn: Callable[..., Awaitable[Any]]


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}


def zeit(ts: datetime | None) -> str | None:
    return ts.astimezone(TZ).strftime("%d.%m.%Y %H:%M") if ts else None


class Kern:
    def __init__(self, store: Store, vektoren: Vektoren, graph: Graph):
        self.store = store
        self.vek = vektoren
        self.graph = graph
        self.werkzeuge: dict[str, KernWerkzeug] = {}
        self.nutzer_name: str | None = os.environ.get("QULSI_NUTZER", "").strip() or None  # nutzer_laden() beim Start
        self.bundesland: str | None = feiertage.land(os.environ.get("QULSI_BUNDESLAND"))
        self.vokabular = _vokabular()
        # Gedächtnis (vier Ebenen, Timeline, Orte, Organisator, Memchip): siehe memory/gedaechtnis.py
        self.gedaechtnis = Gedaechtnis(store, vektoren, graph, os.environ.get("TZ", "Europe/Berlin"))
        objekte = ObjectStore()
        self.memchip = Memchip(self.gedaechtnis, objekte)
        self.organisator = Organisator(self.gedaechtnis, None, objekte, memchip=self.memchip)  # hub setzt main.py
        r = self._reg
        r("memory.search", Stufe.LOW, "Durchsucht das Gedächtnis (Erinnerungen, Vorlieben, Dokumente) nach Bedeutung und Stichworten.",
          _schema({"suche": S, "art": {"type": "string", "enum": ARTEN}, "anzahl": I}, ["suche"]), self.memory_search)
        r("memory.remember", Stufe.MEDIUM, "Merkt sich etwas dauerhaft. art: episodisch (was passiert ist), semantisch (Wissen), praeferenz (Vorliebe), projekt.",
          _schema({"text": S, "art": {"type": "string", "enum": ARTEN}, "person": S, "projekt": S, "wann": {"type": "string", "description": "JJJJ-MM-TT, falls nicht heute"}}, ["text"]),
          self.memory_remember)
        r("memory.timeline", Stufe.LOW, "Was ist an einem Tag passiert: Ereignisse und Termine in zeitlicher Reihenfolge.",
          _schema({"tag": {"type": "string", "description": "JJJJ-MM-TT, Standard heute"}}), self.memory_timeline)
        r("persons.search", Stufe.LOW, "Alles über eine Person: Kontakt, Notizen, Erinnerungen, Beziehungen.",
          _schema({"name": S}, ["name"]), self.persons_search)
        r("persons.save", Stufe.MEDIUM, "Legt eine Person an oder ergänzt sie.",
          _schema({"name": S, "beziehung": S, "notizen": S, "mail": S, "telefon": S}, ["name"]), self.persons_save)
        r("persons.rename", Stufe.MEDIUM, "Benennt eine gespeicherte Person um (der alte Name bleibt als Alias). "
          "Erinnerungen und Beziehungen der Person bleiben erhalten.",
          _schema({"name": S, "neuer_name": S}, ["name", "neuer_name"]), self.persons_rename)
        r("projects.get", Stufe.LOW, "Ein Projekt mit Unterprojekten, Aufgaben, Erinnerungen und Beziehungen.",
          _schema({"name": S}, ["name"]), self.projects_get)
        r("projects.save", Stufe.MEDIUM, "Legt ein Projekt an, optional als Teil eines Oberprojekts.",
          _schema({"name": S, "beschreibung": S, "oberprojekt": S}, ["name"]), self.projects_save)
        r("relations.add", Stufe.MEDIUM, "Speichert eine Beziehung im Wissensgraph, z. B. Max -arbeitet_bei-> Firma X.",
          _schema({"von": S, "relation": S, "zu": S, "von_typ": S, "zu_typ": S}, ["von", "relation", "zu"]), self.relations_add)
        r("tasks.list", Stufe.LOW, "Offene (oder erledigte) Aufgaben.",
          _schema({"status": {"type": "string", "enum": ["offen", "erledigt"]}, "projekt": S}), self.tasks_list)
        r("tasks.create", Stufe.MEDIUM, "Legt eine Aufgabe an.",
          _schema({"titel": S, "faellig": {"type": "string", "description": "JJJJ-MM-TT"}, "projekt": S}, ["titel"]), self.tasks_create)
        r("tasks.done", Stufe.MEDIUM, "Hakt eine Aufgabe ab.", _schema({"id": I}, ["id"]), self.tasks_done)
        r("objects.where", Stufe.LOW, "Wo ist ein Gegenstand? Letzte Kamera-Sichtung und Bluetooth-Raum. Bei veraltet=true camera.capture nutzen.",
          _schema({"objekt": S}, ["objekt"]), self.objects_where)
        r("objects.list", Stufe.LOW, "Alle angelernten persönlichen Gegenstände.", _schema({}), self.objects_list)
        r("system.privacy", Stufe.MEDIUM, "Privacy-Modus: bei an=true wertet Vision keine Kamerabilder aus.",
          _schema({"an": B}, ["an"]), self.system_privacy)
        r("user.name_set", Stufe.MEDIUM, "Speichert, wie der Nutzer heißt. Nur aufrufen, wenn der Nutzer seinen Namen selbst nennt.",
          _schema({"name": S}, ["name"]), self.user_name_set)
        r("user.region_set", Stufe.LOW, "Speichert, in welchem Bundesland der Nutzer wohnt (für Feiertage). Nur aufrufen, "
          "wenn der Nutzer selbst sagt, wo er wohnt.",
          _schema({"bundesland": {"type": "string", "description": "Name oder Kürzel, z. B. Bayern oder BY"}},
                  ["bundesland"]), self.user_region_set)
        r("conversations.search", Stufe.LOW, "Frühere Gespräche mit dem Nutzer (Frage und Antwort, neueste zuerst). "
          "Für Fragen wie „worüber haben wir gestern gesprochen?“ oder „wo ist unser altes Gespräch?“.",
          _schema({"suche": {"type": "string", "description": "Stichwort, leer = alle"},
                   "tage": {"type": "integer", "description": "wie weit zurück, Standard 7"}, "anzahl": I}),
          self.conversations_search)
        r("holidays.list", Stufe.LOW, "Nächste gesetzliche Feiertage in Deutschland (berechnet, verlässlich, ohne Internet). "
          "Für jede Frage nach Feiertagen, langen Wochenenden oder Brückentagen.",
          _schema({"ab": {"type": "string", "description": "JJJJ-MM-TT, Standard heute"}, "anzahl": I,
                   "bundesland": {"type": "string", "description": "Kürzel oder Name, Standard aus den Einstellungen"}}),
          self.holidays_list)
        gedaechtnis_werkzeuge(r, self.gedaechtnis, self.organisator, self.memchip)

    def _reg(self, name, stufe, beschreibung, schema, fn):
        self.werkzeuge[name] = KernWerkzeug(name, stufe, beschreibung, schema, fn)

    async def _nutzer_daten(self) -> dict:
        try:
            row = await self.store.eine("SELECT value FROM system_state WHERE key = 'nutzer'")
        except Exception:  # noqa: BLE001
            row = None
        return dict((row or {}).get("value") or {})

    def _uebernehmen(self, daten: dict) -> None:
        self.nutzer_name = daten.get("name") or os.environ.get("QULSI_NUTZER", "").strip() or None
        self.bundesland = feiertage.land(daten.get("bundesland")) or feiertage.land(os.environ.get("QULSI_BUNDESLAND"))

    async def nutzer_laden(self) -> str | None:
        """Name und Bundesland des Nutzers: aus der App/dem Gespräch (system_state "nutzer"), sonst
        QULSI_NUTZER bzw. QULSI_BUNDESLAND, sonst unbekannt. Jede Box steht woanders: nichts ist fest eingebaut."""
        self._uebernehmen(await self._nutzer_daten())
        return self.nutzer_name

    async def _nutzer_speichern(self, **felder) -> dict:
        daten = await self._nutzer_daten()
        daten.update(felder)  # nur die übergebenen Felder ändern sich
        async with await self.store.conn() as conn:
            await conn.execute(
                """INSERT INTO system_state (key, value) VALUES ('nutzer', %s::jsonb)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, zeit = now()""", (json.dumps(daten),))
        self._uebernehmen(daten)
        return daten

    async def nutzer_setzen(self, name: str | None) -> str | None:
        await self._nutzer_speichern(name=" ".join((name or "").split())[:60] or None)
        return self.nutzer_name

    async def bundesland_setzen(self, land: str | None) -> str | None:
        """Kürzel (BY) oder Name (Bayern); None löscht. Unbekannt: ValueError."""
        kuerzel = feiertage.land(land) if land else None
        if land and not kuerzel:
            raise ValueError(f"Unbekanntes Bundesland: {land}")
        await self._nutzer_speichern(bundesland=kuerzel)
        return self.bundesland

    async def user_region_set(self, bundesland: str) -> str:
        try:
            kuerzel = await self.bundesland_setzen(bundesland)
        except ValueError:
            return (f"„{bundesland}“ ist kein deutsches Bundesland. Frag nach, welches gemeint ist "
                    f"({', '.join(feiertage.LAENDER.values())}).")
        return f"Gespeichert: Der Nutzer wohnt in {feiertage.LAENDER[kuerzel]}. Feiertage gelten jetzt für dieses Land."

    async def user_name_set(self, name: str) -> str:
        if not name.strip():
            return "Kein Name angegeben."
        return f"Gespeichert: Der Nutzer heißt {await self.nutzer_setzen(name)}."

    async def conversations_search(self, suche: str = "", tage: int = 7, anzahl: int = 10) -> dict:
        tage = max(1, min(int(tage or 7), 365))
        anzahl = max(1, min(int(anzahl or 10), 30))
        muster = f"%{suche.strip()}%" if suche and suche.strip() else "%"
        rows = await self.store.alle(
            """SELECT zeit, kanal, frage, antwort, belastet FROM gespraeche
               WHERE zeit > now() - make_interval(days => %s) AND (frage ILIKE %s OR antwort ILIKE %s)
               ORDER BY zeit DESC LIMIT %s""", (tage, muster, muster, anzahl))
        out: dict = {}
        if any(r["belastet"] for r in rows):
            out["fremder_inhalt"] = True  # damals stand Mail-/Web-Text im Gespräch
        out["gespraeche"] = [{"zeit": r["zeit"].astimezone(TZ).strftime("%d.%m.%Y %H:%M"), "kanal": r["kanal"],
                              "nutzer": r["frage"][:300], "qulsi": r["antwort"][:400]} for r in rows]
        if not rows:
            out["hinweis"] = (f"In den letzten {tage} Tagen ist dazu kein Gespräch gespeichert. Gespräche werden "
                              "erst seit dieser Version auf der Box gespeichert; ältere gibt es nur noch in der App, "
                              "falls sie dort noch angezeigt werden.")
        return out

    async def holidays_list(self, ab: str | None = None, anzahl: int = 5, bundesland: str | None = None) -> dict:
        bl = bundesland or self.bundesland or ""
        start = date.fromisoformat(ab) if ab else datetime.now(TZ).date()
        bekannt = feiertage.land(bl)
        return {"bundesland": feiertage.LAENDER[bekannt] if bekannt else "nicht eingestellt (alle Länder)",
                "feiertage": feiertage.naechste(start, max(1, min(int(anzahl or 5), 30)), bekannt)}

    # ------------------------------------------------------------------ Memory

    async def memory_search(self, suche: str, art: str | None = None, anzahl: int = 5) -> list[dict] | dict:
        return await self.gedaechtnis.suchen(suche, art, anzahl)

    async def memory_remember(self, text: str, art: str = "semantisch", person: str | None = None,
                              projekt: str | None = None, wann: str | None = None) -> str:
        ts = datetime.fromisoformat(wann).replace(tzinfo=TZ) if wann else None
        await self.gedaechtnis.speichern(text, art, ts, [person] if person else None, projekt, quelle="gespraech")
        return f"Gemerkt ({art})."

    async def memory_timeline(self, tag: str | None = None) -> list[dict] | dict:
        return await self.gedaechtnis.timeline(tag)

    # ------------------------------------------------------------------ Personen und Projekte

    async def _person(self, name: str) -> dict:
        row = await self.store.eine(
            "SELECT * FROM personen WHERE lower(name) = lower(%s) OR lower(%s) = ANY(SELECT lower(a) FROM unnest(aliase) a)",
            (name, name))
        return row or await self.store.eine("INSERT INTO personen (name) VALUES (%s) RETURNING *", (name,))

    async def _projekt(self, name: str) -> dict:
        row = await self.store.eine("SELECT * FROM projekte WHERE lower(name) = lower(%s)", (name,))
        return row or await self.store.eine("INSERT INTO projekte (name) VALUES (%s) RETURNING *", (name,))

    async def persons_search(self, name: str) -> dict:
        p = await self.store.eine(
            """SELECT * FROM personen WHERE lower(name) = lower(%(n)s) OR lower(%(n)s) = ANY(SELECT lower(a) FROM unnest(aliase) a)
               OR similarity(lower(name), lower(%(n)s)) > 0.4 ORDER BY similarity(lower(name), lower(%(n)s)) DESC LIMIT 1""",
            {"n": name})
        if not p:
            return {"gefunden": False, "hinweis": f"Keine Person namens {name} bekannt."}
        erinn = await self.store.alle(
            "SELECT art, text, zeit FROM erinnerungen WHERE person_id = %s ORDER BY zeit DESC LIMIT 10", (p["id"],))
        return {
            "name": p["name"], "beziehung": p["beziehung"], "kontakt": p["kontakt"], "notizen": p["notizen"],
            "erinnerungen": [{**e, "zeit": zeit(e["zeit"])} for e in erinn],
            "beziehungen": await self.graph.nachbarn(p["name"]),
        }

    async def persons_save(self, name: str, beziehung: str | None = None, notizen: str | None = None,
                           mail: str | None = None, telefon: str | None = None) -> str:
        p = await self._person(name)
        kontakt = dict(p["kontakt"] or {})
        kontakt.update({k: v for k, v in {"mail": mail, "telefon": telefon}.items() if v})
        await self.store.eine(
            """UPDATE personen SET beziehung = coalesce(%s, beziehung), notizen = coalesce(%s, notizen), kontakt = %s
               WHERE id = %s RETURNING id""", (beziehung, notizen, json.dumps(kontakt), p["id"]))
        return f"{p['name']} gespeichert."

    async def persons_rename(self, name: str, neuer_name: str) -> str:
        neu = " ".join(neuer_name.split())[:120]
        if not neu:
            return "Kein neuer Name angegeben."
        alt = await self.store.eine(
            "SELECT * FROM personen WHERE lower(name) = lower(%s) OR lower(%s) = ANY(SELECT lower(a) FROM unnest(aliase) a)",
            (name, name))
        if not alt:
            return f"Eine Person {name} gibt es nicht."
        andere = await self.store.eine("SELECT id FROM personen WHERE lower(name) = lower(%s) AND id <> %s", (neu, alt["id"]))
        if andere:
            return f"Es gibt schon eine Person {neu}. Zusammenführen geht nicht automatisch."
        async with await self.store.conn() as conn:
            await conn.execute(
                """UPDATE personen SET name = %s,
                          aliase = (SELECT array_agg(DISTINCT a) FROM unnest(aliase || %s::text) a WHERE lower(a) <> lower(%s))
                   WHERE id = %s""", (neu, alt["name"], neu, alt["id"]))
            for spalte, typ in (("von", "von_typ"), ("zu", "zu_typ")):
                await conn.execute(
                    f"""UPDATE beziehungen b SET {spalte} = %s WHERE {typ} = 'person' AND lower({spalte}) = lower(%s)
                        AND NOT EXISTS (SELECT 1 FROM beziehungen x WHERE x.von_typ = b.von_typ AND x.relation = b.relation
                            AND x.zu_typ = b.zu_typ AND x.{spalte} = %s
                            AND x.{"zu" if spalte == "von" else "von"} = b.{"zu" if spalte == "von" else "von"})""",
                    (neu, alt["name"], neu))
                await conn.execute(f"DELETE FROM beziehungen WHERE {typ} = 'person' AND lower({spalte}) = lower(%s)", (alt["name"],))
        return f"{alt['name']} heißt jetzt {neu}."

    async def projects_get(self, name: str) -> dict:
        p = await self.store.eine("SELECT * FROM projekte WHERE lower(name) = lower(%s)", (name,))
        if not p:
            return {"gefunden": False}
        unter = await self.store.alle("SELECT name, status FROM projekte WHERE oberprojekt = %s", (p["id"],))
        aufg = await self.store.alle("SELECT id, titel, status, faellig FROM aufgaben WHERE projekt_id = %s ORDER BY status, faellig", (p["id"],))
        erinn = await self.store.alle("SELECT text, zeit FROM erinnerungen WHERE projekt_id = %s ORDER BY zeit DESC LIMIT 10", (p["id"],))
        ober = await self.store.eine("SELECT name FROM projekte WHERE id = %s", (p["oberprojekt"],)) if p["oberprojekt"] else None
        return {
            "name": p["name"], "status": p["status"], "beschreibung": p["beschreibung"], "teil_von": ober and ober["name"],
            "unterprojekte": unter, "aufgaben": [{**a, "faellig": str(a["faellig"]) if a["faellig"] else None} for a in aufg],
            "erinnerungen": [{**e, "zeit": zeit(e["zeit"])} for e in erinn],
            "beziehungen": await self.graph.nachbarn(p["name"]),
        }

    async def projects_save(self, name: str, beschreibung: str | None = None, oberprojekt: str | None = None) -> str:
        p = await self._projekt(name)
        oid = (await self._projekt(oberprojekt))["id"] if oberprojekt else None
        await self.store.eine(
            "UPDATE projekte SET beschreibung = coalesce(%s, beschreibung), oberprojekt = coalesce(%s, oberprojekt) WHERE id = %s RETURNING id",
            (beschreibung, oid, p["id"]))
        if oberprojekt:
            await self.graph.verbinden("projekt", name, "teil_von", "projekt", oberprojekt)
        return f"Projekt {name} gespeichert."

    async def relations_add(self, von: str, relation: str, zu: str, von_typ: str = "ding", zu_typ: str = "ding") -> str:
        await self.graph.verbinden(von_typ, von, relation, zu_typ, zu)
        return f"Gespeichert: {von} {relation} {zu}."

    # ------------------------------------------------------------------ Aufgaben

    async def tasks_list(self, status: str = "offen", projekt: str | None = None) -> list[dict]:
        rows = await self.store.alle(
            """SELECT a.id, a.titel, a.faellig, a.status, p.name AS projekt FROM aufgaben a
               LEFT JOIN projekte p ON p.id = a.projekt_id
               WHERE a.status = %s AND (%s::text IS NULL OR lower(p.name) = lower(%s))
               ORDER BY a.faellig NULLS LAST, a.erstellt""", (status, projekt, projekt))
        return [{**r, "faellig": str(r["faellig"]) if r["faellig"] else None} for r in rows]

    async def tasks_create(self, titel: str, faellig: str | None = None, projekt: str | None = None) -> str:
        prid = (await self._projekt(projekt))["id"] if projekt else None
        row = await self.store.eine(
            "INSERT INTO aufgaben (titel, faellig, projekt_id) VALUES (%s, %s, %s) RETURNING id", (titel, faellig, prid))
        return f"Aufgabe {row['id']} angelegt: {titel}."

    async def tasks_done(self, id: int) -> str:
        row = await self.store.eine(
            "UPDATE aufgaben SET status = 'erledigt', erledigt = now() WHERE id = %s RETURNING titel", (id,))
        return f"Erledigt: {row['titel']}." if row else "Aufgabe nicht gefunden."

    # ------------------------------------------------------------------ Objekte (Vision-Gedächtnis)

    async def _sichtung(self, bedingung: str, param) -> dict | None:
        row = await self.store.eine(
            f"""SELECT s.*, z.name AS zone, coalesce(z.raum, '') AS raum,
                       array(SELECT neben FROM relations r WHERE r.sighting_id = s.id) AS neben
                FROM sightings s LEFT JOIN zones z ON z.id = s.zone_id WHERE {bedingung}
                ORDER BY s.zuletzt DESC LIMIT 1""", param)
        if not row:
            return None
        alter = int((datetime.now(timezone.utc) - row["zuletzt"]).total_seconds() // 60)
        return {
            "sighting_id": row["id"], "ort": row["zone"] or "unbekannte Stelle", "raum": row["raum"], "kamera": row["kamera"],
            "quelle": row["quelle"], "zuletzt_gesehen": zeit(row["zuletzt"]), "vor_minuten": alter,
            "liegt_dort_seit": zeit(row["erstmals"]), "sicherheit": round(row["sicherheit"], 2),
            "neben": row["neben"] or [], "veraltet": alter > STALE_MINUTEN,
        }

    async def objects_where(self, objekt: str) -> dict:
        obj = await self.store.eine(
            """SELECT o.id, o.name, greatest(
                 similarity(lower(o.name), lower(%(q)s)),
                 coalesce((SELECT max(similarity(lower(a), lower(%(q)s))) FROM unnest(o.aliases) a), 0),
                 CASE WHEN lower(o.typ) = lower(%(q)s) THEN 1 ELSE 0 END) AS score
               FROM objects o ORDER BY score DESC LIMIT 1""", {"q": objekt})
        if obj and obj["score"] >= 0.3:
            kamera = await self._sichtung("s.object_id = %s AND s.quelle <> 'ble'", (obj["id"],))
            ble = await self._sichtung("s.object_id = %s AND s.quelle = 'ble'", (obj["id"],))
            name = obj["name"]
        else:
            obj = None
            labels = labels_fuer(objekt, self.vokabular)
            kamera, ble, name = await self._sichtung("lower(s.label) = ANY(%s)", (labels,)), None, objekt
        if not kamera and not ble:
            return {"objekt": name, "gefunden": False, "hinweis": "Keine Sichtung gespeichert. Mit camera.capture nachsehen."}
        out: dict = {"objekt": name, "gefunden": True, "persoenlich_angelernt": bool(obj), "kamera": kamera, "bluetooth": ble}
        if kamera and kamera["veraltet"]:
            out["hinweis"] = f"Kamera-Sichtung ist {kamera['vor_minuten']} Minuten alt. camera.capture('{kamera['kamera']}') holt ein aktuelles Bild."
        if kamera and ble and ble["raum"] != kamera["raum"] and ble["vor_minuten"] < kamera["vor_minuten"]:
            out["hinweis"] = f"Der Bluetooth-Anhänger ist neuer und meldet {ble['raum']}. Der Gegenstand wurde vermutlich bewegt."
        return out

    async def objects_list(self) -> list[dict]:
        rows = await self.store.alle(
            """SELECT o.name, o.typ, o.ble_tag IS NOT NULL AS hat_anhaenger,
                      (SELECT max(zuletzt) FROM sightings s WHERE s.object_id = o.id) AS zuletzt
               FROM objects o ORDER BY o.name""")
        return [{**r, "zuletzt": zeit(r["zuletzt"])} for r in rows]

    async def system_privacy(self, an: bool) -> str:
        await self.store.eine(
            "UPDATE system_state SET value = %s::jsonb, zeit = now() WHERE key = 'privacy_mode' RETURNING key", (json.dumps(an),))
        await self.store.ereignis("privacy", "Privacy-Modus an" if an else "Privacy-Modus aus")
        return "Privacy-Modus ist an. Kameras werden nicht ausgewertet." if an else "Privacy-Modus ist aus."
