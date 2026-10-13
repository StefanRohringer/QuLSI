"""Text aus Dateien und Webseiten holen (für Browser, NAS, Drive, OneDrive, Mail).

Nur lesen, nichts ausführen: HTML wird geparst, aber kein JavaScript ausgeführt.
"""

from __future__ import annotations

import email
import email.policy
import io
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

TEXT_ENDUNGEN = {".txt", ".md", ".csv", ".json", ".log", ".xml", ".yaml", ".yml", ".ics", ".vcf"}
LESBAR = TEXT_ENDUNGEN | {".pdf", ".html", ".htm", ".eml"}


class _Seite(HTMLParser):
    UNSICHTBAR = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
    RAHMEN = {"nav", "footer", "aside", "form"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
             "header", "main", "table", "ul", "ol", "pre", "blockquote", "hr", "dd", "dt"}

    def __init__(self, basis: str):
        super().__init__(convert_charrefs=True)
        self.basis = basis
        self.titel = ""
        self.teile: list[str] = []
        self.links: list[dict] = []
        self._weg = 0          # Tiefe in unsichtbaren Elementen
        self._rahmen = 0       # Tiefe in Navigation/Fußzeile
        self._im_titel = False
        self._link: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._im_titel = True
        elif tag in self.UNSICHTBAR:
            self._weg += 1
        elif tag in self.RAHMEN:
            self._rahmen += 1
        if tag in self.BLOCK:
            self.teile.append("\n")
        if tag == "h1" or tag == "h2" or tag == "h3":
            self.teile.append("## " if tag != "h1" else "# ")
        if tag == "li":
            self.teile.append("- ")
        if tag == "a" and not self._weg:
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self._link = {"url": urljoin(self.basis, href), "text": ""}

    def handle_endtag(self, tag):
        if tag == "title":
            self._im_titel = False
        elif tag in self.UNSICHTBAR:
            self._weg = max(0, self._weg - 1)
        elif tag in self.RAHMEN:
            self._rahmen = max(0, self._rahmen - 1)
        if tag in self.BLOCK:
            self.teile.append("\n")
        if tag == "a" and self._link is not None:
            self._link["text"] = " ".join(self._link["text"].split())[:120]
            if self._link["text"] and all(l["url"] != self._link["url"] for l in self.links):
                self.links.append(self._link)
            self._link = None

    def handle_data(self, data):
        if self._im_titel:
            self.titel += data
            return
        if self._weg:
            return
        if self._link is not None:
            self._link["text"] += data
        if not self._rahmen:
            self.teile.append(data)


def html_text(html: str, basis: str = "") -> tuple[str, str, list[dict]]:
    """(Titel, lesbarer Text, Links) aus einer HTML-Seite."""
    p = _Seite(basis)
    p.feed(html)
    p.close()
    zeilen = [" ".join(z.split()) for z in "".join(p.teile).split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(zeilen)).strip()
    return " ".join(p.titel.split()), text, p.links


def pdf_text(daten: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join((s.extract_text() or "") for s in PdfReader(io.BytesIO(daten)).pages)


def mail_text(msg: email.message.Message, limit: int) -> str:
    teil = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    if not teil:
        return ""
    inhalt = teil.get_content()
    if teil.get_content_type() == "text/html":
        inhalt = html_text(inhalt)[1]
    return inhalt.strip()[:limit]


def text_aus(daten: bytes, endung: str = "", mime: str = "") -> str:
    """Text einer Datei. Unbekannte Formate ergeben ValueError."""
    endung, mime = endung.lower(), mime.split(";")[0].strip().lower()
    if endung == ".pdf" or mime == "application/pdf":
        return pdf_text(daten)
    if endung in (".html", ".htm") or mime in ("text/html", "application/xhtml+xml"):
        return html_text(daten.decode("utf-8", errors="replace"))[1]
    if endung == ".eml" or mime == "message/rfc822":
        msg = email.message_from_bytes(daten, policy=email.policy.default)
        return f"Von: {msg.get('From')}\nBetreff: {msg.get('Subject')}\n\n{mail_text(msg, 200_000)}"
    if endung in TEXT_ENDUNGEN or mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return daten.decode("utf-8", errors="replace")
    raise ValueError(f"Format {endung or mime or 'unbekannt'} kann QuLSI nicht als Text lesen.")


def seite(text: str, ab: int, laenge: int = 6000) -> dict:
    """Ausschnitt eines langen Texts, mit Hinweis wo es weitergeht."""
    ab = max(0, ab)
    return {"text": text[ab : ab + laenge], "laenge": len(text),
            "weiter_ab": ab + laenge if len(text) > ab + laenge else None}
