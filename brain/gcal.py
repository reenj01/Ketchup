"""Google Calendar client using the service account in GOOGLE_SA_JSON."""
import json
import os
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

PEOPLE = json.loads((Path(__file__).parent.parent / "config/people.json").read_text())


def calendar():
    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)
