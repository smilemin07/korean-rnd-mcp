#!/usr/bin/env python3
"""Build the lexical index. Usage: build_index.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rnd_factlink_mcp.index_lexical import build

if __name__ == "__main__":
    sys.exit(build())
