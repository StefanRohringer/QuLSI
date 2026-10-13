# Bonsai-27B braucht den llama.cpp-Fork von PrismML. Normales llama.cpp, Ollama
# und LM Studio können die ternären GGUF-Dateien (PQ2_0, PTQ1_0) nicht laden.
#
# BACKEND=cuda (Standard) baut für RTX 30xx (86), 40xx (89) und 50xx (120).
# BACKEND=cpu baut eine reine CPU-Version (langsam, nur zum Testen).
ARG CUDA_VERSION=12.8.1
ARG BACKEND=cuda

FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04 AS build-cuda
FROM ubuntu:24.04 AS build-cpu
FROM build-${BACKEND} AS build
ARG BACKEND
ARG LLAMA_REPO=https://github.com/PrismML-Eng/llama.cpp
# Für reproduzierbare Builds hier einen Commit eintragen statt des Zweigs.
ARG LLAMA_REF=prism
ARG CUDA_ARCHS="86;89;120"
RUN apt-get update && apt-get install -y --no-install-recommends git cmake build-essential ca-certificates libssl-dev libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 50 --branch "${LLAMA_REF}" "${LLAMA_REPO}" /src || \
    (git clone "${LLAMA_REPO}" /src && git -C /src checkout "${LLAMA_REF}")
WORKDIR /src
RUN if [ "$BACKEND" = "cuda" ]; then EXTRA="-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCHS}"; else EXTRA="-DGGML_NATIVE=OFF"; fi && \
    cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF $EXTRA && \
    cmake --build build -j"$(nproc)" --target llama-server && \
    git rev-parse HEAD > /src/build/bin/COMMIT

FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu24.04 AS run-cuda
FROM ubuntu:24.04 AS run-cpu
FROM run-${BACKEND}
RUN apt-get update && apt-get install -y --no-install-recommends libssl3 libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/bin/llama-server /usr/local/bin/llama-server
COPY --from=build /src/build/bin/COMMIT /COMMIT
COPY start.sh /start.sh
RUN chmod +x /start.sh
# Läuft nicht als root (feste ID, damit Rechte auf ./data passen)
RUN useradd --system --uid 10001 --no-create-home --home-dir /tmp qulsi
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s CMD curl -fs "http://localhost:${LLM_PORT:-8080}/health" || exit 1
ENTRYPOINT ["/start.sh"]
