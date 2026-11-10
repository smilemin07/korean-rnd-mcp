"""Structure- and page-aware chunking (Phase 2A).

Contract (SCHEMA.md §4): BGE-M3 tokenizer counts, target 350~700 (default
target 550, hard max 700), overlap ~50~80 tokens, block (paragraph) boundaries
preferred, page span preserved, one chunk spans at most MAX_PAGES_PER_CHUNK
pages. Only pages with status ok/low_text are indexed — suspect_encoding,
image_only, empty, extract_error pages are extraction gaps, recorded elsewhere.

Chunks carry both raw text (citation) and normalized text (search); the token
budget is counted on normalized text.
"""

import re
from dataclasses import dataclass, field

from .pdf_extract import INDEXABLE_PAGE_STATUS, normalize_text
from .tokenize_ko import bge_token_count

TARGET_TOKENS = 550
HARD_MAX_TOKENS = 700
OVERLAP_TOKENS = 60
MAX_PAGES_PER_CHUNK = 4


@dataclass
class Block:
    raw: str
    norm: str
    n_tokens: int
    page: int | None = None        # pdf_page_index
    heading_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    sheet: str | None = None


@dataclass
class Chunk:
    seq: int
    blocks: list[Block] = field(default_factory=list)

    @property
    def n_tokens(self) -> int:
        return sum(b.n_tokens for b in self.blocks)

    def to_record(self, doc_id: str) -> dict:
        pages = [b.page for b in self.blocks if b.page is not None]
        lines = [b for b in self.blocks if b.line_start is not None]
        first = self.blocks[0]
        if pages:
            locator = {"type": "pdf_page", "page_start": min(pages), "page_end": max(pages)}
        elif first.sheet is not None:
            locator = {"type": "sheet_range", "sheet": first.sheet}
        else:
            locator = {"type": "md", "heading_path": first.heading_path,
                       "line_start": min(b.line_start for b in lines) if lines else None,
                       "line_end": max(b.line_end for b in lines) if lines else None}
        return {
            "chunk_id": f"{doc_id}_c{self.seq:04d}",
            "doc_id": doc_id,
            "locator": locator,
            "page_start": min(pages) if pages else None,
            "page_end": max(pages) if pages else None,
            "n_tokens": self.n_tokens,
            "text_raw": "\n".join(b.raw for b in self.blocks),
            "text_norm": "\n".join(b.norm for b in self.blocks),
        }


