FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY qulsi_core ./qulsi_core
ENV PYTHONUNBUFFERED=1
# Läuft nicht als root (feste ID, damit Rechte auf ./data passen)
RUN useradd --system --uid 10001 --no-create-home --home-dir /tmp qulsi
USER 10001
EXPOSE 8100
CMD ["uvicorn", "qulsi_core.main:app", "--host", "0.0.0.0", "--port", "8100"]
