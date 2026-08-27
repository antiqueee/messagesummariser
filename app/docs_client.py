"""
Google Docs archive client for saved report summaries.

Uses the same Service Account credentials as Google Sheets. Share the target
Google Doc with the service account e-mail and give Editor access.
"""
import os
import re
from datetime import datetime
from pathlib import Path

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/documents"]


def extract_doc_id(url_or_id: str) -> str:
    value = (url_or_id or "").strip()
    if "/document/d/" in value:
        after = value.split("/document/d/", 1)[1]
        return after.split("/", 1)[0]
    match = re.search(r"[?&]id=([^&#]+)", value)
    if match:
        return match.group(1)
    return value


def is_configured(doc_id: str | None = None) -> bool:
    creds_file = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "").strip()
    return bool(doc_id) and bool(creds_file) and Path(creds_file).exists()


def _get_access_token() -> str:
    creds_file = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "").strip()
    if not creds_file:
        raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS_FILE не задан в .env")
    if not Path(creds_file).exists():
        raise RuntimeError(f"Файл credentials не найден: {creds_file}")

    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    creds.refresh(GoogleAuthRequest())
    return creds.token


async def append_report_to_doc(
        *,
        document_id: str,
        report_type: str,
        complex_name: str,
        period_start: str,
        period_end: str,
        text: str,
) -> dict:
    doc_id = extract_doc_id(document_id)
    if not doc_id:
        raise RuntimeError("Google Doc не указан")
    if not text.strip():
        raise RuntimeError("Сводка пустая")

    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        meta_resp = await client.get(
            f"https://docs.googleapis.com/v1/documents/{doc_id}",
            params={"fields": "body/content(endIndex)"},
            headers=headers,
        )
        if meta_resp.status_code >= 400:
            raise RuntimeError(f"Google Docs API get failed: {meta_resp.text}")

        content = meta_resp.json().get("body", {}).get("content", [])
        end_index = max((item.get("endIndex", 1) for item in content), default=1)
        insert_index = max(1, end_index - 1)

        label = "Дневная сводка" if report_type == "daily" else "Сводка"
        created_at = datetime.now().strftime("%d.%m.%Y %H:%M")
        archive_text = (
            f"\n\n{label}: {complex_name}\n"
            f"Период: {period_start} - {period_end}\n"
            f"Записано: {created_at}\n\n"
            f"{text.strip()}\n"
        )

        update_resp = await client.post(
            f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
            headers=headers,
            json={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": insert_index},
                            "text": archive_text,
                        }
                    }
                ]
            },
        )
        if update_resp.status_code >= 400:
            raise RuntimeError(f"Google Docs API batchUpdate failed: {update_resp.text}")

    return {"success": True, "document_id": doc_id}
