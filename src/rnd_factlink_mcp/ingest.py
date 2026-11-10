"""Phase 1 full-corpus ingest: originals copy, metadata, extraction, QA.

Contract (docs/SCHEMA.md §12, plan §Phase 1):
- originals are read at their declared root (corpus_roots.py) and never written
  to; verified copies go to vault/raw/<sha>.<ext>
- every file and every PDF page ends in an explicit status — zero silent gaps
- idempotent: a doc whose content_sha256 + PIPELINE_VERSION was already
  processed is loaded from its per-doc artifact, not re-extracted
- duplicate content (same sha, several paths) = one document + source aliases;
  the primary — whose filename supplies all parsed metadata — is chosen by root
  priority, not by path order

Outputs:
  vault/raw/<sha>.<ext>                originals (content-addressed)
  vault/derived/docmeta/<doc_id>.json  per-doc record + page records
  vault/derived/text/<doc_id>.jsonl    text units (raw + normalized)
  vault/derived/converted/<doc_id>.md  markdown from html/xlsx sources
  vault/meta/documents.jsonl           aggregate (rebuilt every run)
  vault/meta/pages.jsonl               aggregate (rebuilt every run)
  vault/meta/families.jsonl            aggregate (rebuilt every run)
  vault/meta/ingest_runs.jsonl         run log (appended)
  vault/derived/reports/ingest_qa.md   QA report
"""

import hashlib
import json
import shutil
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import convert, corpus_roots, filename_meta, pdf_extract
from .paths import REPO_ROOT, VAULT  # noqa: F401 — re-exported for existing importers

PIPELINE_VERSION = "p1.1"  # p1.1: curated plan metadata merge + sub-volume family split
CURATION_PATH = REPO_ROOT / "curation" / "plans.jsonl"

SUSPECT_DOC_THRESHOLD = 0.2  # fraction of suspect/image_only pages that flags a doc

# extraction results survive the cache; identity/parsed/curated metadata is
# recomputed every run so registry edits and family-rule changes always apply
EXTRACTION_FIELDS = ("extractor", "doc_errors", "n_units", "total_chars",
                     "n_pages", "page_status_counts")
PLAN_STATUS_VALUES = ("current", "superseded", "expired", "unknown")
STATUTORY_VALUES = ("statutory", "non_statutory", "unknown")


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _primary_of(rows: list[dict]) -> dict:
    """Pick the record whose FILENAME defines this document's metadata.

    Title, institution, variant, family and citation eligibility are all parsed
    from the primary's filename, so when the same bytes exist under several roots
    the choice must follow the declared root priority — never path-string order.
    """
    return min(rows, key=lambda r: (
        corpus_roots.priority_of(r.get("source_root_label", corpus_roots.LEGACY_LABEL)),
        r["source_path"]))


def _has_hangul(s: str) -> bool:
    return any(0xAC00 <= ord(c) <= 0xD7A3 for c in s)


def _doc_base(doc_id: str, sha: str, primary: dict, rows: list[dict]) -> dict:
    """Identity + filename-parsed metadata. Recomputed EVERY run, even for cached
    documents — otherwise family-rule or registry changes silently fail to apply
    to previously ingested files."""
    ext = primary["ext"]
    stem = Path(unicodedata.normalize("NFC", primary["source_path"])).stem
    meta = filename_meta.parse_filename(stem, ext)
    return {
        "doc_id": doc_id,
        "file_id": primary["file_id"],
        "content_sha256": sha,
        # source_refs keeps the root label, so two roots holding the same relative
        # path stay distinguishable; source_paths is the flat view used for stems.
        "source_refs": [{
            "root_label": r.get("source_root_label", corpus_roots.LEGACY_LABEL),
            "source_path": unicodedata.normalize("NFC", r["source_path"]),
            "source_folder": unicodedata.normalize("NFC", r["source_folder"]),
        } for r in rows],
        "source_paths": [unicodedata.normalize("NFC", r["source_path"]) for r in rows],
        "source_root_label": primary.get("source_root_label", corpus_roots.LEGACY_LABEL),
        "source_folder": unicodedata.normalize("NFC", primary["source_folder"]),
        "ext": ext,
        "size_bytes": primary["size_bytes"],
        "mtime": primary["mtime"],
        **meta,
        "servable_scope": "local_dev_only",
        "rights_review_status": "deferred",
        "pipeline_version": PIPELINE_VERSION,
        "extractor": None,
        "doc_errors": [],
    }


