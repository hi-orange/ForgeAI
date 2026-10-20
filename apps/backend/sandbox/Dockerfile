FROM node:22.18.0-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.9.26 AS uv
FROM python:3.13-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=uv /uv /uvx /usr/local/bin/
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
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
COPY sandbox/generate_client.py /opt/forgeai/generate_client.py
COPY templates/fullstack-react-v1/frontend/openapi-ts.config.ts /opt/forgeai/openapi-ts.config.ts
COPY sandbox/preview.py /opt/forgeai/preview.py
COPY sandbox/dependencies.py /opt/forgeai/dependencies.py
COPY sandbox/dependency_specs.py /opt/forgeai/dependency_specs.py
RUN pip install --no-cache-dir 'packaging>=25.0'
COPY sandbox/smoke_contract.json /opt/forgeai/smoke_contract.json
COPY sandbox/smoke_manifest.py /opt/forgeai/smoke_manifest.py
COPY sandbox/capture.mjs /opt/forgeai/capture.mjs
COPY sandbox/probe.mjs /opt/forgeai/probe.mjs
COPY sandbox/smoke_responses.mjs /opt/forgeai/smoke_responses.mjs
COPY sandbox/acceptance.py /opt/forgeai/acceptance.py
COPY sandbox/acceptance.mjs /opt/forgeai/acceptance.mjs
LABEL org.forgeai.check-runtime-version="20"
LABEL org.forgeai.preview-runtime-version="3"
LABEL org.forgeai.dependency-runtime-version="3"
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /tmp
USER 65534:65534
