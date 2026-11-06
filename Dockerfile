FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Pre-download turn-detector and VAD model files so the first call is not slow.
RUN python -m ava.voice.agent download-files || true
# SERVICE=backend (FastAPI tools and demo page) or SERVICE=worker (LiveKit voice worker)
ENV SERVICE=backend PORT=8080
CMD ["sh", "-c", "if [ \"$SERVICE\" = worker ]; then python -m ava.voice.agent start; else python -c \"import os,uvicorn; uvicorn.run('ava.backend.app:app', host='0.0.0.0', port=int(os.environ['PORT']))\"; fi"]