def _process_doc(doc_id: str, sha: str, primary: dict, rows: list[dict]) -> tuple[dict, list[dict], list[dict]]:
    """Extract one document. Returns (doc record, page records, text units)."""
    src = corpus_roots.resolve(primary)
    ext = primary["ext"]
    doc = _doc_base(doc_id, sha, primary, rows)

    pages: list[dict] = []
    units: list[dict] = []
    korean_doc = _has_hangul(doc["title"])

    if ext == "pdf":
        doc["extractor"] = "pdfplumber"
        page_recs, errors = pdf_extract.extract_pdf(src, korean_doc)
        doc["doc_errors"] = errors
        for rec in page_recs:
            pages.append({
                "doc_id": doc_id,
                "pdf_page_index": rec["pdf_page_index"],
                "pdf_page_number": rec["pdf_page_number"],
                "printed_page_number": rec["printed_page_number"],
                "printed_page_confidence": rec["printed_page_confidence"],
                "page_status": rec["page_status"],
                "image_ratio": rec["image_ratio"],
                "signals": rec["signals"],
            })
            if rec["text_raw"]:
                units.append({
                    "unit_index": len(units),
                    "locator": {"type": "pdf_page", "pdf_page_index": rec["pdf_page_index"]},
                    "text_raw": rec["text_raw"],
                    "text_normalized": rec["text_normalized"],
                })
    elif ext == "md":
        doc["extractor"] = "plaintext"
        raw = unicodedata.normalize("NFC", src.read_text(encoding="utf-8", errors="replace"))
        units.append({
            "unit_index": 0,
            "locator": {"type": "md", "line_count": raw.count("\n") + 1},
            "text_raw": raw,
            "text_normalized": pdf_extract.normalize_text(raw),
        })
    elif ext == "html":
        doc["extractor"] = "html_to_markdown"
        md = convert.html_to_markdown(src.read_text(encoding="utf-8", errors="replace"))
        conv = VAULT / "derived" / "converted" / f"{doc_id}.md"
        conv.parent.mkdir(parents=True, exist_ok=True)
        conv.write_text(md, encoding="utf-8")
        units.append({
            "unit_index": 0,
            "locator": {"type": "md", "line_count": md.count("\n") + 1, "converted_from": "html"},
            "text_raw": md,
            "text_normalized": pdf_extract.normalize_text(md),
        })
    elif ext == "xlsx":
        doc["extractor"] = "xlsx_to_markdown"
        sheets = convert.xlsx_to_markdown(src)
        conv_lines = []
        for name, text in sheets:
            conv_lines += [f"# sheet: {name}", "", text, ""]
            units.append({
                "unit_index": len(units),
                "locator": {"type": "sheet_range", "sheet": name, "converted_from": "xlsx"},
                "text_raw": text,
                "text_normalized": pdf_extract.normalize_text(text),
            })
            if not text.strip():
                doc["doc_errors"].append(f"sheet {name}: empty after conversion")
        conv = VAULT / "derived" / "converted" / f"{doc_id}.md"
        conv.parent.mkdir(parents=True, exist_ok=True)
        conv.write_text("\n".join(conv_lines), encoding="utf-8")
    else:
        doc["doc_errors"].append(f"unsupported ext: {ext}")

    doc["n_units"] = len(units)
    doc["total_chars"] = sum(len(u["text_raw"]) for u in units)
    if ext == "pdf":
        counts: dict[str, int] = {}
        for p in pages:
            counts[p["page_status"]] = counts.get(p["page_status"], 0) + 1
        doc["n_pages"] = len(pages)
        doc["page_status_counts"] = counts
    return doc, pages, units


