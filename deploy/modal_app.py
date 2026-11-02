"""Modal deploy for the Ketchup web simulator (web/sim.py): one stable URL.

  modal deploy deploy/modal_app.py

The stdlib server runs as a subprocess on port 8000 behind @modal.web_server.
CPU only. Sessions live in memory, so there is at most one container, and it
takes many requests at once. Keys come from the Modal Secret "ketchup-keys"
(BASETEN_API_KEY, META_API_KEY). Calendars are the seeded mocks; nothing
touches Google. MMS-TTS weights are downloaded while the image builds.

Docs used: https://modal.com/docs/guide/webhooks (web_server, concurrent),
https://modal.com/docs/guide/images (uv/pip install, run_commands, env,
add_local_dir), https://modal.com/docs/guide/scale (max_containers,
min_containers, scaledown_window), https://modal.com/docs/guide/secrets.
"""
import subprocess
from pathlib import Path

import modal

REPO = Path(__file__).resolve().parent.parent
APP_DIR = "/root/ketchup"
MMS = "facebook/mms-tts-eng"

image = (
    modal.Image.debian_slim(python_version="3.13")
    .pip_install("torch==2.15.0", index_url="https://download.pytorch.org/whl/cpu")
    .pip_install("google-auth", "google-api-python-client", "transformers==5.22.1", "tokenizers==0.23.3",
                 "safetensors==0.8.0", "huggingface_hub==2.4.0", "numpy==2.5.4", "scipy==1.18.1")
    .run_commands(  # bake the voice weights into the image: short cold starts, no download at runtime
        f"python -c \"from transformers import AutoTokenizer, VitsModel; "
        f"VitsModel.from_pretrained('{MMS}'); AutoTokenizer.from_pretrained('{MMS}')\"")
    .env({"OMP_NUM_THREADS": "1", "HF_HUB_OFFLINE": "1", "KETCHUP_CALENDAR": "mock",
          "HOST": "0.0.0.0", "PORT": "8000", "PYTHONUNBUFFERED": "1"})
    # only code and non-secret config: never .env, key files, state/ or traces/
    .add_local_dir(REPO / "brain", f"{APP_DIR}/brain", ignore=["__pycache__"])
    .add_local_dir(REPO / "config", f"{APP_DIR}/config")
    .add_local_dir(REPO / "web", f"{APP_DIR}/web", ignore=["__pycache__"])
    .add_local_file(REPO / "pebble/pi_pebble.py", f"{APP_DIR}/pebble/pi_pebble.py")
)

app = modal.App("ketchup-sim", image=image)


@app.function(
    secrets=[modal.Secret.from_name("ketchup-keys")],
    cpu=1.0,              # one physical core; torch runs 1 thread
    memory=2048,          # MiB; the server uses about 0.7 GB with MMS loaded
    max_containers=1,     # sessions are in memory: never split them across containers
    min_containers=0,     # scale to zero when nobody is using it
    scaledown_window=300,  # stay warm 5 min after the last request
)
@modal.concurrent(max_inputs=100)  # polling + audio + TTS from several judges at once
@modal.web_server(8000, startup_timeout=120)
def web():
    subprocess.Popen(["python", f"{APP_DIR}/web/sim.py"], cwd=APP_DIR)
