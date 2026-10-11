ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89
FROM ${PYTHON_IMAGE} AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY dist ./dist
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /wheels
COPY scripts/fetch-mediamtx.py ./
RUN python fetch-mediamtx.py --output /mediamtx

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg v4l-utils ca-certificates libglib2.0-0 && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 campix && useradd --uid 10001 --gid campix --no-create-home campix \
    && install -d -m 0700 -o campix -g campix /data
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-deps /wheels/*.whl && rm -rf /wheels /tmp/requirements.lock
COPY --from=builder /mediamtx /usr/local/bin/mediamtx
COPY --from=builder /mediamtx.LICENSE /usr/share/doc/mediamtx/LICENSE
ENV CAMPIX_DATA_DIR=/data CAMPIX_HOST=0.0.0.0 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
ENTRYPOINT ["campixd"]
CMD ["serve"]
