#!/usr/bin/env python3
"""Vault leak-prevention self-check (Phase 0, Gate 0).

Verifies that vault/ (dev corpus originals and derived indexes)
can never be committed to git.

Usage: python3 scripts/check_no_leak.py
Exit codes: 0 = all checks passed, 1 = a check failed.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" ({detail})" if detail else ""))
    return ok


def main() -> int:
    ok = True

    # 1. vault paths are ignored
    for probe in ["vault/raw/__probe__.pdf", "vault/meta/manifest.jsonl", "vault/index/__probe__.db"]:
        r = git("check-ignore", "-q", probe)
        ok &= check(f"gitignore covers {probe}", r.returncode == 0)

    # 2. no vault file is tracked
    r = git("ls-files", "vault")
    ok &= check("no vault/ file tracked by git", r.stdout.strip() == "", r.stdout.strip()[:200])

    # 3. a real file dropped into vault/ stays invisible to git status
    probe = REPO_ROOT / "vault" / "meta" / "__leak_probe__.txt"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("probe")
    try:
        r = git("status", "--porcelain")
        ok &= check("git status ignores files inside vault/", "vault/" not in r.stdout, r.stdout.strip()[:200])
    finally:
        probe.unlink()

    # 4. secret files are ignored
    r = git("check-ignore", "-q", ".env")
    ok &= check("gitignore covers .env", r.returncode == 0)

    if not ok:
        print("LEAK CHECK FAILED — do not commit until fixed.")
        return 1
    print("LEAK CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