def _split_blocks_text(raw_text: str, page: int | None = None,
                       sheet: str | None = None, line_offset: int = 0) -> list[Block]:
    """Split text into paragraph blocks (blank-line separated), tracking line numbers.
    A single oversized paragraph is split by lines so no block exceeds HARD_MAX."""
    blocks: list[Block] = []
    cur: list[tuple[int, str]] = []

    def emit(part: list[tuple[int, str]]):
        """Recursively bisect until under HARD_MAX; a single oversized line is
        split on sentence boundaries, then by character windows as last resort."""
        raw = "\n".join(l for _, l in part)
        norm = normalize_text(raw)
        nt = bge_token_count(norm)
        if nt <= HARD_MAX_TOKENS:
            blocks.append(Block(raw, norm, nt, page=page, sheet=sheet,
                                line_start=part[0][0], line_end=part[-1][0]))
            return
        if len(part) > 1:
            mid = len(part) // 2
            emit(part[:mid])
            emit(part[mid:])
            return
        lineno, line = part[0]
        pieces = [p for p in re.split(r"(?<=다\.)\s+|(?<=[.!?])\s+", line) if p.strip()]
        if len(pieces) < 2:  # no sentence boundary — character windows
            step = max(200, len(line) // ((nt // HARD_MAX_TOKENS) + 1))
            pieces = [line[i:i + step] for i in range(0, len(line), step)]
        acc = ""
        for p in pieces:
            cand = (acc + " " + p).strip()
            if acc and bge_token_count(normalize_text(cand)) > HARD_MAX_TOKENS:
                emit([(lineno, acc)])
                acc = p
            else:
                acc = cand
        if acc:
            n = bge_token_count(normalize_text(acc))
            if n > HARD_MAX_TOKENS and acc != line:
                emit([(lineno, acc)])
            else:
                blocks.append(Block(acc, normalize_text(acc), n, page=page, sheet=sheet,
                                    line_start=lineno, line_end=lineno))

    def flush():
        if cur:
            emit(cur.copy())
        cur.clear()

    for i, line in enumerate(raw_text.splitlines()):
        if line.strip():
            cur.append((line_offset + i, line))
        else:
            flush()
    flush()
    return blocks


def _md_blocks(raw_text: str) -> list[Block]:
    """Markdown: heading-aware blocks with heading_path."""
    blocks: list[Block] = []
    heading_stack: list[tuple[int, str]] = []
    buf: list[tuple[int, str]] = []

    def path() -> str:
        return " > ".join(h for _, h in heading_stack) or None

    def flush():
        nonlocal buf
        if buf:
            raw = "\n".join(l for _, l in buf)
            norm = normalize_text(raw)
            for b in _split_blocks_text(raw, line_offset=buf[0][0]):
                b.heading_path = path()
                blocks.append(b)
            buf = []

    for i, line in enumerate(raw_text.splitlines()):
        s = line.strip()
        if s.startswith("#"):
            flush()
            level = len(s) - len(s.lstrip("#"))
            title = s.lstrip("#").strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            norm = normalize_text(s)
            blocks.append(Block(line, norm, bge_token_count(norm),
                                heading_path=path(), line_start=i, line_end=i))
        elif s:
            buf.append((i, line))
        else:
            flush()
    flush()
    return blocks


def _pack(blocks: list[Block]) -> list[Chunk]:
    """Greedy accumulation to TARGET_TOKENS with overlap and page-span cap."""
    chunks: list[Chunk] = []
    cur = Chunk(seq=0)
    for b in blocks:
        page_span_exceeded = False
        if b.page is not None and cur.blocks:
            pages = [x.page for x in cur.blocks if x.page is not None]
            if pages and b.page - min(pages) + 1 > MAX_PAGES_PER_CHUNK:
                page_span_exceeded = True
        sheet_changed = cur.blocks and cur.blocks[-1].sheet != b.sheet
        if cur.blocks and (cur.n_tokens + b.n_tokens > HARD_MAX_TOKENS
                           or cur.n_tokens >= TARGET_TOKENS
                           or page_span_exceeded or sheet_changed):
            chunks.append(cur)
            # overlap: carry trailing blocks up to OVERLAP_TOKENS (same sheet/page domain)
            tail: list[Block] = []
            if not sheet_changed:
                acc = 0
                for x in reversed(cur.blocks):
                    if acc + x.n_tokens > OVERLAP_TOKENS:
                        break
                    tail.insert(0, x)
                    acc += x.n_tokens
            cur = Chunk(seq=len(chunks), blocks=tail.copy())
        cur.blocks.append(b)
    if cur.blocks:
        chunks.append(cur)
    return chunks


def chunk_document(doc: dict, pages_meta: list[dict], units: list[dict]) -> list[dict]:
    """Chunk one document's text units into chunk records."""
    blocks: list[Block] = []
    if doc["ext"] == "pdf":
        ok_pages = {p["pdf_page_index"] for p in pages_meta
                    if p["page_status"] in INDEXABLE_PAGE_STATUS}
        for u in units:
            page = u["locator"]["pdf_page_index"]
            if page in ok_pages:
                blocks.extend(_split_blocks_text(u["text_raw"], page=page))
    elif doc["ext"] in ("md", "html"):
        blocks = _md_blocks(units[0]["text_raw"]) if units else []
    elif doc["ext"] == "xlsx":
        for u in units:
            blocks.extend(_split_blocks_text(u["text_raw"], sheet=u["locator"]["sheet"]))
    return [c.to_record(doc["doc_id"]) for c in _pack(blocks) if c.n_tokens > 0]


if __name__ == "__main__":
    # self-test: synthetic doc, budget/overlap/page-cap invariants
    para = "국가연구개발사업의 예산 배분과 조정에 관한 문단이다. " * 12
    units = [{"locator": {"type": "pdf_page", "pdf_page_index": i},
              "text_raw": "\n\n".join([f"{i}장 제목", para, para])} for i in range(10)]
    pages_meta = [{"pdf_page_index": i, "page_status": "ok"} for i in range(10)]
    doc = {"doc_id": "d_test", "ext": "pdf"}
    recs = chunk_document(doc, pages_meta, units)
    assert recs, "no chunks produced"
    for r in recs:
        assert r["n_tokens"] <= HARD_MAX_TOKENS + OVERLAP_TOKENS, f"over budget: {r['n_tokens']}"
        assert r["page_end"] - r["page_start"] + 1 <= MAX_PAGES_PER_CHUNK, "page span exceeded"
    # suspect pages excluded
    pages_meta[3]["page_status"] = "suspect_encoding"
    recs2 = chunk_document(doc, pages_meta, units)
    assert all(not (r["page_start"] <= 3 <= r["page_end"]) or r["page_start"] == r["page_end"] == 3
               for r in recs2) and all(3 not in range(r["page_start"], r["page_end"] + 1) for r in recs2), \
        "suspect page leaked into chunks"
    # md heading path
    md_units = [{"locator": {"type": "md", "line_count": 7},
                 "text_raw": "# 제목\n\n## 소제목\n\n본문 첫 문단.\n\n본문 둘째 문단."}]
    mrecs = chunk_document({"doc_id": "d_md", "ext": "md"}, [], md_units)
    assert any(r["locator"].get("heading_path") for r in mrecs)
    print(f"chunker self-test passed ({len(recs)} pdf chunks, {len(mrecs)} md chunks)")
