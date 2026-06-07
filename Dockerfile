FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium + deps de sistema (para tools UI v0)
# `playwright install-deps` nao suporta Debian Trixie oficialmente e falha em ttf-*;
# entao instalamos as libs manualmente e depois so o binario do Chromium.
# PLAYWRIGHT_BROWSERS_PATH fixa o cache num caminho compartilhado — necessario
# porque o container roda como UID nao-root sem HOME, e o default (~/.cache)
# resolveria para /.cache (vazio) em runtime.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates fonts-liberation libasound2 libatk-bridge2.0-0 \
        libatk1.0-0 libatspi2.0-0 libcairo2 libcups2 libdbus-1-3 libdrm2 \
        libexpat1 libgbm1 libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 \
        libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
        libxkbcommon0 libxrandr2 xdg-utils \
    && rm -rf /var/lib/apt/lists/* \
    && playwright install chromium \
    && chmod -R a+rX /opt/ms-playwright

COPY swagger.json .
COPY src/ ./src/
COPY templates/ ./templates/
RUN mkdir -p /app/data

EXPOSE 47321

# MCP Server
ENV MCP_SERVER_NAME="swagger-mcp"
ENV MCP_TRANSPORT="streamable-http"
ENV MCP_HOST="0.0.0.0"
ENV MCP_PORT="47321"

# Swagger / OpenAPI
ENV SWAGGER_URL=""

# API (optional — auto-detected from spec)
ENV API_BASE_URL=""

# OAuth2
ENV OAUTH_CLIENT_ID=""
ENV OAUTH_CLIENT_SECRET=""
ENV OAUTH_REDIRECT_URI="http://localhost:47321/auth/callback"
ENV OAUTH_AUTH_URL=""
ENV OAUTH_TOKEN_URL=""
ENV OAUTH_SCOPE="openid"

# UI v0 (Playwright)
ENV OLIST_UI_BASE_URL="https://erp.olist.com"
ENV OLIST_UI_SESSION_FILE="/app/data/ui-v0-session.json"
ENV OLIST_UI_LOG_FILE="/app/data/ui-v0.log"
ENV OLIST_UI_HEADLESS="true"
ENV OLIST_UI_USER=""
ENV OLIST_UI_PASSWORD=""

# Etiqueta tools — publica via daemon HTTP do servico oneOS `temp`
ENV TEMP_PUBLISH_URL="https://temp.pana.oneos.work/etiquetas/publish"
ENV TEMP_PUBLISH_TOKEN=""
ENV ONEOS_TZ="America/Fortaleza"

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:47321/health', timeout=5.0)" || exit 1

CMD ["python", "-m", "src.server"]
