"""Google Calendar client using the service account in GOOGLE_SA_JSON (the JSON
text) or GOOGLE_SA_FILE (a path to the key file)."""
import json
import os
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

PEOPLE = json.loads((Path(__file__).parent.parent / "config/people.json").read_text())


def calendar():
    if os.environ.get("GOOGLE_SA_JSON"):
        info = json.loads(os.environ["GOOGLE_SA_JSON"])
    else:  # path to the key file, e.g. GOOGLE_SA_FILE=service-account.json (repo root)
        path = Path(os.environ["GOOGLE_SA_FILE"]).expanduser()
        if not path.is_absolute():
            path = Path(__file__).parent.parent / path
        info = json.loads(path.read_text())
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)
