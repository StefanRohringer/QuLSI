# Nur für Entwicklung und Tests (Profil "dev").
FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.142.2 uvicorn==0.53.0
COPY server.py /srv/server.py
WORKDIR /srv
# Läuft nicht als root (feste ID, damit Rechte auf ./data passen)
RUN useradd --system --uid 10001 --no-create-home --home-dir /tmp qulsi
USER 10001
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
