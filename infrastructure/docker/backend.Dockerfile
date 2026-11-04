# syntax=docker/dockerfile:1.7
# AI 3D Studio backend (FastAPI). Build from the repository root:
#   docker build -f infrastructure/docker/backend.Dockerfile -t ai3d-backend .
#
# Opt-in variant with a CPU build of COLMAP 4 for local photogrammetry
# (much larger; see docs/INSTALLATION.md, "Container images"):
#   docker build -f infrastructure/docker/backend.Dockerfile --target backend-colmap \
#     -t ai3d-backend:colmap .
#
# Build arguments:
#   AI3D_EXTRAS  optional extras of ai3d-backend to install, space separated.
#                Default "postgres usdz". Add "reconstruction" for Open3D
#                point-cloud filtering (large; not needed for photogrammetry).
#   COLMAP_SPEC  conda-forge package spec used by the backend-colmap target.

ARG PYTHON_IMAGE=python:3.11-slim-trixie

# ---------------------------------------------------------------------------
# Python dependencies and the two workspace packages in a self-contained venv.
FROM ${PYTHON_IMAGE} AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
WORKDIR /app
ARG AI3D_EXTRAS="postgres usdz"

# Dependencies first (cached unless the lockfile or the extras change).
COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/README.md backend/
COPY local-engine/pyproject.toml local-engine/README.md local-engine/
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; extras=""; for e in $AI3D_EXTRAS; do extras="$extras --extra $e"; done; \
    uv sync --frozen --no-dev --all-packages $extras --no-install-workspace

COPY backend/ai3d backend/ai3d
COPY local-engine/ai3d_engine local-engine/ai3d_engine
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eu; extras=""; for e in $AI3D_EXTRAS; do extras="$extras --extra $e"; done; \
    uv sync --frozen --no-dev --all-packages $extras --no-editable

# ---------------------------------------------------------------------------
# Shared runtime: non-root user, the venv, a /data volume. Nothing is written
# outside /data and /tmp, so the container can run with a read-only root.
FROM ${PYTHON_IMAGE} AS runtime
RUN useradd --create-home --uid 10001 ai3d \
    && mkdir -p /data && chown ai3d:ai3d /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Pixar USD otherwise stages USDZ packages in /var/tmp (read-only in compose).
    TMPDIR=/tmp \
    AI3D_DATA_DIR=/data \
    AI3D_HOST=0.0.0.0 \
    AI3D_ENVIRONMENT=production \
    AI3D_LOG_FORMAT=json \
    AI3D_EXPOSE_PATHS=false
USER ai3d
WORKDIR /home/ai3d
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)"]
CMD ["ai3d-server"]

# ---------------------------------------------------------------------------
# Opt-in: CPU COLMAP 4 from conda-forge. Debian's colmap package is 3.x, which
# the app does not support. Only built for --target backend-colmap.
FROM mambaorg/micromamba:2.3.3 AS colmap
ARG COLMAP_SPEC="colmap=4.2.1=cpu*"
USER root
RUN --mount=type=cache,target=/opt/conda/pkgs \
    micromamba create -y -p /opt/colmap -c conda-forge --override-channels "$COLMAP_SPEC"
# The conda environment is ~1.4 GB (Qt, codecs, CLI tools). Keep only the colmap
# executable, the shared libraries it links (same paths, so its RPATH still
# works) and Qt's headless platform plugins: ~0.4 GB. Library aliases (e.g.
# liblapack.so.3 -> OpenBLAS) are copied as symlinks next to their targets.
RUN set -eu; src=/opt/colmap; dst=/opt/colmap-min; \
    plugins="$src/lib/qt6/plugins/platforms/libqoffscreen.so $src/lib/qt6/plugins/platforms/libqminimal.so"; \
    mkdir -p "$dst/bin" "$dst/lib/qt6/plugins/platforms"; \
    cp "$src/bin/colmap" "$dst/bin/"; \
    cp $plugins "$dst/lib/qt6/plugins/platforms/"; \
    if ldd "$src/bin/colmap" $plugins | grep 'not found'; then exit 1; fi; \
    ldd "$src/bin/colmap" $plugins | awk '$3 ~ "^/opt/colmap/" {print $3}' \
      | xargs -n1 readlink -f | sort -u > /tmp/keep; \
    while read -r f; do cp "$f" "$dst/lib/"; done < /tmp/keep; \
    find "$src/lib" -maxdepth 1 -type l | while read -r l; do \
      if grep -qxF "$(readlink -f "$l")" /tmp/keep; then cp -P "$l" "$dst/lib/"; fi; \
    done; \
    if ldd "$dst/bin/colmap" "$dst"/lib/qt6/plugins/platforms/*.so | grep 'not found'; then exit 1; fi; \
    "$dst/bin/colmap" -h | head -n 1

FROM runtime AS backend-colmap
COPY --from=colmap /opt/colmap-min /opt/colmap
# The command-line tools need no display; keep Qt from looking for one.
ENV AI3D_COLMAP_PATH=/opt/colmap/bin/colmap \
    PATH=$PATH:/opt/colmap/bin \
    QT_QPA_PLATFORM=offscreen

# ---------------------------------------------------------------------------
# Default image. Keep this stage last so a plain `docker build` produces it.
FROM runtime AS backend
