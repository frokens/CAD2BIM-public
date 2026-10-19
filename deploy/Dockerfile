FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/opt/cad2bim \
    UV_LINK_MODE=copy \
    PATH="/opt/cad2bim/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install "uv==0.9.27" \
    && groupadd --system cad2bim \
    && useradd --system --gid cad2bim --home-dir /nonexistent cad2bim \
    && mkdir -p /var/lib/cad2bim /run/cad2bim-exec \
    && chown -R cad2bim:cad2bim /var/lib/cad2bim /run/cad2bim-exec

COPY pyproject.toml uv.lock README.md alembic.ini ./

# Resolve third-party dependencies before copying application sources. This
# keeps the expensive amd64 dependency layer reusable for ordinary code and
# documentation changes, while the cache mount survives an interrupted build.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --no-install-project

COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable

USER cad2bim

CMD ["cad2bim-mcp-direct"]

FROM runtime AS production
