"""Corpus source roots — the single declaration shared by manifest build and ingest.

Two roots, deliberately kept apart (decision 2026-08-14, cross-validated):

  corpus — the project's canonical originals. Documents downloaded for the
           product corpus live here. Outside the repo (so `vault/` can be wiped
           and rebuilt without losing originals) and outside the read-only mhk root
           (so the project never writes into it). This is what must be backed up.

  mhk    — a second, read-only document root. This project never writes, moves,
           renames or deletes anything under it. It is the legacy source of the
           original development corpus. Its location
           and folder list are private: they are read from a local file beside
           the Gate 0 ledger (LOCAL_ROOTS_PATH), never kept in this repo.

`priority` decides which record becomes the primary when the same bytes (same
sha256) appear under several roots. It matters because title, institution,
variant, family and citation eligibility are all parsed from the primary's
FILENAME — path-string sort order must never be what picks them.
`corpus` wins because curation/plans.jsonl bindings are authored against the
filenames used there.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

CORPUS_ROOT = Path.home() / "rnd-factlink-corpus"

# Private curation directory: Gate 0 approval ledger, private exclusions and the
# local root declarations. Lives beside the originals (backed up with them) and
# outside the public repo: it lists the operator's non-served holdings.
PRIVATE_CURATION = CORPUS_ROOT / "curation"
BASELINE_PATH = PRIVATE_CURATION / "corpus_baseline.json"
PRIVATE_EXCLUSIONS_PATH = PRIVATE_CURATION / "corpus_exclusions.private.jsonl"
LOCAL_ROOTS_PATH = PRIVATE_CURATION / "corpus_roots.local.json"

# Manifest rows written before multi-root support carry no label; every one of
# them came from mhk. New rows always carry it, so a missing label on a row that
# also fails to resolve is a bug, not a legacy row.
LEGACY_LABEL = "mhk"


def _load_local_roots(path: Path) -> list[dict]:
    """Extra roots declared in the private local file.

    On the operator's machine (the private curation directory exists) the file
    is required: a missing or malformed file must stop the build, never shrink
    the corpus silently. Elsewhere (a public clone) there are no extra roots.
    """
    if not path.parent.is_dir():
        return []
    if not path.is_file():
        raise RuntimeError(f"local corpus roots file missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    roots = []
    for spec in data.get("roots", []):
        label, prio, folders = spec.get("label"), spec.get("priority"), spec.get("folders")
        if not (isinstance(label, str) and label and isinstance(prio, int)
                and isinstance(folders, list) and folders
                and all(isinstance(f, str) and f for f in folders)
                and isinstance(spec.get("root"), str) and spec["root"]):
            raise RuntimeError(f"malformed root entry in {path}")
        roots.append({
            "label": label,
            "priority": prio,
            "root": Path(spec["root"]).expanduser(),
            "read_only": bool(spec.get("read_only", False)),
            "folders": folders,
        })
    if not roots:
        raise RuntimeError(f"no roots declared in {path}")
    return roots


ROOTS = [
    {
        "label": "corpus",
        "priority": 0,
        "root": CORPUS_ROOT,
        "folders": ["policies"],
    },
    *_load_local_roots(LOCAL_ROOTS_PATH),
]

BY_LABEL = {spec["label"]: spec for spec in ROOTS}
# a local root reusing a label or priority would silently re-point or reorder roots
if len(BY_LABEL) != len(ROOTS) or len({s["priority"] for s in ROOTS}) != len(ROOTS):
    raise RuntimeError(f"duplicate root label or priority in {LOCAL_ROOTS_PATH}")


def resolve(row: dict) -> Path:
    """Absolute path of a manifest row, resolved against its own root."""
    label = row.get("source_root_label", LEGACY_LABEL)
    spec = BY_LABEL.get(label)
    if spec is None:
        raise KeyError(f"unknown source_root_label {label!r} for {row['source_path']!r}")
    return spec["root"] / row["source_path"]


def priority_of(label: str) -> int:
    """Sort rank of a root label; unknown labels sort last."""
    spec = BY_LABEL.get(label)
    return spec["priority"] if spec else len(ROOTS)


if __name__ == "__main__":
    # roots are distinct, and their priorities are a strict order (no ties —
    # a tie would put the primary choice back at the mercy of sort order)
    labels = [s["label"] for s in ROOTS]
    assert len(set(labels)) == len(labels), labels
    prios = [s["priority"] for s in ROOTS]
    assert len(set(prios)) == len(prios), prios
    assert priority_of("__unknown__") == len(ROOTS)
    assert all(priority_of("corpus") < s["priority"] for s in ROOTS if s["label"] != "corpus"), \
        "corpus filenames must win"

    assert resolve({"source_root_label": "corpus", "source_path": "policies/a.pdf"}) == \
        BY_LABEL["corpus"]["root"] / "policies/a.pdf"
    if LEGACY_LABEL in BY_LABEL:
        # a pre-multi-root manifest row carries no label and is a legacy-root row
        assert resolve({"source_path": "folder/b.pdf"}) == BY_LABEL[LEGACY_LABEL]["root"] / "folder/b.pdf"
        assert BY_LABEL[LEGACY_LABEL].get("read_only") is True
    try:
        resolve({"source_root_label": "nope", "source_path": "x"})
        raise AssertionError("unknown label must raise, not silently fall back")
    except KeyError:
        pass

    for spec in ROOTS:
        assert not str(spec["root"]).startswith(str(REPO_ROOT / "vault")), \
            f"{spec['label']}: originals must not live under vault/ (wiped on rebuild)"
    print("corpus_roots self-test passed")
