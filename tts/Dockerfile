# Sprachausgabe auf der GPU. Ein Image je Modell, weil die Modelle unterschiedliche Bibliotheken brauchen:
#   docker compose build tts                              -> TTS_MODELL aus .env (Standard qwen3-tts)
#   docker build --build-arg MODELL=cosyvoice3 tts        -> Fun-CosyVoice3 0.5B
#   docker build --build-arg MODELL=chatterbox tts        -> Chatterbox Multilingual
#   docker build --build-arg MODELL=fake tts              -> ohne Modell (Tests)
# torch kommt immer aus dem CUDA-12.8-Index: die RTX 5060 Ti (Blackwell) läuft erst ab torch 2.7.
FROM python:3.12-slim
ARG MODELL=qwen3-tts
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128
ARG TORCH_VERSION=2.8.0
# CosyVoice hat kein pip-Paket: Quellcode mit Matcha-TTS, für reproduzierbare Builds Commit eintragen
ARG COSYVOICE_REF=main
ENV MODELL=${MODELL}
WORKDIR /srv
RUN pip install --no-cache-dir fastapi==0.118.0 "uvicorn==0.37.0" numpy huggingface_hub
RUN set -e; \
    if [ "$MODELL" != "fake" ]; then \
      apt-get update && apt-get install -y --no-install-recommends git ffmpeg sox libsndfile1 && rm -rf /var/lib/apt/lists/*; \
      pip install --no-cache-dir torch==$TORCH_VERSION torchaudio==$TORCH_VERSION --index-url "$TORCH_INDEX"; \
    fi; \
    case "$MODELL" in \
      cosyvoice3) \
        git clone --recursive https://github.com/FunAudioLLM/CosyVoice /opt/CosyVoice && \
        git -C /opt/CosyVoice checkout "$COSYVOICE_REF" && git -C /opt/CosyVoice submodule update --init --recursive && \
        grep -v -E '^(--|torch|torchaudio|deepspeed|tensorrt|gradio|tensorboard|fastapi|uvicorn|grpcio|onnxruntime==|matplotlib|wget|gdown)' \
          /opt/CosyVoice/requirements.txt > /tmp/cosy.txt && \
        pip install --no-cache-dir -r /tmp/cosy.txt ;; \
      qwen3-tts) \
        pip install --no-cache-dir --no-deps qwen-tts==0.1.1 && \
        pip install --no-cache-dir transformers==4.57.3 accelerate==1.12.0 librosa soundfile sox onnxruntime einops ;; \
      chatterbox) \
        pip install --no-cache-dir --no-deps chatterbox-tts==0.1.7 && \
        pip install --no-cache-dir "numpy<2" librosa==0.11.0 s3tokenizer transformers==5.2.0 diffusers==0.29.0 \
          resemble-perth conformer==0.3.2 safetensors==0.5.3 spacy-pkuseg pykakasi==2.3.0 pyloudnorm omegaconf ;; \
      fake) ;; \
      *) echo "Unbekanntes MODELL: $MODELL" >&2; exit 1 ;; \
    esac
COPY aufbereiten.py bei_bedarf.py qulsi_tts.py schnell.py ./
ENV PYTHONUNBUFFERED=1 TTS_MODELL=${MODELL} HF_HOME=/models/tts/hf
RUN useradd --system --uid 10001 --no-create-home --home-dir /tmp qulsi
USER 10001
EXPOSE 8400
ENTRYPOINT ["python", "qulsi_tts.py"]
CMD ["dienst"]
