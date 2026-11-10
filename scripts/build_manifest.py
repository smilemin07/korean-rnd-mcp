#!/usr/bin/env python3
"""Build the corpus manifest (Phase 0, Gate 0).

Scans every declared corpus root (src/rnd_factlink_mcp/corpus_roots.py), records
path/size/ext/sha256/mtime per file, and compares the scan against the approved
baseline in curation/corpus_baseline.json.

Gate 0 compares per-file sha256, not just totals: a deletion masked by an
addition, or a same-size content swap, both fail. The manifest is written only
after the gate passes, so a failed run can never leave a manifest that a later
ingest would silently consume.

Growing the corpus is normal work, not a code change: stage the new files under
a declared root, run with --approve-baseline to review and record them.

Usage:
  python3 scripts/build_manifest.py                    # verify against baseline
  python3 scripts/build_manifest.py --approve-baseline # adopt the scan as the new baseline
Exit codes: 0 = gate passed, 1 = gate failed.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rnd_factlink_mcp import corpus_roots  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "vault" / "meta" / "manifest.jsonl"
BASELINE_PATH = corpus_roots.BASELINE_PATH
# Documents the operator has ruled out of the corpus. Matched by content digest, so a
# rename cannot smuggle one back in. Hitting one is a hard stop, not a warning:
# --approve-baseline must not be able to override an exclusion.
EXCLUSIONS_PATH = REPO_ROOT / "curation" / "corpus_exclusions.jsonl"
# Exclusion entries that are not published live beside the Gate 0 ledger,
# outside the repo. Both lists are required.
PRIVATE_EXCLUSIONS_PATH = corpus_roots.PRIVATE_EXCLUSIONS_PATH


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def scan() -> list[dict]:
    records = []
    for spec in corpus_roots.ROOTS:
        root = spec["root"]
        for folder in spec["folders"]:
            base = root / folder
            if not base.is_dir():
                sys.exit(f"FATAL: source folder missing: {base}")
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                if path.name.startswith(".") or path.name.startswith("._"):
                    continue  # .DS_Store etc.
                sha = sha256_of(path)
                stat = path.stat()
                # macOS stores Hangul filenames decomposed (NFD); every consumer
                # compares composed (NFC), so normalize at the boundary.
                rel = unicodedata.normalize("NFC", str(path.relative_to(root)))
                records.append({
                    "file_id": "f_" + sha[:12],
                    "source_root_label": spec["label"],
                    "source_path": rel,
                    "source_folder": unicodedata.normalize("NFC", folder),
                    "ext": path.suffix.lower().lstrip("."),
                    "size_bytes": stat.st_size,
                    "content_sha256": sha,
                    "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
                })
    records.sort(key=lambda r: (corpus_roots.priority_of(r["source_root_label"]),
                                r["source_root_label"], r["source_path"]))
    return records


def enforce_exclusions(records: list[dict]) -> None:
    """Stop the build if an excluded document is sitting in a corpus root."""
    excluded = {}
    for path in (EXCLUSIONS_PATH, PRIVATE_EXCLUSIONS_PATH):
        # fail closed: a missing or empty list would let excluded documents back in
        if not path.is_file():
            sys.exit(f"GATE 0 FATAL: exclusion list missing: {path} "
                     "(building the corpus needs the operator's private curation files)")
        n = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                sha = row["content_sha256"]
                # lowercase hex only: sha256_of() emits lowercase, so any other form never matches
                if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
                    sys.exit(f"GATE 0 FATAL: malformed sha in {path.name}")
                excluded[sha] = row
                n += 1
        if n == 0:
            sys.exit(f"GATE 0 FATAL: exclusion list empty: {path}")
    hits = [(r, excluded[r["content_sha256"]]) for r in records
            if r["content_sha256"] in excluded]
    if not hits:
        return
    print(f"GATE 0 FATAL: {len(hits)} excluded document(s) found in a corpus root")
    for rec, row in hits:
        print(f"  [{rec['source_root_label']}] {rec['source_path']}")
        print(f"      사유: {row['reason']}")
    sys.exit("Remove them from the corpus root, or delete the entry in the exclusion "
             "list if the exclusion no longer applies.")


def as_baseline(records: list[dict]) -> dict:
    """Scan reduced to the fields the gate approves: per-root path -> sha/ext/size.
    mtime is deliberately excluded — it changes on copy and restore."""
    roots: dict[str, dict] = {}
    for r in records:
        files = roots.setdefault(r["source_root_label"], {})
        files[r["source_path"]] = {
            "sha256": r["content_sha256"],
            "ext": r["ext"],
            "size_bytes": r["size_bytes"],
        }
    return {"roots": roots}


def diff_roots(approved: dict, actual: dict) -> list[str]:
    """Human-readable differences, per root, between approved and scanned files."""
    msgs = []
    for label in sorted(set(approved) | set(actual)):
        want = approved.get(label, {})
        have = actual.get(label, {})
        for path in sorted(set(want) - set(have)):
            msgs.append(f"[{label}] REMOVED  {path}")
        for path in sorted(set(have) - set(want)):
            msgs.append(f"[{label}] ADDED    {path}  sha={have[path]['sha256'][:12]}")
        for path in sorted(set(want) & set(have)):
            if want[path]["sha256"] != have[path]["sha256"]:
                msgs.append(f"[{label}] CHANGED  {path}  "
                            f"{want[path]['sha256'][:12]} -> {have[path]['sha256'][:12]}")
    return msgs


def summarize(records: list[dict]) -> None:
    per_root: dict[str, dict[str, int]] = {}
    by_sha: dict[str, list[str]] = {}
    for r in records:
        counts = per_root.setdefault(r["source_root_label"], {})
        counts[r["ext"]] = counts.get(r["ext"], 0) + 1
        by_sha.setdefault(r["content_sha256"], []).append(
            f"{r['source_root_label']}:{r['source_path']}")

    print(f"total files: {len(records)}")
    for spec in corpus_roots.ROOTS:
        counts = per_root.get(spec["label"], {})
        print(f"  [{spec['label']}] {sum(counts.values())} files: "
              + (", ".join(f"{e}={n}" for e, n in sorted(counts.items())) or "none"))

    dupes = {sha: paths for sha, paths in by_sha.items() if len(paths) > 1}
    if dupes:
        print(f"duplicate content_sha256: {len(dupes)} group(s) (merged into one document each)")
        for sha, paths in dupes.items():
            print(f"  {sha[:12]}: " + " | ".join(paths))
    else:
        print("duplicate content_sha256: none")

    # NFC normalization can map two distinct on-disk names onto one key
    seen: dict[tuple[str, str], str] = {}
    for r in records:
        key = (r["source_root_label"], r["source_path"])
        if key in seen and seen[key] != r["content_sha256"]:
            sys.exit(f"FATAL: NFC path collision under {key[0]}: {key[1]}")
        seen[key] = r["content_sha256"]


def write_manifest(records: list[dict]) -> None:
    """Atomic replace — a partial manifest must never be visible to ingest."""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, MANIFEST_PATH)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve-baseline", action="store_true",
                    help="adopt the current scan as the approved corpus baseline")
    args = ap.parse_args()

    records = scan()
    enforce_exclusions(records)
    summarize(records)
    actual = as_baseline(records)

    if not BASELINE_PATH.is_file():
        if not args.approve_baseline:
            print(f"GATE 0 FAIL: no baseline at {BASELINE_PATH}")
            print("Run with --approve-baseline to record the current corpus as approved.")
            return 1
        approved_roots = {}
    else:
        approved_roots = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["roots"]

    changes = diff_roots(approved_roots, actual["roots"])

    if changes and not args.approve_baseline:
        print(f"GATE 0 FAIL: {len(changes)} unapproved change(s) vs {BASELINE_PATH.name}")
        for m in changes:
            print(f"  {m}")
        print("Review the list. If every change is intended, re-run with --approve-baseline.")
        print("Manifest NOT written — the previous manifest is untouched.")
        return 1

    if args.approve_baseline:
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(
            {"approved_on": datetime.now(timezone.utc).date().isoformat(),
             "note": "Gate 0 approved corpus. Regenerate with scripts/build_manifest.py --approve-baseline.",
             "roots": actual["roots"]},
            ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline approved: {BASELINE_PATH} ({len(changes)} change(s) recorded)")
        for m in changes:
            print(f"  {m}")

    write_manifest(records)
    print(f"manifest: {MANIFEST_PATH}")
    print(f"GATE 0 PASS: {len(records)} files across {len(corpus_roots.ROOTS)} roots "
          "match the approved baseline (per-file sha256)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
