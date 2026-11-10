"""Boot warm-up state and release identity for the public HTTP deployment.

Why this exists (UPDATE-PLAN-R1 §1, C6'/C8). A freshly started container accepts
traffic before its caches, tokenizer and index pages are loaded, so the first
public query paid the entire cold cost — and an unattended `restart:
unless-stopped` restart exposes that cost with no operator present. The server
now warms the local search path in a worker thread and answers MCP requests with
a retry-able 503 until the warm pass finishes.

Two deliberate limits, both load-bearing:

1. The warm pass never calls the remote query-embedding backend. It drives the
   production matrix path with a fixed local vector (search_hybrid.
   rank_precomputed_embedding), so no Workers AI neuron is spent and no
   query-log record is written. Workers AI DNS/TLS, credentials and the daily
   budget therefore stay OUTSIDE readiness: `/readyz=ready` promises that the
   local retrieval path is warm, NOT that a search will succeed.

2. The gate is bounded and fails OPEN. Index consistency is already checked
   fail-closed before the socket is bound (server._run_http), so warm-up is a
   latency optimisation, not a correctness gate. A bug in warm-up-only code must
   never be able to take a working server offline: after WARM_DEADLINE_S, or on
   any exception, the gate opens and the state is reported as degraded/failed —
   the worst case is the pre-R1a behaviour (a slow first query), never an
   outage. The planned-cutover path stays strict because the cutover script
   refuses to lift the kill switch on anything but `ready`.

Release identity: the id is derived from the release manifest's per-file sha256
map, so the same code rebuilt under a new mutable tag keeps its id and changed
code cannot reuse one. It is opaque (12 hex chars) and carries no timestamp,
commit or path.
"""

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .paths import REPO_ROOT

STATUS_STARTING = "starting"
STATUS_WARMING = "warming"
STATUS_READY = "ready"
STATUS_DEGRADED = "degraded"  # deadline passed while still warming — gate open
STATUS_FAILED = "failed"      # warm pass raised — gate open, operator must look

# Statuses that let traffic through. "ready" is the normal one; the other two are
# the bounded fail-open described above.
SERVING_STATUSES = (STATUS_READY, STATUS_DEGRADED, STATUS_FAILED)

MANIFEST_PATH = REPO_ROOT / "release_manifest.json"
# Diagnostics the public endpoint deliberately does NOT carry (failure stage,
# exception type, per-stage timings). Written to the container tmpfs and read by
# the operator with `docker exec`, never over HTTP.
STATE_FILE = Path(os.environ.get("RND_FACTLINK_READINESS_FILE", "/tmp/readiness.json"))


def warm_deadline_s() -> float:
    """Seconds to keep the gate closed before failing open. Read per call so a
    deployment can retune it without a code change (see Dockerfile HEALTHCHECK
    --start-period, which must stay larger so the honest state is still visible
    to the container health check after the gate opens)."""
    return float(os.environ.get("RND_FACTLINK_WARM_DEADLINE_S", "60"))


_lock = threading.Lock()
_state: dict = {
    "status": STATUS_STARTING,
    "stage": None,
    "stages_ms": {},
    "error": None,
    "started_at": None,
    "finished_at": None,
    "note": None,
}
_warm_task = None  # asyncio.Task — kept referenced so it is never GC'd mid-flight
_release_id: str | None = None
# Operator-only diagnostics merged into the state file (never into /readyz):
# URL registry digest and null-URL set digest (server.url_registry_diagnostics).
_diagnostics: dict = {}


# --------------------------------------------------------------------------- #
# release identity
# --------------------------------------------------------------------------- #

def compute_release_id(manifest: dict) -> str:
    """Opaque 12-hex id over the manifest's per-file digest map. Shared by the
    bundle builder and the server so the two can never drift apart."""
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return ""
    blob = "\n".join(f"{path}\t{digest}" for path, digest in sorted(files.items()))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def release_id() -> str:
    """"dev" outside a bundle, "unknown" when the manifest is unusable, else the
    computed id. It is computed here rather than read from the manifest field, so
    a hand-edited manifest cannot make one release impersonate another.

    "unknown" is not a boot failure on purpose: refusing to serve because a
    diagnostic file is unreadable would trade a working service for a label. The
    deployment gate is the cutover script, which refuses to lift the kill switch
    unless the running release matches the one it deployed.
    """
    global _release_id
    if _release_id is not None:
        return _release_id
    if not MANIFEST_PATH.exists():
        _release_id = "dev"
        return _release_id
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _release_id = "unknown"
        return _release_id
    computed = compute_release_id(manifest)
    recorded = manifest.get("release_id")
    if not computed or (recorded and recorded != computed):
        _release_id = "unknown"
    else:
        _release_id = computed
    return _release_id


