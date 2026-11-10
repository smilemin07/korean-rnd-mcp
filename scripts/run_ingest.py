#!/usr/bin/env python3
"""CLI wrapper for the Phase 1 ingest. Usage: run_ingest.py [--force]"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rnd_factlink_mcp.ingest import run

if __name__ == "__main__":
    sys.exit(run(force="--force" in sys.argv[1:]))
