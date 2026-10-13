import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Code ohne Installation importierbar machen
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "vision"))
sys.path.insert(0, str(ROOT / "egress"))
sys.path.insert(0, str(ROOT / "connectors" / "_lib"))
sys.path.insert(0, str(ROOT / "tresor"))
sys.path.insert(0, str(ROOT / "tts"))
sys.path.insert(0, str(ROOT / "voice"))
