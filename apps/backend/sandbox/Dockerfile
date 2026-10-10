FROM node:22.18.0-bookworm-slim AS node
FROM python:3.13-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends chromium fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/visual
RUN npm init -y \
    && npm install --ignore-scripts --no-audit --no-fund playwright-core@1.55.0
WORKDIR /opt/frontend
COPY templates/fullstack-react-v1/frontend/package.json ./package.json
COPY templates/fullstack-react-v1/frontend/package-lock.json ./package-lock.json
COPY templates/fullstack-react-v1/frontend/src/index.css ./index.css
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY templates/fullstack-react-v1/backend/pyproject.toml /opt/backend/pyproject.toml
RUN python -c "import tomllib,subprocess; p=tomllib.load(open('/opt/backend/pyproject.toml','rb')); subprocess.check_call(['pip','install','--no-cache-dir',*p['project']['dependencies']])"
COPY sandbox/check.py /opt/forgeai/check.py
COPY sandbox/smoke_contract.json /opt/forgeai/smoke_contract.json
COPY sandbox/capture.mjs /opt/forgeai/capture.mjs
COPY sandbox/probe.mjs /opt/forgeai/probe.mjs
LABEL org.forgeai.check-runtime-version="14"
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /tmp
USER 65534:65534
