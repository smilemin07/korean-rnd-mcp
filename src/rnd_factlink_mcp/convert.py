"""One-off converters: HTML and XLSX sources to markdown text (stdlib only).

The corpus has 2 HTML files (generated summaries) and 1 XLSX (official annex,
39 per-ministry sheets, sharedStrings, few merged cells). Converted markdown is
stored under vault/derived/converted/ and then ingested as md-type text.
"""

import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class _HtmlToText(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}
    BLOCK = {"p", "div", "li", "tr", "br", "ul", "ol", "table", "section", "article"}

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self._skip_depth = 0
        self._pending_prefix = ""

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.HEADINGS:
            self.out.append("\n\n" + self.HEADINGS[tag] + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "td" or tag == "th":
            self.out.append(" | ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.HEADINGS or tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.out.append(re.sub(r"\s+", " ", data))


def html_to_markdown(html: str) -> str:
    p = _HtmlToText()
    p.feed(html)
    text = "".join(p.out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return unicodedata.normalize("NFC", text).strip() + "\n"


def _cell_text(cell, shared: list[str]) -> str:
    t = cell.get("t", "n")
    if t == "s":  # shared string
        v = cell.find(f"{_NS}v")
        return shared[int(v.text)] if v is not None and v.text else ""
    if t == "inlineStr":
        return "".join(el.text or "" for el in cell.iter(f"{_NS}t"))
    v = cell.find(f"{_NS}v")
    return v.text if v is not None and v.text else ""


def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def xlsx_to_markdown(path) -> list[tuple[str, str]]:
    """Returns [(sheet_name, markdown_table_text), ...] for all sheets."""
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{_NS}si"):
            shared.append("".join(el.text or "" for el in si.iter(f"{_NS}t")))

    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    rid_to_target = {r.get("Id"): r.get("Target") for r in rels.findall(f"{rel_ns}Relationship")}
    rid_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

    sheets: list[tuple[str, str]] = []
    for sheet in wb.iter(f"{_NS}sheet"):
        name = sheet.get("name")
        target = rid_to_target.get(sheet.get(rid_attr), "")
        member = "xl/" + target.lstrip("/") if not target.startswith("xl/") else target
        if member not in z.namelist():
            sheets.append((name, ""))  # explicit gap, not silent
            continue
        root = ET.fromstring(z.read(member))
        lines = []
        for row in root.iter(f"{_NS}row"):
            cells: dict[int, str] = {}
            for c in row.findall(f"{_NS}c"):
                txt = _cell_text(c, shared).strip()
                if txt:
                    cells[_col_index(c.get("r", "A1"))] = txt
            if not cells:
                continue
            width = max(cells) + 1
            lines.append("| " + " | ".join(cells.get(i, "") for i in range(width)) + " |")
        sheets.append((name, unicodedata.normalize("NFC", "\n".join(lines))))
    return sheets


if __name__ == "__main__":
    # self-check on the corpus files the current manifest lists (xlsx and html only)
    import json
    from pathlib import Path

    from rnd_factlink_mcp import corpus_roots

    manifest = Path(__file__).resolve().parents[2] / "vault" / "meta" / "manifest.jsonl"
    rows = [json.loads(l) for l in manifest.read_text(encoding="utf-8").splitlines() if l.strip()]
    checked = 0
    for row in rows:
        if row["ext"] == "xlsx":
            sheets = xlsx_to_markdown(corpus_roots.resolve(row))
            total = sum(len(t) for _, t in sheets)
            assert sheets and total > 0, f"{row['file_id']}: empty xlsx text"
            print(f"xlsx ok: {row['file_id']}: {len(sheets)} sheets, {total} chars")
            checked += 1
        elif row["ext"] == "html":
            md = html_to_markdown(corpus_roots.resolve(row).read_text(encoding="utf-8", errors="replace"))
            hangul = sum(1 for c in md if 0xAC00 <= ord(c) <= 0xD7A3)
            assert hangul > 200, f"{row['file_id']}: too little Hangul ({hangul})"
            print(f"html ok: {row['file_id']}: {len(md)} chars")
            checked += 1
    print(f"convert self-check: {checked} file(s) converted")
