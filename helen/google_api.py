"""Google OAuth + Tasks API wrapper.

Single-user setup: client_id/client_secret are stored in the SQLite `config`
table (entered through the settings UI), the OAuth refresh-token is stored
in `oauth_tokens`. All API calls run through `tasks_service()` which
auto-refreshes the token on demand.
"""
from __future__ import annotations

import os
# Google adds openid/email/profile scopes server-side when the OAuth consent
# screen has them enabled. requests-oauthlib then raises because the granted
# scope set differs from what we requested. Telling oauthlib to relax the
# scope-equality check fixes that without affecting security (we only ever
# *use* the tasks scope; the extras are ignored).
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

SCOPES = ["https://www.googleapis.com/auth/tasks"]
HELEN_LIST_TITLE = "Helen"


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
    db.set_config("helen_tasklist_id", None)


# ---------- Tasks service ----------

def tasks_service():
    creds = load_credentials()
    if creds is None:
        raise RuntimeError("Nicht mit Google verbunden.")
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def ensure_helen_tasklist() -> str:
    """Find or create the 'Helen' task list. Returns its id."""
    existing = db.get_config("helen_tasklist_id")
    svc = tasks_service()
    if existing:
        try:
            svc.tasklists().get(tasklist=existing).execute()
            return existing
        except HttpError as e:
            if e.resp.status != 404:
                raise
            db.set_config("helen_tasklist_id", None)

    page_token = None
    while True:
        resp = svc.tasklists().list(maxResults=100, pageToken=page_token).execute()
        for item in resp.get("items", []):
            if item.get("title") == HELEN_LIST_TITLE:
                db.set_config("helen_tasklist_id", item["id"])
                return item["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    created = svc.tasklists().insert(body={"title": HELEN_LIST_TITLE}).execute()
    db.set_config("helen_tasklist_id", created["id"])
    log.info("Created Helen task list: %s", created["id"])
    return created["id"]


def create_task(title: str, due_iso_z: str, notes: str = "") -> dict:
    svc = tasks_service()
    tasklist_id = ensure_helen_tasklist()
    body = {"title": title, "due": due_iso_z, "notes": notes}
    return svc.tasks().insert(tasklist=tasklist_id, body=body).execute()


def delete_task(google_task_id: str) -> None:
    svc = tasks_service()
    tasklist_id = ensure_helen_tasklist()
    try:
        svc.tasks().delete(tasklist=tasklist_id, task=google_task_id).execute()
    except HttpError as e:
        if e.resp.status != 404:
            raise


def patch_task_status(google_task_id: str, completed: bool) -> dict:
    svc = tasks_service()
    tasklist_id = ensure_helen_tasklist()
    body = {"status": "completed" if completed else "needsAction"}
    if not completed:
        body["completed"] = None
    return svc.tasks().patch(tasklist=tasklist_id, task=google_task_id, body=body).execute()


def list_tasks(show_completed: bool = True, show_hidden: bool = True) -> list[dict]:
    svc = tasks_service()
    tasklist_id = ensure_helen_tasklist()
    items: list[dict] = []
    page_token = None
    while True:
        resp = svc.tasks().list(
            tasklist=tasklist_id,
            showCompleted=show_completed,
            showHidden=show_hidden,
            maxResults=100,
            pageToken=page_token,
        ).execute()
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items
