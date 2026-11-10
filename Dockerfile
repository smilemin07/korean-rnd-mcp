# korean-rnd-mcp public serving image — built from a public bundle directory
# (scripts/build_public_bundle.py output) as the build context, on the
# serving host (x86_64 native). The bundle mirrors the repo layout, so REPO_ROOT resolves
# to /app and every data path lands inside the image.
#
#   docker build -t korean-rnd-mcp:<bundle-ts> .
#
# Runtime state (query stats, neuron budget) lives on a mounted volume at
# /app/vault/derived/reports; the kill-switch file is a separate read-only
# mount (see deploy notes). No torch, no pdf parsing — slim runtime only.
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 serve
WORKDIR /app

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY src/ src/
COPY models/ models/
COPY vault/ vault/
COPY curation/ curation/
COPY release_manifest.json .

# Normalize the runtime-writable state dir: drop anything a local smoke run
# left in the bundle and reset modes (host filesystem ACLs can mangle tar-extracted
# permission bits in the build context). In production a host volume mounts
# over this path; standalone runs write here.
RUN rm -rf /app/vault/derived/reports \
 && mkdir -p /app/vault/derived/reports \
 && chown serve:serve /app/vault/derived/reports \
 && chmod 700 /app/vault/derived/reports
USER serve

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    RND_FACTLINK_EMBED_BACKEND=workersai \
    RND_FACTLINK_HTTP_HOST=0.0.0.0 \
    PORT=8081 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

EXPOSE 8081

# Readiness probe (UPDATE-PLAN-R1 §1 C6'). Parameters are pre-fixed from
# measurement, not tuned on the serving host: three cold-process warm passes on the dev
# machine took 1.0-1.4s over 11,374 chunks / 46.6MB of vectors, so start-period
# carries roughly a 60x margin for the slower container. It must also stay above
# RND_FACTLINK_WARM_DEADLINE_S (60s), so the gate's bounded fail-open happens
# first and the container then reports the honest degraded/failed state instead
# of hiding it behind "starting". Failing this check never restarts anything —
# compose's restart policy reacts to process exit only.
HEALTHCHECK --interval=10s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-m", "rnd_factlink_mcp.healthcheck"]

CMD ["python", "-m", "rnd_factlink_mcp.server", "--http"]
