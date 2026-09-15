FROM node:22.18.0-bookworm-slim AS node
FROM python:3.13-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
WORKDIR /opt/frontend
COPY templates/fullstack-v1/frontend/package.json ./package.json
RUN npm install --ignore-scripts --no-audit --no-fund
COPY templates/fullstack-v1/backend/pyproject.toml /opt/backend/pyproject.toml
RUN python -c "import tomllib,subprocess; p=tomllib.load(open('/opt/backend/pyproject.toml','rb')); subprocess.check_call(['pip','install','--no-cache-dir',*p['project']['dependencies']])"
COPY sandbox/check.py /opt/forgeai/check.py
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /tmp
USER 65534:65534
