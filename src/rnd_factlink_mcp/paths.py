"""Repo-root and vault paths — the single lightweight declaration.

Runtime modules (server, search, embedding) import VAULT from here so the
serving container never pulls ingest-only dependencies (pdfplumber/pypdfium2)
through the import chain. ingest.py re-exports these for compatibility.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VAULT = REPO_ROOT / "vault"