# --------------------------------------------------------------------------- #
# state machine
# --------------------------------------------------------------------------- #

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_state_file() -> None:
    """Atomic replace — `docker exec cat` must never see a half-written file.

    Two writers are possible at once: the warm thread recording a stage, and the
    event loop's deadline task flipping the status. So the snapshot is taken
    under the lock and copies stages_ms (json.dumps with an indent runs the
    pure-Python encoder, which raises if the dict grows mid-iteration), and each
    writer stages through its own temp name before the atomic rename.
    """
    with _lock:
        snapshot = {**_state, "stages_ms": dict(_state["stages_ms"]),
                    "release": release_id(), "diagnostics": dict(_diagnostics)}
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(
            f"{STATE_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:  # noqa: BLE001 — diagnostics must never break serving
        pass


def _set(only_if_status: tuple | None = None, **fields) -> bool:
    """Apply a state change, optionally only from an expected status.

    The guard is what makes the deadline safe. Without it the deadline task can
    read "still warming", lose the race to a worker that finishes a millisecond
    later, and then overwrite a legitimate `ready` with `degraded` — leaving a
    server that warmed perfectly well answering /readyz 503 forever, which in a
    planned cutover means the release is rolled back for no reason.
    """
    with _lock:
        if only_if_status is not None and _state["status"] not in only_if_status:
            return False
        _state.update(fields)
        # stages_ms is copied too: the warm thread keeps inserting into it, and
        # formatting a dict iterates it (same hazard as the file write below)
        snapshot = {**_state, "stages_ms": dict(_state["stages_ms"])}
    _write_state_file()  # takes its own locked snapshot
    if snapshot["status"] in (STATUS_READY, STATUS_DEGRADED, STATUS_FAILED):
        print(f"readiness: {snapshot['status']} release={release_id()} "
              f"stage={snapshot['stage']} error={snapshot['error']} "
              f"stages_ms={snapshot['stages_ms']}", flush=True)
    return True


def state() -> dict:
    """Full internal state (diagnostics). Never serialized to a public response."""
    with _lock:
        return {**_state, "stages_ms": dict(_state["stages_ms"]),
                "release": release_id(), "diagnostics": dict(_diagnostics)}


def set_diagnostics(**fields) -> None:
    """Attach operator diagnostics to the state file. Values are plain scalars
    (digests, counts); nothing here is ever served over HTTP."""
    with _lock:
        _diagnostics.update(fields)
    _write_state_file()


def status() -> str:
    with _lock:
        return _state["status"]


def serving_allowed() -> bool:
    """False only while the warm pass is still expected to finish shortly."""
    return status() in SERVING_STATUSES


def is_ready() -> bool:
    """Strict readiness — what /readyz answers 200 for and what the cutover
    script requires before lifting the kill switch. `degraded`/`failed` serve
    traffic but are never reported as ready."""
    return status() == STATUS_READY


# --------------------------------------------------------------------------- #
# warm-up driver
# --------------------------------------------------------------------------- #

def deterministic_warm_vector(dim: int):
    """Fixed non-zero unit vector matching the index's dimension and dtype.

    Non-zero matters: an all-zero vector makes every score tie, which takes a
    different sort path than a real query. The values themselves are irrelevant
    to what is being warmed (full mmap page-in, BLAS kernel, stable argsort), so
    a fixed seed keeps the warm pass byte-for-byte reproducible.
    """
    import numpy as np
    rng = np.random.default_rng(20260825)
    vec = rng.standard_normal(dim).astype(np.float32)
    return vec / np.linalg.norm(vec)


def _run_warm(warm_fn) -> None:
    """Runs in a worker thread. Records per-stage timings and never raises."""
    stage_started = [time.perf_counter()]

    def stage(name: str) -> None:
        with _lock:
            previous = _state["stage"]
            if previous:
                _state["stages_ms"][previous] = int(
                    (time.perf_counter() - stage_started[0]) * 1000)
            _state["stage"] = name
        stage_started[0] = time.perf_counter()
        # Persist per stage, not only on transitions: when a warm pass hangs, the
        # stage it hung in is the whole diagnosis, and the operator can only read
        # this file (`docker exec ... cat`), never a partial in-memory state.
        _write_state_file()

    try:
        warm_fn(stage)
    except BaseException as e:  # noqa: BLE001 — a warm-up bug must not kill serving
        with _lock:
            failed_stage = _state["stage"]
        _set(status=STATUS_FAILED, stage=failed_stage, finished_at=_now(),
             error=f"{type(e).__name__}: {e}")
        return
    stage("")  # flush the last stage's timing
    if not _set(only_if_status=(STATUS_WARMING,), status=STATUS_READY, stage=None,
                error=None, finished_at=_now(), note=None):
        # the deadline flipped us to degraded first — promote, and say so
        _set(only_if_status=(STATUS_DEGRADED,), status=STATUS_READY, stage=None,
             error=None, finished_at=_now(), note="warm finished after the deadline")


def start_warmup(warm_fn) -> None:
    """Run the warm pass off the event loop and arm the fail-open deadline.

    A dedicated daemon thread, not asyncio.to_thread: the default executor is
    joined during interpreter shutdown, so a warm pass wedged in BLAS or on a
    stuck mmap read would hold the process open past uvicorn's graceful budget
    and turn an ordinary container swap into a SIGKILL. A daemon thread cannot
    do that. It also cannot be cancelled, which is why the deadline only flips
    the state (opening the gate) and lets the thread run on — if it does finish
    successfully later, the state is promoted back to ready.
    """
    global _warm_task
    import asyncio

    _set(status=STATUS_WARMING, started_at=_now())
    worker = threading.Thread(target=_run_warm, args=(warm_fn,),
                              name="krnd-warmup", daemon=True)
    worker.start()

    async def _driver() -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + warm_deadline_s()
        while worker.is_alive() and loop.time() < deadline:
            await asyncio.sleep(0.1)
        if worker.is_alive():
            with _lock:
                stage = _state["stage"]
            _set(only_if_status=(STATUS_WARMING,), status=STATUS_DEGRADED,
                 error=f"warm-up exceeded {warm_deadline_s():g}s at stage {stage!r} "
                       "— gate opened, requests are served cold")

    _warm_task = asyncio.get_running_loop().create_task(_driver())
