#!/usr/bin/env python3
"""Build dense embedding snapshot. Usage: build_dense.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rnd_factlink_mcp.embed_dense import build

if __name__ == "__main__":
    sys.exit(build())
