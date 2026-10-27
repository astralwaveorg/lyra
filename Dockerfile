# Build the Vue frontend first, then install the Python package into a slim
# runtime image. The frontend is never copied from the working tree: the image
# always contains an artifact built from the current source.

ARG NODE_IMAGE=node:22-alpine
FROM ${NODE_IMAGE} AS web

ARG NPM_REGISTRY=https://registry.npmjs.org

WORKDIR /web
COPY web/package.json web/package-lock.json web/.npmrc ./
RUN npm ci --no-audit --no-fund --registry="${NPM_REGISTRY}"

COPY web/ ./
RUN npm run build \
 && test -f /src/lyra/web/index.html

FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=
# 可选：把 Debian 源换成国内镜像（只换主机名，路径保持不变）。
# 不设就用官方源——上游用户不该被强制走某个地区的镜像。
ARG DEBIAN_MIRROR_HOST=

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# fpcalc provides Chromaprint fingerprints for AcoustID lookups.
RUN set -eux; \
    if [ -n "${DEBIAN_MIRROR_HOST}" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \
            [ -f "$f" ] || continue; \
            sed -i "s|deb\.debian\.org|${DEBIAN_MIRROR_HOST}|g; s|security\.debian\.org|${DEBIAN_MIRROR_HOST}|g" "$f"; \
        done; \
    fi; \
    apt-get update \
 && apt-get install -y --no-install-recommends libchromaprint-tools \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN rm -rf /app/src/lyra/web
COPY --from=web /src/lyra/web ./src/lyra/web

RUN pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        ${PIP_TRUSTED_HOST:+--trusted-host "${PIP_TRUSTED_HOST}"} \
        --timeout 60 --retries 5 \
        . \
 && rm -rf /app/src /app/pyproject.toml

ENV LYRA_CONFIG=/config/lyra.toml \
    LYRA_LIBRARY_ROOT=/music \
    LYRA_DATA_DIR=/data \
    LYRA_WEB_BIND=0.0.0.0 \
    LYRA_WEB_PORT=8080

USER 1000:1000

EXPOSE 8080

LABEL org.opencontainers.image.title="Lyra" \
      org.opencontainers.image.description="Self-hosted lyrics, tags, and library organization for a local music collection" \
      org.opencontainers.image.source="https://github.com/astralwaveorg/lyra" \
      org.opencontainers.image.licenses="MIT"

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["lyra"]
CMD ["serve"]
