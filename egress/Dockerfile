FROM python:3.12-slim
RUN pip install --no-cache-dir pyyaml==6.0.3 cryptography==50.0.1
WORKDIR /app
COPY qulsi_egress.py qulsi_integritaet.py ./
ENV PYTHONUNBUFFERED=1
# Läuft nicht als root (feste ID, damit Rechte auf ./data passen)
RUN useradd --system --uid 10001 --no-create-home --home-dir /tmp qulsi
USER 10001
EXPOSE 3128
CMD ["python", "qulsi_egress.py"]
