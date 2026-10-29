"""Make the relocated application modules and tools available to the test suite."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "src", ROOT / "scripts"):
    sys.path.insert(0, str(directory))