def _load_plan_registry() -> tuple[list[dict], list[str]]:
    """Load and validate curation/plans.jsonl (git-tracked curated data)."""
    if not CURATION_PATH.is_file():
        return [], []
    rows = _load_jsonl(CURATION_PATH)
    errors, seen = [], set()
    for r in rows:
        pid = r.get("plan_id")
        if not pid or pid in seen:
            errors.append(f"plan_id 누락/중복: {pid!r}")
        seen.add(pid)
        if r.get("plan_status") not in PLAN_STATUS_VALUES:
            errors.append(f"{pid}: plan_status 값 오류: {r.get('plan_status')!r}")
        if r.get("plan_status") == "current" and not (
                r.get("plan_status_as_of") and r.get("current_basis")):
            errors.append(f"{pid}: current에는 plan_status_as_of·current_basis 필수")
        if r.get("statutory_status") not in STATUTORY_VALUES:
            errors.append(f"{pid}: statutory_status 값 오류: {r.get('statutory_status')!r}")
    return rows, errors


def _merge_curation(docs: list[dict], registry: list[dict]) -> dict:
    """Attach curated plan metadata to documents via bindings.

    Matching: content_sha256 first (an edited file must NOT silently inherit a
    verification done on the old content), filename stem as fallback for
    documents whose sha has not been recorded yet. A doc bound by several plans
    (e.g. a joint press release) keeps all plan_ids; its plan_status is
    "current" if ANY bound plan is current, else the first plan's status.
    """
    by_sha = {d["content_sha256"]: d for d in docs}
    # A stem shared by several documents cannot identify one of them. Binding the
    # last writer would attach a hand-verified plan record to an arbitrary file,
    # so ambiguous stems are refused outright and reported.
    stem_hits: dict[str, set[str]] = {}
    by_stem: dict[str, dict] = {}
    for d in docs:
        for p in d["source_paths"]:
            stem = unicodedata.normalize("NFC", Path(p).stem)
            stem_hits.setdefault(stem, set()).add(d["doc_id"])
            by_stem[stem] = d
    ambiguous_stems = {s for s, ids in stem_hits.items() if len(ids) > 1}

    plan_by_id = {r["plan_id"]: r for r in registry}
    bound: dict[str, list[str]] = {}
    report = {"matched": [], "stem_only": [], "unresolved": [], "conflicts": []}
    for r in registry:
        for b in r.get("bindings", []):
            sha = b.get("content_sha256")
            stem = unicodedata.normalize("NFC", b.get("filename_stem", ""))
            target = by_sha.get(sha) if sha else None
            if target is None and stem in ambiguous_stems:
                report["conflicts"].append(
                    f"{r['plan_id']}: stem 중복 — '{stem[:44]}'이 여러 문서에 해당. "
                    "bindings에 content_sha256을 기재해 결박할 것")
                continue
            if sha and target is None and stem in by_stem:
                report["conflicts"].append(
                    f"{r['plan_id']}: sha 불일치 — '{stem[:44]}' 문서가 교체된 듯. 재검증 필요")
                continue
            if target is None:
                target = by_stem.get(stem)
                if target is not None and not sha:
                    report["stem_only"].append(
                        f"{r['plan_id']} -> {target['doc_id']} (sha 미기록: {target['content_sha256'][:12]}…)")
            if target is None:
                report["unresolved"].append(f"{r['plan_id']}: {stem[:50]}")
                continue
            bound.setdefault(target["doc_id"], []).append(r["plan_id"])
            report["matched"].append(f"{r['plan_id']} -> {target['doc_id']}")

    for d in docs:
        pids = bound.get(d["doc_id"])
        if not pids:
            continue
        plans = [plan_by_id[p] for p in pids]
        rep = next((p for p in plans if p["plan_status"] == "current"), plans[0])
        d["plan_ids"] = pids
        d["plan_id"] = pids[0]
        d["plan_status"] = "current" if any(p["plan_status"] == "current" for p in plans) \
            else plans[0]["plan_status"]
        for f in ("plan_status_as_of", "current_basis", "statutory_status",
                  "statutory_basis", "plan_period", "announced_on", "plan_ministry",
                  "rnd_level"):
            d[f] = rep.get(f)
    return report


