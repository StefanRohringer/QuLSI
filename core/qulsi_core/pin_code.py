"""Einrichtungscode für die Freigabe-PIN: nur auf der Box selbst abrufbar, nie über die App.

    python -m qulsi_core.pin_code            neuen Code zeigen (15 Minuten gültig, einmal verwendbar)
    python -m qulsi_core.pin_code --wenn-leer nur, wenn noch keine PIN eingerichtet ist (start.sh, status)
    python -m qulsi_core.pin_code --aus      PIN entfernen (Erlauben in der App geht dann erst nach neuer PIN)

RunPod: bash runpod/qulsi.sh pin    Box: docker compose exec core python -m qulsi_core.pin_code
Mit dem Code legt man in der App unter Einstellungen die erste PIN fest oder setzt eine vergessene neu.
"""

from __future__ import annotations

import asyncio
import sys

from .config import Settings
from .confirm import AppPin
from .memory import Store


async def _main(argv: list[str]) -> int:
    s = Settings()
    pin = AppPin(Store(s.database_url), s.freigabe_pin)
    await pin.laden()
    if "--aus" in argv:
        await pin.setzen(None, "box")
        print("Freigabe-PIN entfernt. Neue PIN in der App mit einem Einrichtungscode: qulsi.sh pin")
        return 0
    if "--wenn-leer" in argv and pin.eingerichtet:
        return 0
    code = await pin.code_neu()
    was = "eine neue Freigabe-PIN" if pin.eingerichtet else "die Freigabe-PIN"
    print(f"Einrichtungscode: {code}   (15 Minuten gültig, nur einmal)")
    print(f"In der QuLSI-App unter Einstellungen > Sicherheit {was} mit diesem Code festlegen.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
