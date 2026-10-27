"""Turn a spoken plan phrase into a concrete plan with Muse Spark Open.

Baseten (Muse Spark Open) is the main brain; Meta's Model API is the backup.
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime
from zoneinfo import ZoneInfo

from lengths import clean
from timing import record

TZ = ZoneInfo("America/New_York")
BASETEN = ("https://inference.baseten.co/v1/chat/completions",
           "meta-models/muse-spark-open-1026", "BASETEN_API_KEY")
META = ("https://api.meta.ai/v1/chat/completions", "muse-spark-1.3", "META_API_KEY")
# How hard the model thinks before answering. Both providers document
# low | medium | high | xhigh (xhigh = default): docs.baseten.co/inference/model-apis/reasoning
# and dev.meta.ai/docs/muse-spark-open/settings. "low" cut reasoning tokens ~4x in our tests.
EFFORT = json.loads((Path(__file__).resolve().parent.parent / "config" / "settings.json")
                    .read_text()).get("reasoning_effort")
_conns = threading.local()  # one keep-alive HTTPS connection per thread per host


def _post(url, body, headers, timeout=90):
    """POST over a reused HTTPS connection (saves the TLS handshake each call)."""
    u = urlsplit(url)
    pool = _conns.__dict__.setdefault("pool", {})
    for fresh in (False, True):
        conn = pool.get(u.netloc)
        if conn is None or fresh:
            if conn:
                conn.close()
            conn = pool[u.netloc] = http.client.HTTPSConnection(u.netloc, timeout=timeout)
        try:
            conn.request("POST", u.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except (http.client.HTTPException, ConnectionError, OSError):
            conn.close()
            pool.pop(u.netloc, None)
            if fresh:
                raise
            continue  # stale keep-alive socket: retry once on a new one
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {data[:200].decode(errors='replace')}")
        return json.loads(data)

SYSTEM = """You are a scheduling agent. Someone said a plan phrase out loud.
Turn it into a concrete plan. Reply with JSON only, no prose:
{"activity": short phrase like "catch up" or "grab lunch",
 "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD",
 "duration_min": integer}
Rules:
- Resolve relative days against today's date given below. A bare weekday
  ("Thursday") means the next upcoming one (today counts if it is that day).
  "next week" means next Monday through Sunday. "this week" or no time said
  means today through this Sunday.
- start_date and end_date are inclusive and never before today.
- duration_min defaults to 30 when not said."""


def _call(url, model, key_env, system, user, max_tokens):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        **({"reasoning_effort": EFFORT} if EFFORT else {}),
    }).encode()
    headers = {"Authorization": f"Bearer {os.environ[key_env]}",
               "Content-Type": "application/json", "Connection": "keep-alive"}
    t0 = time.perf_counter()
    try:
        data = _post(url, body, headers)
    except Exception as e:
        record(kind="model", label=system.split("\n")[0][:60], model=model, ok=False,
               seconds=round(time.perf_counter() - t0, 2), error=type(e).__name__)
        raise
    u = data.get("usage") or {}
    record(kind="model", label=system.split("\n")[0][:60], model=model, ok=True,
           seconds=round(time.perf_counter() - t0, 2), prompt_tokens=u.get("prompt_tokens"),
           completion_tokens=u.get("completion_tokens"),
           reasoning_tokens=(u.get("completion_tokens_details") or {}).get("reasoning_tokens"))
    choice = data["choices"][0]
    message = choice["message"]
    if not message.get("content"):
        raise ValueError(f"no answer text (finish_reason={choice.get('finish_reason')}; "
                         "the reasoning likely used up max_tokens)")
    content = message["content"].strip()
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    return json.loads(content), message.get("reasoning_content")


def ask_json(system: str, user: str, check=None, max_tokens=12000, tries=(2, 3)) -> dict:
    """Ask Muse Spark Open for a strict-JSON reply; fall back to Meta's API.

    `check(reply)` may raise to reject a malformed reply (it is retried).
    Returns {"reply", "reasoning", "model", "errors"}.
    """
    errors = []
    for url, model, key_env, tries in (BASETEN + (tries[0],), META + (tries[1],)):
        for attempt in range(tries):
            try:
                reply, reasoning = _call(url, model, key_env, system, user, max_tokens)
                if check:
                    check(reply)
                return {"reply": reply, "reasoning": reasoning, "model": model,
                        "errors": errors}
            except Exception as e:  # network, HTTP, bad JSON or bad shape: retry / fall back
                errors.append(f"{model} attempt {attempt + 1}: {type(e).__name__}: {e}")
                time.sleep(1)
    raise RuntimeError("Both brains failed: " + " | ".join(errors))


def parse_plan(text: str, now: datetime | None = None) -> dict:
    """Returns {"plan", "reasoning", "model", "errors"}."""
    now = now or datetime.now(TZ)
    user = (f"Today is {now:%A %Y-%m-%d}, {now:%H:%M} America/New_York.\n"
            f"Phrase: {text}")
    out = ask_json(SYSTEM, user)
    plan = out["reply"]
    plan["duration_min"] = clean(plan.get("duration_min") or 30)  # multiple of 5, rounded down
    return {"plan": plan, "reasoning": out["reasoning"], "model": out["model"],
            "errors": out["errors"]}


if __name__ == "__main__":
    print(json.dumps(parse_plan(" ".join(sys.argv[1:]))["plan"]))
