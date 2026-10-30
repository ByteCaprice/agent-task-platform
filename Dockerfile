FROM python:3.12-slim AS app-source
WORKDIR /app
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY domain ./domain
COPY framework ./framework
COPY infra ./infra
COPY interfaces ./interfaces
COPY orchestration ./orchestration
COPY agent_hub ./agent_hub
COPY plugins ./plugins
COPY migrations ./migrations
COPY scripts ./scripts

# Optional image for workers executing runtime.type: pi Agents.
FROM node:22.19-bookworm-slim AS pi
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv && \
    rm -rf /var/lib/apt/lists/*
COPY --from=app-source /app /app
WORKDIR /app
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir . && \
    pi_node_dir=$(/opt/venv/bin/python -I -c 'from framework.runtime.pi.transport import NODE_DIR; print(NODE_DIR)') && \
    npm ci --ignore-scripts --prefix "$pi_node_dir" && \
    /opt/venv/bin/agent-task-platform init-config --config-dir /app/config
ENV PATH="/opt/venv/bin:${PATH}" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8765
ENTRYPOINT ["agent-task-platform"]
CMD ["worker", "--config-dir", "/app/config"]

# Default, non-Pi image deliberately omits Node and npm.
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=app-source /app /app
RUN pip install --no-cache-dir . && \
    agent-task-platform init-config --config-dir /app/config
EXPOSE 8765
ENTRYPOINT ["agent-task-platform"]
CMD ["serve", "--api-only", "--config-dir", "/app/config", "--host", "0.0.0.0"]
