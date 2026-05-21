"""Google OAuth + Calendar API wrapper.

Single-user setup: client_id/client_secret are stored in the SQLite `config`
table (entered through the settings UI), the OAuth refresh-token is stored
in `oauth_tokens`. All API calls run through `calendar_service()` which
auto-refreshes the token on demand.

Helen creates and uses a dedicated calendar named "Helen". Each scheduled
task instance becomes a timed event there. Completion is signalled by the
event's `colorId` (graphite = done).
"""
from __future__ import annotations

import os
# Google adds openid/email/profile scopes server-side when the OAuth consent
# screen has them enabled. requests-oauthlib then raises because the granted
# scope set differs from what we requested. Telling oauthlib to relax the
# scope-equality check fixes that without affecting security.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import json
import logging
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from helen import db

log = logging.getLogger("helen.google")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
HELEN_CAL_TITLE = "Helen"
COMPLETED_COLOR_ID = "8"  # graphite — visually "done"


# ---------- OAuth flow ----------

def _client_config() -> Optional[dict]:
    cid = db.get_config("oauth_client_id")
    csec = db.get_config("oauth_client_secret")
    if not cid or not csec:
        return None
    return {
        "web": {
            "client_id": cid,
            "client_secret": csec,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_url()],
        }
    }


def redirect_url() -> str:
    return os.environ.get("HELEN_OAUTH_REDIRECT_URL", "https://adminhelen.l8tenever.com/oauth/callback")


def build_flow(state: Optional[str] = None) -> Flow:
    cfg = _client_config()
    if cfg is None:
        raise RuntimeError("OAuth client_id/client_secret nicht konfiguriert.")
    flow = Flow.from_client_config(cfg, scopes=SCOPES, state=state)
    flow.redirect_uri = redirect_url()
    return flow


def authorization_url(state: str) -> str:
    flow = build_flow(state=state)
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return url


def exchange_code(state: str, full_callback_url: str) -> None:
    flow = build_flow(state=state)
    flow.fetch_token(authorization_response=full_callback_url)
    creds = flow.credentials
    db.save_oauth_token(creds.to_json())
    log.info("OAuth token stored.")


# ---------- Credentials access ----------

def load_credentials() -> Optional[Credentials]:
    token_json = db.load_oauth_token()
    if not token_json:
        return None
    info = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            db.save_oauth_token(creds.to_json())
        except Exception:
            log.exception("Token refresh failed.")
            return None
    return creds


def is_connected() -> bool:
    return load_credentials() is not None


def disconnect() -> None:
    db.clear_oauth_token()
    db.set_config("helen_calendar_id", None)
    # Wipe the legacy Tasks list id so a re-auth doesn't try to reuse it.
    db.set_config("helen_tasklist_id", None)


# ---------- Calendar service ----------

def calendar_service():
    creds = load_credentials()
    if creds is None:
        raise RuntimeError("Nicht mit Google verbunden.")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def ensure_helen_calendar() -> str:
    """Find or create the 'Helen' calendar. Returns its id."""
    existing = db.get_config("helen_calendar_id")
    svc = calendar_service()
    if existing:
        try:
            svc.calendars().get(calendarId=existing).execute()
            return existing
        except HttpError as e:
            if e.resp.status != 404:
                raise
            db.set_config("helen_calendar_id", None)

    page_token = None
    while True:
        resp = svc.calendarList().list(pageToken=page_token).execute()
        for item in resp.get("items", []):
            if item.get("summary") == HELEN_CAL_TITLE:
                db.set_config("helen_calendar_id", item["id"])
                return item["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    tz = os.environ.get("HELEN_TZ", "Europe/Berlin")
    created = svc.calendars().insert(
        body={"summary": HELEN_CAL_TITLE, "timeZone": tz}
    ).execute()
    db.set_config("helen_calendar_id", created["id"])
    log.info("Created Helen calendar: %s", created["id"])
    return created["id"]


def create_event(
    title: str, start_iso: str, end_iso: str, tz: str, description: str = "",
) -> dict:
    svc = calendar_service()
    cal_id = ensure_helen_calendar()
    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": tz},
        "end": {"dateTime": end_iso, "timeZone": tz},
    }
    return svc.events().insert(calendarId=cal_id, body=body).execute()


def delete_event(event_id: str) -> None:
    svc = calendar_service()
    cal_id = ensure_helen_calendar()
    try:
        svc.events().delete(calendarId=cal_id, eventId=event_id).execute()
    except HttpError as e:
        if e.resp.status not in (404, 410):
            raise


def patch_event_completed(event_id: str, completed: bool) -> dict:
    """Mark/unmark an event done by toggling its colour (graphite = done)."""
    svc = calendar_service()
    cal_id = ensure_helen_calendar()
    body = {"colorId": COMPLETED_COLOR_ID if completed else None}
    return svc.events().patch(calendarId=cal_id, eventId=event_id, body=body).execute()


def update_event(event_id: str, summary: str, description: str, completed: bool) -> dict:
    """Patch summary, description, and completion colour on an existing event."""
    svc = calendar_service()
    cal_id = ensure_helen_calendar()
    body = {
        "summary": summary,
        "description": description,
        "colorId": COMPLETED_COLOR_ID if completed else None,
    }
    return svc.events().patch(calendarId=cal_id, eventId=event_id, body=body).execute()


def list_events(
    time_min_iso: Optional[str] = None, time_max_iso: Optional[str] = None,
) -> list[dict]:
    svc = calendar_service()
    cal_id = ensure_helen_calendar()
    items: list[dict] = []
    page_token = None
    while True:
        kwargs: dict = {
            "calendarId": cal_id,
            "maxResults": 2500,
            "singleEvents": True,
            "showDeleted": False,
            "pageToken": page_token,
        }
        if time_min_iso:
            kwargs["timeMin"] = time_min_iso
        if time_max_iso:
            kwargs["timeMax"] = time_max_iso
        resp = svc.events().list(**kwargs).execute()
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items
