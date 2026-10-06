FROM python:3.13-slim AS build
# git only while the SDK is installed from its repository rather than from the package index.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m venv /opt/navimow-collector && /opt/navimow-collector/bin/pip install --no-cache-dir .

FROM python:3.13-slim
LABEL org.opencontainers.image.title="Navimow collector" \
      org.opencontainers.image.description="Records Navimow mower Trails and Jobs into a database" \
      org.opencontainers.image.source="https://github.com/randax/navimow-plugin" \
      org.opencontainers.image.licenses="GPL-3.0-only"
COPY --from=build /opt/navimow-collector /opt/navimow-collector
COPY LICENSE /usr/share/doc/navimow-collector/LICENSE
# Not root. Everything that must outlive the container, the login above all, is in one
# directory: mount a volume there.
RUN useradd --system --uid 10001 --no-create-home navimow \
    && install -d -o navimow -g navimow -m 700 /var/lib/navimow-collector
ENV PATH=/opt/navimow-collector/bin:$PATH \
    NAVIMOW_AUTH_STATE_FILE=/var/lib/navimow-collector/tokens.json \
    NAVIMOW_COLLECTOR_STATE_DIR=/var/lib/navimow-collector
USER navimow
VOLUME /var/lib/navimow-collector
EXPOSE 9477
# 503 only when the collection loop has stopped, the one fault a restart fixes. It asks the
# default health.listen: a container that moves it or turns it off needs its own check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9477/health', timeout=4)"]
ENTRYPOINT ["navimow-collector"]
CMD ["collect"]
