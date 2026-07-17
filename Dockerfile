FROM python:3.12-slim-bookworm

ARG FLOWMIND_GIT_COMMIT=unversioned

LABEL org.opencontainers.image.title="FlowMind" \
      org.opencontainers.image.revision="${FLOWMIND_GIT_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/tmp/.cache

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        libatomic1 \
        libexpat1 \
        libgl1 \
        libgomp1 \
        libx11-6 \
        libxau6 \
        libxcb1 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.runtime.txt .
RUN python -m pip install --no-cache-dir --requirement requirements.runtime.txt \
    && python -c "import joblib, lightgbm, networkx, pandas, sklearn, sumolib, traci"

COPY . .

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin flowmind \
    && mkdir -p /app/results /tmp/matplotlib /tmp/.cache \
    && chown -R flowmind:flowmind /app/results /tmp/matplotlib /tmp/.cache

USER flowmind

ENV FLOWMIND_RESULTS_DIR=/app/results \
    FLOWMIND_WEB_RESULTS_DIR=/app/results/web_demo \
    FLOWMIND_IMAGE_COMMIT=${FLOWMIND_GIT_COMMIT} \
    FLOWMIND_CONTROLLER_COMMIT=${FLOWMIND_GIT_COMMIT}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, pathlib, sumolib, urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5).read(); sumolib.checkBinary('sumo'); p=pathlib.Path('/app/results/.healthcheck'); p.write_text('ok'); p.unlink()"]

CMD ["python", "-m", "uvicorn", "api.web_dashboard:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
