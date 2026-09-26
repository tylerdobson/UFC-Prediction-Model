FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/cache \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app
RUN python -m pip install --no-cache-dir uv==0.12.9

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY app.py dashboard.css ./
COPY .streamlit/config.toml ./.streamlit/config.toml
COPY scripts/dashboard-container-entrypoint.sh ./scripts/dashboard-container-entrypoint.sh

RUN uv sync --frozen --extra dashboard --no-dev --python /usr/local/bin/python \
    && chmod 755 ./scripts/dashboard-container-entrypoint.sh

USER 10001:10001
EXPOSE 8501
ENTRYPOINT ["/app/scripts/dashboard-container-entrypoint.sh"]
