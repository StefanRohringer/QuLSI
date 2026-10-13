# Vision-Dienst. Standard: CUDA-Build für die RTX 5060 Ti (Blackwell braucht CUDA 12.8+).
#   docker compose build vision                         -> GPU
#   docker build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cpu ...   -> CPU
#   docker build --build-arg WITH_ML=false ...              -> ohne Modelle (nur Tests)
#   docker build --build-arg DETECTOR=yolo-world ...        -> zusätzlich YOLO-World (ultralytics, AGPL-3.0)
FROM python:3.12-slim
ARG WITH_ML=true
ARG DETECTOR=owlv2
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128
WORKDIR /srv
COPY requirements.txt requirements-ml.txt requirements-yolo.txt ./
RUN if [ "$WITH_ML" = "true" ]; then \
      pip install --no-cache-dir torch torchvision --index-url "$TORCH_INDEX" && \
      pip install --no-cache-dir -r requirements-ml.txt; \
    fi
# Nur mit DETECTOR=yolo-world: ultralytics (AGPL-3.0). git für den CLIP-Textencoder, libgl1 für das opencv-Paket von ultralytics
RUN if [ "$WITH_ML" = "true" ] && [ "$DETECTOR" = "yolo-world" ]; then \
      apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 && \
      rm -rf /var/lib/apt/lists/* && \
      pip install --no-cache-dir -r requirements-yolo.txt; \
    fi
RUN pip install --no-cache-dir -r requirements.txt
COPY qulsi_vision ./qulsi_vision
ENV PYTHONUNBUFFERED=1 HF_HOME=/models/hf YOLO_CONFIG_DIR=/models/ultralytics
WORKDIR /models
# Läuft nicht als root (feste ID, damit Rechte auf ./data passen)
RUN useradd --system --uid 10001 --no-create-home --home-dir /tmp qulsi
USER 10001
EXPOSE 8200
CMD ["uvicorn", "qulsi_vision.main:app", "--app-dir", "/srv", "--host", "0.0.0.0", "--port", "8200"]
