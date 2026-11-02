"""Calendar client. Google Calendar via the service account in GOOGLE_SA_JSON
(the JSON text) or GOOGLE_SA_FILE (a path to the key file), or the seeded mock
calendars (mock_calendar.py) when KETCHUP_CALENDAR=mock, or when it is unset
and no Google key is configured."""
import json
import os
from pathlib import Path

PEOPLE = json.loads((Path(__file__).parent.parent / "config/people.json").read_text())


def use_mock():
    mode = os.environ.get("KETCHUP_CALENDAR", "").lower()
    if mode in ("mock", "google"):
        return mode == "mock"
    return not (os.environ.get("GOOGLE_SA_JSON") or os.environ.get("GOOGLE_SA_FILE"))


def calendar():
    if use_mock():
        from mock_calendar import MockCalendar
        return MockCalendar()
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
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
