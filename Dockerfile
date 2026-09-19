FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r /app/requirements.txt

COPY gee_fire_context_labeling.py /app/gee_fire_context_labeling.py
COPY web_server.py /app/web_server.py
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 10000

# Fallback Dockerfile : Render démarre toujours le serveur HTTP.
# La labellisation GEE tourne ensuite dans un thread de fond.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
