# Ein Image für alle Connectoren. Welcher läuft, legt docker-compose fest:
#   python <name>/server.py   (home_assistant, calendar, mail, files, web, vision,
#                              google, microsoft, nas, browser)
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/_lib
# Läuft nicht als root (feste ID, damit Rechte auf ./data passen)
RUN useradd --system --uid 10001 --no-create-home --home-dir /tmp qulsi
USER 10001
EXPOSE 8000
CMD ["python", "web/server.py"]