def _build_families(docs: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for d in docs:
        groups.setdefault(filename_meta.family_key(d), []).append(d)

    families = []
    for key, members in sorted(groups.items()):
        fam_id = "fam_" + hashlib.sha1(key.encode()).hexdigest()[:10]
        # anchor: best original-ish tier-1 member
        rank = {"original": 0, "full_text": 1, "notice": 2, "excerpt": 3, "press_release": 4, "summary": 5}
        # title-length tiebreak: among same-rank originals the main volume has the
        # shortest title (sub-volumes carry " - 세부 - NN ..." suffixes)
        anchor = min(members, key=lambda d: (d["authority_tier"], rank.get(d["variant_type"], 9), len(d["title"])))
        rel_by_variant = {"summary": "summarizes", "press_release": "accompanies",
                          "notice": "accompanies", "excerpt": "derived_from", "full_text": "accompanies"}
        for d in members:
            d["family_id"] = fam_id
            if d["doc_id"] != anchor["doc_id"]:
                d["relation_type"] = rel_by_variant.get(d["variant_type"], "accompanies")
                d["relation_target"] = anchor["doc_id"]
                d["relation_confidence"] = 0.7  # filename-based inference
            else:
                d["relation_type"] = None
                d["relation_target"] = None
                d["relation_confidence"] = None
        families.append({
            "family_id": fam_id,
            "family_key": key,
            "anchor_doc_id": anchor["doc_id"],
            "n_members": len(members),
            "member_doc_ids": [d["doc_id"] for d in members],
        })
    return families


def _qa_report(docs, all_pages, families, manifest, gate_ok, gate_msgs) -> str:
    lines = ["# Phase 1 ingest QA report", "",
             f"- run: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             f"- pipeline: {PIPELINE_VERSION}",
             f"- manifest files: {len(manifest)}, unique content: {len(docs)} docs, families: {len(families)}", ""]
    status_totals: dict[str, int] = {}
    for p in all_pages:
        status_totals[p["page_status"]] = status_totals.get(p["page_status"], 0) + 1
    lines += ["## PDF page status totals", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(status_totals.items())]
    lines += ["", "## Flagged documents (suspect/image_only/error over threshold or doc errors)", ""]
    for d in docs:
        if d["ext"] != "pdf":
            if d["doc_errors"]:
                lines.append(f"- {d['doc_id']} [{d['ext']}] {d['title']}: {d['doc_errors']}")
            continue
        n = d["n_pages"] or 1
        bad = sum(d["page_status_counts"].get(k, 0) for k in ("suspect_encoding", "image_only", "extract_error"))
        if bad / n > SUSPECT_DOC_THRESHOLD or d["doc_errors"]:
            lines.append(f"- {d['doc_id']} {d['title']}: pages={n}, {d['page_status_counts']}, errors={len(d['doc_errors'])}")
    lines += ["", "## Families (n>=2)", ""]
    by_id = {d["doc_id"]: d for d in docs}
    for f in families:
        if f["n_members"] < 2:
            continue
        mem = ", ".join(f"{by_id[m]['variant_type']}[{by_id[m]['ext']}]" for m in f["member_doc_ids"])
        lines.append(f"- {f['family_key']}: {mem}")
    lines += ["", "## Gate 1", ""]
    lines += [f"- {'PASS' if gate_ok else 'FAIL'}"] + [f"- {m}" for m in gate_msgs]
    lines += ["- golden 정답 페이지 추출 성공률: 보류 (golden set 질문 미수집)", ""]
    return "\n".join(lines)


def run(force: bool = False) -> int:
    t0 = time.perf_counter()
    manifest = _load_jsonl(VAULT / "meta" / "manifest.jsonl")
    if not manifest:
        print("FATAL: manifest missing — run scripts/build_manifest.py first")
        return 1

    registry, registry_errors = _load_plan_registry()
    # previous family assignment, for the migration report
    old_families = {d["doc_id"]: d.get("family_id")
                    for d in _load_jsonl(VAULT / "meta" / "documents.jsonl")}

    by_sha: dict[str, list[dict]] = {}
    for row in manifest:
        by_sha.setdefault(row["content_sha256"], []).append(row)

    docs, all_pages = [], []
    n_cached = 0
    copy_errors: list[str] = []
    for sha, rows in sorted(by_sha.items(), key=lambda kv: kv[1][0]["source_path"]):
        primary = _primary_of(rows)
        rows = [primary] + [r for r in rows if r is not primary]
        doc_id = "d_" + sha[:12]

        # originals copy (content-addressed, copy once). The copy is verified by
        # digest, not size: a same-size corrupted or swapped source would
        # otherwise be preserved forever under the name of the sha it is not.
        raw_dst = VAULT / "raw" / f"{sha}.{primary['ext']}"
        if not raw_dst.is_file() or raw_dst.stat().st_size != primary["size_bytes"]:
            raw_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(corpus_roots.resolve(primary), raw_dst)
            copied_sha = _sha256_of(raw_dst)
            if copied_sha != sha:
                raw_dst.unlink(missing_ok=True)
                copy_errors.append(
                    f"{primary['source_path']}: 원본 해시 불일치 "
                    f"(manifest {sha[:12]}… vs 실제 {copied_sha[:12]}…) — manifest 재생성 필요")
                continue

        docmeta_path = VAULT / "derived" / "docmeta" / f"{doc_id}.json"
        text_path = VAULT / "derived" / "text" / f"{doc_id}.jsonl"
        cached = None
        if not force and docmeta_path.is_file() and text_path.is_file():
            loaded = json.loads(docmeta_path.read_text(encoding="utf-8"))
            if loaded["doc"].get("pipeline_version") == PIPELINE_VERSION and \
               loaded["doc"].get("content_sha256") == sha:
                cached = loaded
        if cached:
            # extraction results come from the cache; identity/parsed metadata is
            # rebuilt so family-rule and registry changes reach cached documents
            doc = _doc_base(doc_id, sha, primary, rows)
            for f in EXTRACTION_FIELDS:
                if f in cached["doc"]:
                    doc[f] = cached["doc"][f]
            pages = cached["pages"]
            n_cached += 1
        else:
            doc, pages, units = _process_doc(doc_id, sha, primary, rows)
            docmeta_path.parent.mkdir(parents=True, exist_ok=True)
            _write_jsonl(text_path, units)
            docmeta_path.write_text(
                json.dumps({"doc": doc, "pages": pages}, ensure_ascii=False), encoding="utf-8")
        docs.append(doc)
        all_pages.extend(pages)

    curation_report = _merge_curation(docs, registry)
    families = _build_families(docs)
    # re-persist docmeta with family/relation fields filled
    for doc in docs:
        docmeta_path = VAULT / "derived" / "docmeta" / f"{doc['doc_id']}.json"
        stored = json.loads(docmeta_path.read_text(encoding="utf-8"))
        stored["doc"] = doc
        docmeta_path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")

    _write_jsonl(VAULT / "meta" / "documents.jsonl", docs)
    _write_jsonl(VAULT / "meta" / "pages.jsonl", all_pages)
    _write_jsonl(VAULT / "meta" / "families.jsonl", families)

    # Gate 1: coverage and page-status completeness
    gate_msgs = []
    covered = {d["content_sha256"] for d in docs}
    missing = [r["source_path"] for r in manifest if r["content_sha256"] not in covered]
    gate_msgs.append(f"manifest coverage: {len(manifest) - len(missing)}/{len(manifest)} files, missing={len(missing)}")
    pdf_docs = [d for d in docs if d["ext"] == "pdf"]
    unclassified = [p for p in all_pages if not p.get("page_status")]
    gate_msgs.append(f"pdf docs: {len(pdf_docs)}, pages: {len(all_pages)}, unclassified pages: {len(unclassified)}")
    empty_docs = [d["doc_id"] for d in docs if d["n_units"] == 0 and d["ext"] != "pdf"]
    gate_msgs.append(f"non-pdf docs with zero text units: {empty_docs or 'none'}")
    for e in copy_errors:
        gate_msgs.append(f"originals copy ERROR: {e}")
    for e in registry_errors:
        gate_msgs.append(f"plan registry ERROR: {e}")
    for c in curation_report["conflicts"]:
        gate_msgs.append(f"plan binding CONFLICT: {c}")
    gate_msgs.append(
        f"plan bindings: matched={len(curation_report['matched'])}, "
        f"stem_only={len(curation_report['stem_only'])}, "
        f"unresolved(미편입 문서)={len(curation_report['unresolved'])}")
    # sub-volume pattern hits — reviewed by a human whenever new corpus arrives
    sub_hits = [d["title"] for d in docs if d.get("is_sub_volume")]
    gate_msgs.append(f"sub-volume family split hits: {len(sub_hits)}")
    fam_moves = [(d["doc_id"], old_families.get(d["doc_id"]), d["family_id"])
                 for d in docs
                 if d["doc_id"] in old_families and old_families[d["doc_id"]] != d["family_id"]]
    gate_msgs.append(f"family migration: {len(fam_moves)} docs moved")
    gate_ok = (not missing and not unclassified and not empty_docs and not copy_errors
               and not registry_errors and not curation_report["conflicts"])
    # detail lines for the QA report (review targets, not gate conditions)
    for t in sub_hits:
        gate_msgs.append(f"  sub-volume: {t[:70]}")
    for m in curation_report["stem_only"]:
        gate_msgs.append(f"  stem-only binding (레지스트리에 sha 기입 권장): {m}")
    for u in curation_report["unresolved"]:
        gate_msgs.append(f"  unresolved binding: {u}")
    for did, old, new in fam_moves:
        gate_msgs.append(f"  family move: {did} {old} -> {new}")

    report = _qa_report(docs, all_pages, families, manifest, gate_ok, gate_msgs)
    report_path = VAULT / "derived" / "reports" / "ingest_qa.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    run_rec = {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "pipeline_version": PIPELINE_VERSION,
        "n_files": len(manifest), "n_docs": len(docs), "n_cached": n_cached,
        "n_pages": len(all_pages), "gate1": "PASS" if gate_ok else "FAIL",
        "seconds": round(time.perf_counter() - t0, 1),
    }
    with open(VAULT / "meta" / "ingest_runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(run_rec, ensure_ascii=False) + "\n")

    print(json.dumps(run_rec, ensure_ascii=False))
    for m in gate_msgs:
        print(m)
    print(f"report: {report_path}")
    print(f"GATE 1 {'PASS' if gate_ok else 'FAIL'}")
    return 0 if gate_ok else 1


if __name__ == "__main__":
    # --- primary selection follows root priority, never path-string order ---
    mhk_row = {"source_root_label": "mhk", "source_path": "folder/A.pdf"}
    corpus_row = {"source_root_label": "corpus", "source_path": "policies/zzz.pdf"}
    # "policies/zzz" sorts after "folder/A"; priority must still pick the corpus row
    assert _primary_of([mhk_row, corpus_row]) is corpus_row
    assert _primary_of([corpus_row, mhk_row]) is corpus_row
    # legacy rows without a label are mhk rows
    legacy = {"source_path": "folder/B.pdf"}
    assert _primary_of([legacy, corpus_row]) is corpus_row
    # within one root, path order still decides (deterministic)
    a = {"source_root_label": "mhk", "source_path": "a.pdf"}
    b = {"source_root_label": "mhk", "source_path": "b.pdf"}
    assert _primary_of([b, a]) is a

    # --- a stem shared by several documents must NOT bind, and must be reported ---
    def _doc(doc_id, sha, path):
        return {"doc_id": doc_id, "content_sha256": sha, "source_paths": [path]}

    docs = [_doc("d_1", "sha1", "x/제1차 계획.pdf"), _doc("d_2", "sha2", "y/제1차 계획.pdf")]
    reg = [{"plan_id": "P900", "plan_status": "current",
            "bindings": [{"filename_stem": "제1차 계획", "content_sha256": None}]}]
    rep = _merge_curation(docs, reg)
    assert not rep["matched"], rep["matched"]
    assert any("stem 중복" in c for c in rep["conflicts"]), rep["conflicts"]
    assert "plan_id" not in docs[0] and "plan_id" not in docs[1]

    # an unambiguous stem still binds
    docs = [_doc("d_1", "sha1", "x/제1차 계획.pdf"), _doc("d_2", "sha2", "y/제2차 계획.pdf")]
    rep = _merge_curation(docs, reg)
    assert rep["matched"] == ["P900 -> d_1"], rep["matched"]
    assert docs[0]["plan_id"] == "P900" and "plan_id" not in docs[1]

    # sha binding wins over stem, and NFD input matches an NFC registry stem
    docs = [_doc("d_1", "sha1", unicodedata.normalize("NFD", "x/제1차 계획.pdf"))]
    reg2 = [{"plan_id": "P901", "plan_status": "current",
             "bindings": [{"filename_stem": "제1차 계획", "content_sha256": None}]}]
    rep = _merge_curation(docs, reg2)
    assert rep["matched"] == ["P901 -> d_1"], rep["matched"]

    print("ingest self-test passed")
