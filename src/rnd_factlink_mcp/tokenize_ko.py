"""Korean tokenization for lexical search + BGE-M3 token counting.

- Kiwi (kiwipiepy, version pinned in pyproject; default model — CoNg model is
  reported slow/buggy on Apple Silicon) pre-tokenizes text into search tokens
  because SQLite FTS5 has no Korean tokenizer. Document body and queries MUST
  go through the same function (search_tokens).
- Kept POS: content morphemes — nouns (NNG/NNP/NNB), foreign (SL), numbers (SN),
  hanja (SH), roots (XR), verb/adjective stems (VV/VA).
- BGE-M3 tokenizer (models/bge-m3/tokenizer.json) is the canonical token-count
  basis for chunk sizing (SCHEMA.md §4).

Reproducibility gate (Gate 2A): tests/fixtures/tokenization_golden.json stores
expected outputs for synthetic fixtures; regenerate ONLY on a deliberate
version bump (rerun with --write-golden).
"""

import json
from functools import lru_cache
from pathlib import Path

from kiwipiepy import Kiwi
from tokenizers import Tokenizer

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BGE_TOKENIZER_PATH = REPO_ROOT / "models" / "bge-m3" / "tokenizer.json"
GOLDEN_PATH = REPO_ROOT / "tests" / "fixtures" / "tokenization_golden.json"

KEEP_TAGS = {"NNG", "NNP", "NNB", "SL", "SN", "SH", "XR", "VV", "VA"}


@lru_cache(maxsize=1)
def _kiwi() -> Kiwi:
    return Kiwi()


@lru_cache(maxsize=1)
def _bge() -> Tokenizer:
    if not BGE_TOKENIZER_PATH.is_file():
        raise FileNotFoundError(
            f"{BGE_TOKENIZER_PATH} missing — download with: "
            "hf_hub_download('BAAI/bge-m3', 'tokenizer.json', local_dir='models/bge-m3')")
    return Tokenizer.from_file(str(BGE_TOKENIZER_PATH))


def search_tokens(text: str) -> str:
    """Kiwi content-morpheme tokens joined by spaces (for FTS5 columns and queries)."""
    if not text.strip():
        return ""
    out = []
    for tok in _kiwi().tokenize(text):
        if tok.tag in KEEP_TAGS:
            form = tok.form.lower() if tok.tag == "SL" else tok.form
            out.append(form)
    return " ".join(out)


def fts_match_query(text: str) -> str:
    """FTS5 MATCH expression for a query: every Kiwi token as a quoted string,
    OR-ed together.

    Unquoted tokens are parsed as FTS5 *syntax*, so tokens such as "137.6",
    "1,000", "p." or "한-미" raised `fts5: syntax error` and failed the whole
    search (S1, 2026-09-03; 1,794 corpus tokens, 54% of chunks affected).
    Inside "..." only the double quote is special (escaped by doubling); the
    FTS5 tokenizer then splits the string exactly as it split the indexed text,
    so "137.6" matches the adjacent index tokens `137 6`.

    Returns "" (length 0) when the text yields no tokens — callers must then
    skip the database entirely, as before."""
    tokens = search_tokens(text).split()
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def bge_token_count(text: str) -> int:
    if not text:
        return 0
    return len(_bge().encode(text, add_special_tokens=False).ids)


# synthetic fixtures only — no corpus text (this file's golden output is committed to git)
FIXTURES = [
    "2027년도 국가연구개발 투자방향 및 기준(안)을 검토하였다.",
    "연구개발비 사용 기준 제10조 제5호에 따라 데이터베이스 이용료를 집행할 수 있는가?",
    "Coding rules for embedded software, revision 2020.",
    "제6차 과학기술기본계획(2026-2030) 세부 실천과제 내역 - 국토교통부 소관",
    # numbers here are placeholders for tokenizer testing, not real statistics
    "가상기관 기술수준평가에서 건설·교통 분야는 최고기술국 대비 00.0% 수준이다.",
]


def _current() -> dict:
    import importlib.metadata as im
    return {
        "kiwipiepy_version": im.version("kiwipiepy"),
        "fixtures": {t: {"search_tokens": search_tokens(t), "bge_count": bge_token_count(t)}
                     for t in FIXTURES},
    }


if __name__ == "__main__":
    import sys
    cur = _current()
    if "--write-golden" in sys.argv:
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"golden written: {GOLDEN_PATH}")
        sys.exit(0)
    if not GOLDEN_PATH.is_file():
        sys.exit(f"golden missing — run with --write-golden first: {GOLDEN_PATH}")
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    if golden != cur:
        print("TOKENIZATION REPRODUCIBILITY FAIL")
        if golden["kiwipiepy_version"] != cur["kiwipiepy_version"]:
            print(f"  kiwipiepy: golden {golden['kiwipiepy_version']} vs current {cur['kiwipiepy_version']}")
        for t in FIXTURES:
            if golden["fixtures"].get(t) != cur["fixtures"][t]:
                print(f"  fixture: {t}")
                print(f"    golden : {golden['fixtures'].get(t)}")
                print(f"    current: {cur['fixtures'][t]}")
        sys.exit(1)
    print("tokenization reproducibility PASS")
