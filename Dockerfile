# One image, four roles: the three MCP servers and the agent client all run
# from this build, selected by the compose `command`.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    MINING_AGENT_DATA_MODE=offline \
    MCP_HOST=0.0.0.0

WORKDIR /app

# Dependencies first so the layer caches across source edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# `python -m mining_agent.servers.*` works via PYTHONPATH, so no editable
# install is needed and the image builds without a second network round trip.
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
COPY data ./data
COPY mcp-config.json mcp-config.docker.json ./

RUN useradd --create-home --shell /bin/bash app \
    && mkdir -p /app/output \
    && chown -R app:app /app
USER app

# Smoke-check the fixtures made it into the image.
RUN python -c "from mining_agent.config import Settings; s=Settings.from_env(); \
print('fixtures:', s.fixtures_dir, '->', [p.name for p in s.fixtures_dir.glob('*')])"

CMD ["python", "-m", "mining_agent", "--help"]
