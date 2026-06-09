"""Gmail API client backed by a user OAuth refresh token.

First run opens a browser for one-time consent; subsequent runs use the
saved token.json. A single OAuth grant covers both Gmail and Sheets —
the refresh token is shared between gmail_client and sheets_client.

Client exposes:
  client.search(query)
  client.get_message(mid)
  client.get_thread(tid)
  client.mark_read(mid)
  client.ensure_label(name) -> label_id
  client.add_label_to_thread(tid, label_id)
  client.send_reply(*, to, cc, subject, html_body, thread_id, in_reply_to)

Module-level helpers on normalized message dicts:
  header(msg, name), message_html_body(msg), iter_pdf_attachments(msg)
"""
from __future__ import annotations

import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.labels",
    # Full read+write on Sheets — read Master Sheet LT for routing,
    # write new rows to the chargeback tracker on successful send.
    "https://www.googleapis.com/auth/spreadsheets",
]


# ---------------------------------------------------------------------------
# Pure helpers on normalized message dicts
# ---------------------------------------------------------------------------

def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii") + b"==")


def header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def message_html_body(msg: dict) -> str:
    html, plain = "", ""

    def walk(part):
        nonlocal html, plain
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        if mime == "text/html" and data and not html:
            html = _decode(data).decode("utf-8", errors="replace")
        elif mime == "text/plain" and data and not plain:
            plain = _decode(data).decode("utf-8", errors="replace")
        for p in part.get("parts", []) or []:
            walk(p)

    walk(msg.get("payload", {}))
    return html or plain


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def build_oauth_creds(credentials_path: str, token_path: str) -> Credentials:
    """Load + refresh user OAuth credentials. On first run, opens the browser
    for consent and writes token_path."""
    creds = None
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json())
    return creds


# ---------------------------------------------------------------------------
# Gmail API client
# ---------------------------------------------------------------------------

class ApiClient:
    def __init__(self, creds: Credentials):
        self.creds = creds
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    # --- search / fetch ---
    def search(self, query: str, max_pages: int = 10) -> list[dict]:
        """Search messages, paginating through up to max_pages pages of 500.
        Default page size is 100 — hard-coding 500 (the API max) and walking
        page tokens is needed for label-scoped queries like the reminder pass,
        which can match >100 threads in active periods."""
        out: list[dict] = []
        page_token: str | None = None
        for _ in range(max_pages):
            kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self.svc.users().messages().list(**kwargs).execute()
            out.extend(resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return out

    def get_message(self, msg_id: str) -> dict:
        msg = self.svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
        self._hydrate_attachments(msg, msg_id)
        return msg

    def _hydrate_attachments(self, msg: dict, msg_id: str) -> None:
        def walk(part):
            body = part.get("body", {}) or {}
            if not body.get("data") and body.get("attachmentId"):
                att = self.svc.users().messages().attachments().get(
                    userId="me", messageId=msg_id, id=body["attachmentId"]
                ).execute()
                body["data"] = att.get("data", "")
            for p in part.get("parts", []) or []:
                walk(p)
        walk(msg.get("payload", {}))

    def get_thread(self, thread_id: str) -> dict:
        thread = self.svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        for m in thread.get("messages", []):
            self._hydrate_attachments(m, m["id"])
        return thread

    def mark_read(self, msg_id: str) -> None:
        self.svc.users().messages().modify(
            userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()

    # --- labels ---
    def ensure_label(self, name: str) -> str:
        labels = self.svc.users().labels().list(userId="me").execute().get("labels", [])
        for lbl in labels:
            if lbl["name"].lower() == name.lower():
                return lbl["id"]
        return self.svc.users().labels().create(
            userId="me",
            body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        ).execute()["id"]

    def add_label_to_thread(self, thread_id: str, label_id: str) -> None:
        self.svc.users().threads().modify(
            userId="me", id=thread_id, body={"addLabelIds": [label_id]}
        ).execute()

    # --- close: no-op for API client, kept for interface parity ---
    def close(self) -> None:
        pass

    # --- send ---
    def send_reply(self, *, to: str, cc: str = "", subject: str, html_body: str,
                   thread_id: str | None = None, in_reply_to: str | None = None) -> dict:
        mime = MIMEMultipart("alternative")
        mime["To"] = to
        if cc:
            mime["Cc"] = cc
        mime["Subject"] = subject
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = in_reply_to
        mime.attach(MIMEText(html_body, "html"))
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        body = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return self.svc.users().messages().send(userId="me", body=body).execute()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_client(cfg: dict, base_dir: Path | None = None) -> ApiClient:
    base_dir = base_dir or Path(".")
    auth = cfg.get("auth", {}) or {}
    creds = build_oauth_creds(
        str(base_dir / auth.get("credentials_path", "credentials.json")),
        str(base_dir / auth.get("token_path", "token.json")),
    )
    return ApiClient(creds)


# ---------------------------------------------------------------------------
# PDF attachment iterator (kept so the pipeline handles PDFs if they arrive,
# even though current chargeback mails are HTML-only)
# ---------------------------------------------------------------------------

def iter_pdf_attachments(msg: dict) -> Iterable[tuple[str, bytes]]:
    def walk(part):
        for p in part.get("parts", []) or []:
            yield from walk(p)
        filename = part.get("filename") or ""
        if not filename.lower().endswith(".pdf"):
            return
        body = part.get("body", {}) or {}
        data = body.get("data")
        if data:
            yield filename, _decode(data)

    yield from walk(msg.get("payload", {}))
