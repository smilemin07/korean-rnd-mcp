"""Dense embeddings (Phase 2B): BGE-M3 on MPS + NumPy exact search snapshot.

Per plan §Phase 2B: L2-normalized float32 vectors in a .npy snapshot with a
chunk-id manifest, swapped atomically; no ANN (7.6k chunks — exact dot product
is milliseconds). sqlite-vec deliberately not used (pre-v1; prod target is
pgvector).
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .paths import VAULT

MODEL_NAME = "BAAI/bge-m3"
DENSE_DIR = VAULT / "index" / "dense"
EMB_PATH = DENSE_DIR / "embeddings.npy"
IDS_PATH = DENSE_DIR / "chunk_ids.json"
META_PATH = DENSE_DIR / "meta.json"

_model = None


def get_model():
    global _model
    if _model is None:
        import torch
        from sentence_transformers import SentenceTransformer
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _model = SentenceTransformer(MODEL_NAME, device=device)
    return _model


def _model_revision() -> str | None:
    """Commit hash of the local HF snapshot that SentenceTransformer(MODEL_NAME) resolves to.

    Record-only (UPDATE-PLAN-R1 §8-5): the loader is unchanged; this names the snapshot
    actually used for a build so two builds can be compared. None if unresolvable offline.
    """
    try:
        from huggingface_hub import snapshot_download
        return Path(snapshot_download(MODEL_NAME, local_files_only=True)).name
    except Exception:  # noqa: BLE001 — provenance is best-effort, never blocks a build
        return None


def embed_texts(texts: list[str], batch_size: int = 16) -> np.ndarray:
    vecs = get_model().encode(texts, batch_size=batch_size, normalize_embeddings=True,
                              convert_to_numpy=True, show_progress_bar=False)
    return np.asarray(vecs, dtype=np.float32)


def build() -> int:
    import sqlite3
    t0 = time.perf_counter()
    con = sqlite3.connect(VAULT / "index" / "lexical.db")
    rows = con.execute("SELECT chunk_id, text_norm FROM chunks ORDER BY rowid").fetchall()
    con.close()
    if not rows:
        print("FATAL: lexical.db empty — build lexical index first")
        return 1
    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]

    vecs = np.zeros((len(texts), 1024), dtype=np.float32)
    B = 64
    for i in range(0, len(texts), B):
        vecs[i:i + B] = embed_texts(texts[i:i + B])
        if (i // B) % 10 == 0:
            print(f"embedded {min(i + B, len(texts))}/{len(texts)} "
                  f"({time.perf_counter() - t0:.0f}s)", flush=True)

    # atomic-ish snapshot swap (tmp files then rename; ids+meta after array)
    DENSE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = EMB_PATH.with_suffix(".npy.tmp")
    with open(tmp, "wb") as f:  # np.save(path) would append ".npy" to the tmp name
        np.save(f, vecs)
    tmp.replace(EMB_PATH)
    IDS_PATH.write_text(json.dumps(ids), encoding="utf-8")
    META_PATH.write_text(json.dumps({
        "model": MODEL_NAME, "model_revision": _model_revision(), "dim": 1024, "n": len(ids),
        "normalized": True, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }), encoding="utf-8")

    # gate: alignment + self-retrieval sanity (a chunk's own text must retrieve itself top-1)
    norms = np.linalg.norm(vecs, axis=1)
    ok_norm = bool(np.all(np.abs(norms - 1.0) < 1e-3))
    probe_idx = [0, len(ids) // 2, len(ids) - 1]
    q = embed_texts([texts[i] for i in probe_idx])
    top1 = np.argmax(q @ vecs.T, axis=1)
    ok_self = all(int(t) == i for t, i in zip(top1, probe_idx))
    ok = ok_norm and ok_self and len(ids) == vecs.shape[0]
    print(f"n={len(ids)} dim={vecs.shape[1]} normalized={ok_norm} self_retrieval={ok_self} "
          f"seconds={time.perf_counter() - t0:.0f}")
    print(f"GATE 2B embed {'PASS' if ok else 'FAIL'}: {EMB_PATH}")
    return 0 if ok else 1


class DenseIndex:
    def __init__(self):
        if not EMB_PATH.is_file():
            raise FileNotFoundError(f"{EMB_PATH} missing — run scripts/build_dense.py")
        self.vecs = np.load(EMB_PATH, mmap_mode="r")
        self.ids: list[str] = json.loads(IDS_PATH.read_text(encoding="utf-8"))
        self.meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        assert len(self.ids) == self.vecs.shape[0], "ids/embeddings misaligned"

    # NOTE: no search() method here on purpose — retrieval goes through
    # search_hybrid.dense_ranking, which enforces the eligibility mask before
    # the top-k cutoff (v2 contract). An unmasked search path would bypass it.
