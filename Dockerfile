# Ketchup web simulator: one process, CPU only.
#   docker build -t ketchup-sim .
#   docker run -p 8000:8000 -e BASETEN_API_KEY -e META_API_KEY ketchup-sim
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 KETCHUP_CALENDAR=mock HOST=0.0.0.0 PORT=8000
WORKDIR /app
COPY requirements.txt requirements-sim.txt ./
RUN pip install --no-cache-dir torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements-sim.txt
# MMS-TTS weights baked into the image, so start-up needs no download
RUN python -c "from transformers import AutoTokenizer, VitsModel; VitsModel.from_pretrained('facebook/mms-tts-eng'); AutoTokenizer.from_pretrained('facebook/mms-tts-eng')"
ENV HF_HUB_OFFLINE=1
COPY brain brain
COPY config config
COPY pebble/pi_pebble.py pebble/pi_pebble.py
COPY web web
EXPOSE 8000
CMD ["python", "web/sim.py"]
