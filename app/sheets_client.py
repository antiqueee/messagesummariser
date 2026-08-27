"""
Google Sheets export client for AI-agent summaries.
Uses a Service Account for server-side authentication.

Setup:
1. Create a Google Cloud project, enable the Google Sheets API
2. Create a Service Account and download its credentials JSON file
3. Set GOOGLE_SHEETS_CREDENTIALS_FILE=/path/to/credentials.json in .env
4. Share each Google Sheet with the service account email (give Editor access)
5. Paste each sheet URL in the ЖК card in the web UI

One Service Account / one credentials file works for ALL sheets —
just share each sheet with the service account email separately.

Sheet column layout (must already exist in the spreadsheet):
A: Дата | B: No | C: Тревожные темы | D: Чат, где обсуждают |
E: Доп. информация | F: Риски/реакция | G: Основные (фоновые) темы
"""
import asyncio
import os
from datetime import datetime
from pathlib import Path

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


def is_configured() -> bool:
    """Return True when the Google Sheets integration is ready to use."""
    if not GSPREAD_AVAILABLE:
        return False
    creds_file = os.getenv('GOOGLE_SHEETS_CREDENTIALS_FILE', '').strip()
    return bool(creds_file) and Path(creds_file).exists()


def get_service_account_email() -> str | None:
    """Return the service account e-mail so the user knows what to share sheets with."""
    if not GSPREAD_AVAILABLE:
        return None
    creds_file = os.getenv('GOOGLE_SHEETS_CREDENTIALS_FILE', '').strip()
    if not creds_file or not Path(creds_file).exists():
        return None
    try:
        import json
        with open(creds_file) as f:
            data = json.load(f)
        return data.get('client_email')
    except Exception:
        return None


def _get_client() -> 'gspread.Client':
    if not GSPREAD_AVAILABLE:
        raise RuntimeError(
            "gspread не установлен. Выполните: pip install gspread google-auth"
        )
    creds_file = os.getenv('GOOGLE_SHEETS_CREDENTIALS_FILE', '').strip()
    if not creds_file:
        raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS_FILE не задан в .env")
    if not Path(creds_file).exists():
        raise RuntimeError(f"Файл credentials не найден: {creds_file}")
    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    return gspread.authorize(creds)


def extract_sheet_id(url_or_id: str) -> str:
    """Extract the spreadsheet ID from a Google Sheets URL, or return it unchanged."""
    url_or_id = url_or_id.strip()
    if '/spreadsheets/d/' in url_or_id:
        after = url_or_id.split('/spreadsheets/d/')[1]
        return after.split('/')[0]
    return url_or_id


async def export_to_sheet(
    sheet_id: str,
    date_str: str,
    rows: list[dict],
) -> dict:
    """
    Write structured rows for one day into a Google Sheet.

    Any existing rows where column A equals date_str are deleted first,
    then the new rows are appended so the operation is idempotent.

    Each dict in `rows` must have:
        chat, alarming_topics, additional_info, risks_reaction, background_topics

    Column layout:
        A: Дата  B: No  C: Тревожные темы  D: Чат, где обсуждают
        E: Доп. информация  F: Риски/реакция  G: Основные (фоновые) темы

    Returns: {'success': bool, 'message': str, 'rows_written': int}
    """

    def _sync_export():
        gc = _get_client()
        spreadsheet = gc.open_by_key(sheet_id)
        ws = spreadsheet.sheet1

        all_values = ws.get_all_values()

        # Collect 1-based row indices that already have this date in column A
        existing_row_indices = [
            i + 1
            for i, row in enumerate(all_values)
            if row and row[0] == date_str
        ]

        # Delete them from bottom to top so indices stay valid
        for row_idx in sorted(existing_row_indices, reverse=True):
            ws.delete_rows(row_idx)

        # Re-fetch after deletions to get current state of column B (No)
        remaining = ws.get_all_values()

        # Find the largest existing number in column B (skips header / non-numeric)
        last_no = 0
        for row in remaining:
            if len(row) >= 2:
                try:
                    val = int(str(row[1]).strip())
                    if val > last_no:
                        last_no = val
                except (ValueError, TypeError):
                    pass

        # Append new rows with No = last_no + 1, last_no + 2, ...
        new_sheet_rows = []
        for i, row in enumerate(rows):
            new_sheet_rows.append([
                date_str,
                last_no + i + 1,
                row.get('alarming_topics', ''),
                row.get('chat', ''),
                row.get('additional_info', ''),
                row.get('risks_reaction', ''),
                row.get('background_topics', ''),
            ])

        if new_sheet_rows:
            ws.append_rows(new_sheet_rows, value_input_option='RAW')

        deleted = len(existing_row_indices)
        written = len(new_sheet_rows)
        parts = []
        if deleted:
            parts.append(f"удалено {deleted} старых строк за {date_str}")
        parts.append(f"записано {written} строк (No {last_no + 1}–{last_no + written})")
        return {'success': True, 'message': ', '.join(parts), 'rows_written': written}

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _sync_export)
    except Exception as e:
        return {'success': False, 'message': str(e), 'rows_written': 0}


def _parse_sheet_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


async def read_weekly_rows(
    sheet_id: str,
    start_date: datetime,
    end_date: datetime,
) -> list[dict]:
    """
    Read rows from the linked daily-summary sheet for an inclusive date range.

    Expected layout:
        A: Дата  B: No  C: Тревожные темы  D: Чат, где обсуждают
        E: Доп. информация  F: Риски/реакция  G: Основные (фоновые) темы
    """

    start_day = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)

    def _sync_read():
        gc = _get_client()
        spreadsheet = gc.open_by_key(sheet_id)
        ws = spreadsheet.sheet1
        values = ws.get_all_values()
        rows = []

        for row_index, row in enumerate(values[1:], start=2):
            padded = row + [""] * (7 - len(row))
            row_date = _parse_sheet_date(padded[0])
            if not row_date or row_date < start_day or row_date > end_day:
                continue

            rows.append({
                "row_index": row_index,
                "date": padded[0].strip(),
                "no": padded[1].strip(),
                "alarming_topics": padded[2].strip(),
                "chat": padded[3].strip(),
                "additional_info": padded[4].strip(),
                "risks_reaction": padded[5].strip(),
                "background_topics": padded[6].strip(),
            })

        return rows

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_read)
