# WSL2/Docker deployment, replacing the native Windows Task Scheduler launcher
# (C:\Scripts\llm-usage-dashboard.ps1 + Startup VBS) -- Docker's own restart
# policy stands in for that hand-rolled PowerShell restart loop, matching the
# same reasoning that moved study-platform and event-radar to this pattern
# (Windows Scheduled Task / hidden-process fragility, see their own Dockerfiles).
#
# DIGEST-PINNED, not floating `python:3.12-slim` -- the same digest quant and
# event-radar already pinned, so Docker's content-addressed layer cache shares
# this base-OS layer across all of them for free, with zero coupling to any
# project's own (separately pinned) dependency layers above it. To pick up a
# real upstream update later, re-resolve deliberately (`docker pull` +
# `docker inspect --format '{{index .RepoDigests 0}}'`) rather than letting it
# drift silently on every rebuild.
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

# Official static uv binary (same pinned digest as quant/event-radar, for the
# same shared-cache reason), not `pip install uv` or a curl installer -- avoids
# needing network access mid-build just to fetch the installer.
COPY --from=ghcr.io/astral-sh/uv@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc /uv /uvx /usr/local/bin/

WORKDIR /app

# Lock file first, own layer -- uv.lock pins every transitive dependency, so
# --frozen guarantees the same resolution every build; this layer only
# rebuilds when dependencies actually change, not on every code edit.
COPY pyproject.toml uv.lock ./
# This WSL2 instance has shown flaky/slow throughput to PyPI before (same
# issue study-platform and event-radar's Dockerfiles document) -- a longer
# per-request timeout absorbs that instead of failing the whole build on one
# slow package.
ENV UV_HTTP_TIMEOUT=120
RUN uv sync --frozen --no-install-project

COPY app.py ledger.py alerts.py services.py noc.py ./

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8095

CMD ["python", "app.py"]
