# Voice-Dienst (STT + TTS). Für CUDA nutzt faster-whisper die cuBLAS/cuDNN-Pakete aus pip.
#   docker build --build-arg WITH_ML=false ...   -> nur Fake-Provider (Tests)
FROM python:3.12-slim
ARG WITH_ML=true
WORKDIR /srv
COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "$WITH_ML" = "true" ]; then \
      pip install --no-cache-dir -r requirements-ml.txt nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"; \
    fi
COPY qulsi_voice ./qulsi_voice
ENV PYTHONUNBUFFERED=1 VOICE_MODELS=/models/voice
# faster-whisper findet die CUDA-Bibliotheken aus den nvidia-Paketen über LD_LIBRARY_PATH
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib
# Läuft nicht als root (feste ID, damit Rechte auf ./data passen)
RUN useradd --system --uid 10001 --no-create-home --home-dir /tmp qulsi
USER 10001
EXPOSE 8300
CMD ["uvicorn", "qulsi_voice.main:app", "--host", "0.0.0.0", "--port", "8300"]
