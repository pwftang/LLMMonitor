FROM python:3.13-slim

WORKDIR /app
COPY server/pyproject.toml server/
RUN pip install --no-cache-dir ./server

COPY server/ server/
COPY web/ web/

# One volume holds config + SQLite data — mount it to migrate/backup.
#   podman build -t llmmonitor -f Containerfile .
#   podman run -p 8400:8400 -v llmmonitor-data:/data llmmonitor
ENV HUB_CONFIG=/data/hub.toml
VOLUME /data
EXPOSE 8400
WORKDIR /app/server
CMD ["python", "-m", "hub"]
