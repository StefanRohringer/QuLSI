# Tresor für Geheimnisse der Connectoren (age-verschlüsselt). Siehe qulsi_tresor.py.
FROM python:3.12-slim
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
WORKDIR /app
COPY qulsi_tresor.py .
ENV PYTHONUNBUFFERED=1
# Läuft nicht als root (feste ID, damit Rechte auf ./data passen)
RUN useradd --system --uid 10001 --no-create-home --home-dir /tmp qulsi
USER 10001
EXPOSE 8400
ENTRYPOINT ["python", "qulsi_tresor.py"]
CMD ["dienst"]
