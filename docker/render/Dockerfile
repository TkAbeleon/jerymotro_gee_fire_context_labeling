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

EXPOSE 10000

# Render doit toujours lancer le serveur HTTP.
# Le worker GEE est démarré en arrière-plan par web_server.py.
ENTRYPOINT ["uvicorn", "web_server:app", "--host", "0.0.0.0", "--port", "10000"]
