"""Turn a spoken plan phrase into structured JSON with Muse Spark Open."""
import json
import os
import sys
import urllib.request

BASETEN_URL = "https://inference.baseten.co/v1/chat/completions"
MODEL = "meta-models/muse-spark-open-1026"
SYSTEM = (
    "Extract the plan from what was said. Reply with JSON only: "
    '{"activity": ..., "when": ..., "duration_min": ...}. Use null when not said.'
)


def parse_plan(text: str) -> dict:
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": text},
        ],
        "max_tokens": 800,
    }).encode()
    req = urllib.request.Request(BASETEN_URL, data=body, headers={
        "Authorization": f"Bearer {os.environ['BASETEN_API_KEY']}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        message = json.load(resp)["choices"][0]["message"]
    return {
        "plan": json.loads(message["content"]),
        "reasoning": message.get("reasoning_content"),
    }


if __name__ == "__main__":
    result = parse_plan(" ".join(sys.argv[1:]))
    print(json.dumps(result["plan"]))
