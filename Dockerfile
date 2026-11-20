# M.A.R.S. в контейнере: все шесть сервисов запускает run_all.py, наружу открыт только gateway (8000).
# Ollama — отдельный контейнер (docker-compose.yml). Данные пациентов и модели — в томах /data и /models.
#
#   docker build -t mars .
#   docker build --build-arg WITH_MEDIAPIPE=1 -t mars .   # + поиск точек лица на сервере (vision/detector.py)
FROM python:3.12-slim

ARG WITH_MEDIAPIPE=0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    MODELS_DIR=/models \
    SPEECH_VENV=.none \
    PORT=8000

WORKDIR /app

# зависимости отдельным слоем: правка кода не переустанавливает Whisper
COPY speech/requirements.txt /tmp/speech-requirements.txt
RUN pip install --no-cache-dir -r /tmp/speech-requirements.txt \
 && if [ "$WITH_MEDIAPIPE" = "1" ]; then \
        python -m venv /opt/vision-venv \
     && /opt/vision-venv/bin/pip install --no-cache-dir mediapipe==0.10.21; \
    fi

RUN useradd --create-home --uid 10001 mars \
 && mkdir -p /data /models \
 && chown mars:mars /data /models

COPY --chown=mars:mars . /app
# run_all.py берёт vision/.venv, если оно есть: MediaPipe живёт отдельно от numpy 2 для Whisper
RUN if [ -d /opt/vision-venv ]; then ln -s /opt/vision-venv /app/vision/.venv; fi

USER mars
EXPOSE 8000
VOLUME ["/data", "/models"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
CMD ["python", "run_all.py"]
