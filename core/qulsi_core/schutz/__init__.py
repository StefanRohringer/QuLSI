"""Schutz vor Angriffen über fremde Inhalte (Mail, Web, Dateien)."""

from .injektion import (Befund, Spur, analysieren, eigene_ziele_aufnehmen, pruefen_ergebnis, spur_von,
                        verpacken, verpacken_wenn_fremd, verschaerfen, warnung)

__all__ = ["Befund", "Spur", "analysieren", "eigene_ziele_aufnehmen", "pruefen_ergebnis", "spur_von",
           "verpacken", "verpacken_wenn_fremd", "verschaerfen", "warnung"]
