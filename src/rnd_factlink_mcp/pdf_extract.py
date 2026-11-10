"""PDF page extraction with quality classification (pdfplumber primary).

Signals and thresholds validated by SPIKE-001 (docs/SPIKE-001-pdf-extractor.md):
- (cid:NNNN) pollution = broken ToUnicode CMap (explicit failure)
- garbage_suspect = substantial non-ASCII text with almost no Hangul on a Korean doc
- image_only needs low_text + image area (slide decks keep tiny page-number text)

raw text: NFC only. normalized text: NFC + whitespace/zero-width/soft-hyphen cleanup.
Printed page numbers: only actually-extracted footer/header numbers that form a
+1 chain of length >= 3 are stored (no offset extrapolation — SCHEMA.md §3).
"""

import re
import unicodedata

import pdfplumber

ZERO_WIDTH_RE = re.compile("[​‌‍﻿­]")
MULTISPACE_RE = re.compile(r"[ \t]{2,}")
FOOTER_NUM_RE = re.compile(r"^[-‒–—]?\s*(\d{1,4})\s*[-‒–—]?$")

EDGE_BAND = 0.08  # top/bottom fraction of page height scanned for page numbers

# Single source of truth for the page-status taxonomy (SCHEMA.md §3).
# chunker indexes INDEXABLE pages; gap detection counts GAP pages.
# "empty" (no text, no images — 간지) is neither indexable nor a gap.
INDEXABLE_PAGE_STATUS = {"ok", "low_text"}
from .page_status import GAP_PAGE_STATUS  # noqa: F401 — canonical set lives in the light module


def signals(text: str) -> dict:
    if not text:
        return {"n_chars": 0}
    n = len(text)
    nonspace = [c for c in text if not c.isspace()]
    ns = len(nonspace) or 1
    hangul = sum(1 for c in nonspace if 0xAC00 <= ord(c) <= 0xD7A3)
    jamo = sum(1 for c in nonspace
               if 0x1100 <= ord(c) <= 0x11FF or 0x3130 <= ord(c) <= 0x318F)
    pua = sum(1 for c in nonspace if 0xE000 <= ord(c) <= 0xF8FF)
    ascii_ = sum(1 for c in nonspace if ord(c) < 128)
    return {
        "n_chars": n,
        "hangul_ratio": round(hangul / ns, 4),
        "jamo_ratio": round(jamo / ns, 4),
        "pua_ratio": round(pua / ns, 4),
        "ascii_ratio": round(ascii_ / ns, 4),
        "fffd_count": text.count("�"),
        "cid_count": text.count("(cid:"),
    }


def classify(sig: dict, image_ratio: float, korean_doc: bool) -> str:
    n = sig.get("n_chars", 0)
    if n == 0:
        return "image_only" if image_ratio > 0.1 else "empty"
    if n < 30:
        return "image_only" if image_ratio > 0.5 else "low_text"
    if sig["cid_count"] * 8 > n * 0.2:
        return "suspect_encoding"
    if sig["fffd_count"] > 0 or sig["jamo_ratio"] > 0.05 or sig["pua_ratio"] > 0.3:
        return "suspect_encoding"
    if korean_doc and n > 80 and sig["hangul_ratio"] < 0.05 and sig["ascii_ratio"] < 0.7:
        return "suspect_encoding"
    return "ok"


def normalize_text(raw_nfc: str) -> str:
    t = ZERO_WIDTH_RE.sub("", raw_nfc)
    t = MULTISPACE_RE.sub(" ", t)
    return "\n".join(line.rstrip() for line in t.splitlines())


def _edge_number_candidates(page) -> set[int]:
    """Standalone numbers in the top/bottom edge bands of the page."""
    cands = set()
    try:
        words = page.extract_words()
    except Exception:
        return cands
    h = float(page.height) or 1.0
    for w in words:
        if w["top"] > h * EDGE_BAND and w["bottom"] < h * (1 - EDGE_BAND):
            continue
        m = FOOTER_NUM_RE.match(w["text"].strip())
        if m:
            v = int(m.group(1))
            if 1 <= v <= 3000:
                cands.add(v)
    return cands


def _printed_chains(cands_per_page: list[set[int]]) -> list[tuple[int | None, float]]:
    """Assign printed_page_number per page from +1 chains of length >= 3."""
    n = len(cands_per_page)
    assigned: list[tuple[int | None, float]] = [(None, 0.0)] * n
    i = 0
    while i < n:
        best_chain: list[tuple[int, int]] = []
        for start in sorted(cands_per_page[i]):
            chain = [(i, start)]
            j, v = i + 1, start + 1
            while j < n and v in cands_per_page[j]:
                chain.append((j, v))
                j += 1
                v += 1
            if len(chain) > len(best_chain):
                best_chain = chain
        if len(best_chain) >= 3:
            conf = min(1.0, len(best_chain) / 10)
            for j, v in best_chain:
                if assigned[j][0] is None:
                    assigned[j] = (v, round(conf, 2))
            i = best_chain[-1][0] + 1
        else:
            i += 1
    return assigned


def extract_pdf(path, korean_doc: bool) -> tuple[list[dict], list[str]]:
    """Returns (page records, doc-level errors). Every page gets a status — no silent gaps."""
    pages: list[dict] = []
    errors: list[str] = []
    cands: list[set[int]] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            rec = {"pdf_page_index": i, "pdf_page_number": i + 1}
            try:
                raw = unicodedata.normalize("NFC", page.extract_text() or "")
                area = float(page.width) * float(page.height) or 1.0
                img = 0.0
                for im in page.images:
                    img += max(0.0, (im["x1"] - im["x0"]) * (im["bottom"] - im["top"]))
                image_ratio = min(1.0, img / area)
                sig = signals(raw)
                rec.update({
                    "page_status": classify(sig, image_ratio, korean_doc),
                    "image_ratio": round(image_ratio, 3),
                    "signals": sig,
                    "text_raw": raw,
                    "text_normalized": normalize_text(raw),
                })
                cands.append(_edge_number_candidates(page))
            except Exception as e:
                rec.update({"page_status": "extract_error", "signals": {"n_chars": 0},
                            "image_ratio": 0.0, "text_raw": "", "text_normalized": ""})
                cands.append(set())
                errors.append(f"page {i}: {type(e).__name__}: {e}")
            pages.append(rec)

    for rec, (printed, conf) in zip(pages, _printed_chains(cands)):
        rec["printed_page_number"] = printed
        rec["printed_page_confidence"] = conf if printed is not None else None
    return pages, errors
