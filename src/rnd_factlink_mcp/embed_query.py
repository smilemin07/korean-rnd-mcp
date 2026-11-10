"""Query-embedding backends: local BGE-M3 (default) or Cloudflare Workers AI.

Public deployment embeds ONLY the query remotely (corpus vectors are prebuilt
into vault/index/dense). Design decisions (README.md "개인정보·국외 처리 고지", "이용 제한과 문제 해결"):
- #8  single provider (@cf/baai/bge-m3), no automatic fallback provider
- #11 daily neuron budget, fail-closed on exhaustion — callers surface the
  error and must NEVER degrade to BM25-only silently

Every remote failure raises EmbedUnavailable. Received vectors are
re-normalized to unit L2 before use (compat requirement).
"""

import json
import os
import threading
from datetime import datetime, timezone

import numpy as np

from .paths import VAULT

WORKERS_AI_MODEL = "@cf/baai/bge-m3"
EXPECTED_DIM = 1024
# Workers AI pricing basis for bge-m3: 1075 neurons per 1M input tokens
NEURONS_PER_MTOK = 1075
BUDGET_PATH = VAULT / "derived" / "reports" / "embed_budget.json"
_budget_lock = threading.Lock()


class EmbedUnavailable(RuntimeError):
    """Query embedding cannot be computed right now — search must fail closed."""


def backend() -> str:
    return os.environ.get("RND_FACTLINK_EMBED_BACKEND", "local")


def embed_query(text: str) -> np.ndarray:
    """Unit-normalized float32 query vector via the configured backend."""
    if backend() == "workersai":
        return _embed_workersai(text)
    from .embed_dense import embed_texts
    return embed_texts([text])[0]


def _estimate_neurons(text: str) -> float:
    try:
        from .tokenize_ko import bge_token_count
        tokens = bge_token_count(text)
    except Exception:
        tokens = len(text)  # over-estimate: 1 token per char is conservative
    return max(tokens, 1) * NEURONS_PER_MTOK / 1_000_000


def _daily_cap() -> float:
    return float(os.environ.get("RND_FACTLINK_EMBED_DAILY_NEURONS", "25000"))


def _charge_budget(text: str) -> None:
    """Add this query's estimated neurons to today's counter, fail-closed at cap.
    UTC day boundary matches the Workers AI free-quota reset."""
    est = _estimate_neurons(text)
    today = datetime.now(timezone.utc).date().isoformat()
    with _budget_lock:
        state = {"date": today, "neurons": 0.0, "queries": 0}
        if BUDGET_PATH.exists():
            try:
                prev = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
            except (ValueError, OSError) as e:
                # fail-closed: a corrupt counter must not silently reset the
                # daily cap (review 2026-08-21 P0). Operator deletes the file to
                # recover — absence is a legitimate fresh start, corruption is not.
                raise EmbedUnavailable(
                    "임베딩 예산 상태 파일 손상 — 운영자 확인 필요") from e
            if prev.get("date") == today:
                state = prev
        if state["neurons"] + est > _daily_cap():
            raise EmbedUnavailable(
                "일일 임베딩 예산 소진 — 오늘은 검색이 중단됩니다(자정 UTC 초기화)")
        state["neurons"] = round(state["neurons"] + est, 6)
        state["queries"] = int(state.get("queries", 0)) + 1
        BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = BUDGET_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(BUDGET_PATH)


def _embed_workersai(text: str) -> np.ndarray:
    account = os.environ.get("CF_ACCOUNT_ID")
    token = os.environ.get("CF_WORKERS_AI_TOKEN")
    if not account or not token:
        raise EmbedUnavailable("Workers AI 자격증명 미설정(CF_ACCOUNT_ID/CF_WORKERS_AI_TOKEN)")
    _charge_budget(text)

    import httpx
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{WORKERS_AI_MODEL}"
    try:
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            res = client.post(url, headers={"Authorization": f"Bearer {token}"},
                              json={"text": [text]})
    except httpx.HTTPError as e:
        raise EmbedUnavailable(f"Workers AI 호출 실패({type(e).__name__})") from e
    if res.status_code != 200:
        # never include res.text — error bodies are not ours to relay
        raise EmbedUnavailable(f"Workers AI HTTP {res.status_code}")
    try:
        payload = res.json()
        data = payload["result"]["data"]
        vec = np.asarray(data[0], dtype=np.float32)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise EmbedUnavailable("Workers AI 응답 형식 불일치") from e
    if vec.shape != (EXPECTED_DIM,) or not np.all(np.isfinite(vec)):
        raise EmbedUnavailable(f"Workers AI 벡터 검증 실패(shape={vec.shape})")
    norm = float(np.linalg.norm(vec))
    if not norm > 0:
        raise EmbedUnavailable("Workers AI 영벡터 반환")
    return vec / norm


if __name__ == "__main__":
    # self-test: local backend roundtrip + workersai fail-closed paths (no network)
    assert backend() == "local" or os.environ.get("RND_FACTLINK_EMBED_BACKEND")
    os.environ["RND_FACTLINK_EMBED_BACKEND"] = "workersai"
    os.environ.pop("CF_ACCOUNT_ID", None)
    os.environ.pop("CF_WORKERS_AI_TOKEN", None)
    try:
        embed_query("테스트")
        raise AssertionError("credentials missing must fail closed")
    except EmbedUnavailable:
        pass
    assert 0 < _estimate_neurons("한국어 질의 테스트") < 1
    os.environ["RND_FACTLINK_EMBED_BACKEND"] = "local"
    v = embed_query("국가연구개발 투자방향")
    assert v.shape == (EXPECTED_DIM,) and abs(float(np.linalg.norm(v)) - 1.0) < 1e-3
    print("embed_query self-test passed")
