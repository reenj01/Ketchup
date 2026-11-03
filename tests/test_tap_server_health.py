"""Dot check-in on the tap server: POST /hello and /tap mark a Dot online (one line each, plus
"BOTH DOTS ONLINE"), silence past OFFLINE_S marks it offline, GET /health reports it.
Real HTTP on a free port; no model calls, no calendar writes."""
import contextlib
import io
import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import tap_server as ts  # noqa: E402

out = io.StringIO()
with contextlib.redirect_stdout(out):
    ts.check_in("pebble-mayo", now=100)
    ts.check_in("pebble-mayo", now=101)  # already online: no second line
    assert ts.health(now=101)["both_online"] is False
    ts.check_in("pebble-frankie", now=102)
    h = ts.health(now=103)
    assert h["both_online"] and h["dots"] == {"pebble-mayo": 2.0, "pebble-frankie": 1.0}, h
    assert ts.sweep_offline(now=110) == []
    assert ts.sweep_offline(now=101 + ts.OFFLINE_S + 0.5) == ["pebble-mayo"]
    ts.check_in("pebble-mayo", now=120)  # back
lines = out.getvalue().splitlines()
assert lines == ["DOT ONLINE  pebble-mayo (Mayo's Dot)", "DOT ONLINE  pebble-frankie (Frankie's Dot)",
                 "BOTH DOTS ONLINE: ready to film", "DOT OFFLINE pebble-mayo (no check-in for 15 s)",
                 "DOT ONLINE  pebble-mayo (Mayo's Dot)", "BOTH DOTS ONLINE: ready to film"], lines

ts.seen.clear(), ts.online.clear()
srv = ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}"


def post(path, body):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


with contextlib.redirect_stdout(io.StringIO()):
    assert post("/hello", {"device_id": "pebble-mayo"}) == (200, {"ok": True})
    assert post("/hello", {"device_id": "pebble-nobody"})[0] == 400
    with urllib.request.urlopen(url + "/health", timeout=5) as r:
        h = json.load(r)
assert h["ok"] and h["online"] == ["pebble-mayo"] and not h["both_online"], h
srv.shutdown()
print("test_tap_server_health: OK")
