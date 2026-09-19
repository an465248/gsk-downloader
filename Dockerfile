FROM python:3.12-slim

# ffmpeg = server-merge fallback ke liye (default download user device pe hota hai)
# nodejs 22 + canvas build-tools = PO-Token (bgutil) server ke liye.
# PO-Token se YouTube bot-check ("Sign in to confirm you're not a bot") bina
# login-cookies ke bypass hota hai. POT_ENABLED=0 (default) par POT server
# start hi nahi hota — purana behavior 100% same rehta hai.
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates gnupg git \
    python3 make g++ pkg-config \
    libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev libpixman-1-dev \
  && mkdir -p /etc/apt/keyrings \
  && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
  && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

# PO-Token provider server (bgutil 2.0.0) — plugin version se match zaroori.
RUN git clone --single-branch --branch 2.0.0 --depth 1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
  && cd /opt/bgutil/server && npm ci && npx tsc && npm prune --omit=dev \
  && (node -e "require('/opt/bgutil/server/node_modules/canvas'); console.log('canvas ok')" \
    || echo "WARNING: canvas native build failed — POT token low-integrity rahega")

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000
CMD ["sh", "/app/start_server.sh"]
