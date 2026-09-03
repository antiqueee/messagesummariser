import os
import json
import re
import traceback
import asyncio
import uuid
from datetime import datetime
from typing import Optional, Callable, Awaitable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from dotenv import load_dotenv

from . import database as db


def extract_building_number(chat_name: str) -> tuple[int, str]:
    """Extract building number for sorting. Returns (sort_key, name).
    Building chats (корпус X, секция X) get priority, general chats go last."""
    if not chat_name:
        return (1, 0, '')  # Empty names go last
    name_lower = chat_name.lower()

    # Look for patterns like "корпус 17", "корп. 5", "к.12", "секция 3"
    patterns = [
        r'корпус\s*(\d+)',
        r'корп\.?\s*(\d+)',
        r'к\.?\s*(\d+)',
        r'секция\s*(\d+)',
        r'секц\.?\s*(\d+)',
        r'^(\d+)\s*корп',
    ]

    for pattern in patterns:
        match = re.search(pattern, name_lower)
        if match:
            return (0, int(match.group(1)), chat_name)  # Buildings first, sorted by number

    # General/common chats go last
    return (1, 0, chat_name)  # Non-building chats last, sorted alphabetically


def sort_chats_by_building(chats: list[dict]) -> list[dict]:
    """Sort chats: buildings first (by number), then general chats"""
    return sorted(chats, key=lambda c: extract_building_number(c.get('custom_name') or c.get('original_title') or ''))


def format_chat_log_context(chat: dict, start_date: Optional[datetime] = None,
                            end_date: Optional[datetime] = None,
                            topic_ids: Optional[list[int]] = None) -> str:
    """Build a compact chat context string for diagnostic logs."""
    return (
        f"chat_db_id={chat.get('id')}, "
        f"source={chat.get('source') or 'telegram'}, "
        f"chat_name={chat.get('custom_name') or chat.get('original_title')!r}, "
        f"original_title={chat.get('original_title')!r}, "
        f"telegram_id={chat.get('telegram_id')!r} ({type(chat.get('telegram_id')).__name__}), "
        f"account_id={chat.get('account_id')!r}, "
        f"max_account_id={chat.get('max_account_id')!r}, "
        f"source_account_id={chat.get('source_account_id')!r}, "
        f"source_chat_id={chat.get('source_chat_id')!r}, "
        f"selected_topics_raw={chat.get('selected_topics')!r}, "
        f"topic_ids={topic_ids!r}, "
        f"start_date={start_date!r}, "
        f"end_date={end_date!r}"
    )


def extract_vk_access_token(raw_value: str) -> str:
    """Accept either a raw VK token or the full oauth redirect URL."""
    value = (raw_value or "").strip()
    if not value:
        return ""

    match = re.search(r"(?:[#?&]|^)access_token=([^&#\s]+)", value)
    if match:
        return match.group(1).strip()

    return value


def parse_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_report_chat_name(chat: dict) -> str:
    """Build a stable chat label that the AI must preserve in the final report."""
    base_name = chat.get('custom_name') or chat.get('original_title') or 'Unknown chat'
    source = normalize_source_name(chat.get('source'))
    if source == 'vk':
        return f"VK | {base_name}"
    if source == 'max':
        return f"MAX | {base_name}"
    return base_name

from .telegram_client import init_telegram_manager, get_telegram_manager
from .max_client import init_max_manager, get_max_manager
from .proxy_manager import get_proxy_manager
from .source_router import fetch_chat_messages, get_source_label, normalize_source_name, SourceMessageFetchError
from .summarizer import (
    init_summarizer,
    get_summarizer,
    get_default_report_rules,
    get_default_weekly_report_rules,
    get_default_negativists_rules,
)
from .vk_client import init_vk_manager, get_vk_manager
from .bot import start_bot, stop_bot
from .models import (
    AccountCreateRequest, AccountVerifyRequest,
    ComplexCreateRequest, ChatUpdateRequest, GenerateReportRequest,
    GenerateWeeklyReportRequest,
    ComplexMaxTargetRequest, SaveReportRequest, SendReportToMaxRequest,
    AnalyzeNegativistsRequest, MaxAccountCreateRequest, MaxChatAddRequest,
    VkAccountCreateRequest, VkTokenUpdateRequest
)

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await db.init_db()

    api_id = os.getenv('TELEGRAM_API_ID')
    api_hash = os.getenv('TELEGRAM_API_HASH')
    openrouter_key = os.getenv('OPENROUTER_API_KEY')
    ai_model = os.getenv('AI_MODEL')  # Optional: override default model
    telegram_use_proxy = parse_bool_env('TELEGRAM_USE_PROXY', True)

    if api_id and api_hash:
        init_telegram_manager(int(api_id), api_hash, use_proxy=telegram_use_proxy)
        if telegram_use_proxy:
            try:
                pm = get_proxy_manager()
                await pm.start_background_monitor()
            except Exception as e:
                print(f"[ProxyManager] WARNING: background monitor not started: {e}", flush=True)
        else:
            print("[TelegramClient] Proxy disabled by TELEGRAM_USE_PROXY=false", flush=True)

    # Initialize Max messenger manager
    mm = init_max_manager()
    print("[Max] Max messenger manager initialized")

    vk = init_vk_manager()
    if vk:
        print("[VK] VK manager initialized", flush=True)

    # Auto-start authorized Max accounts (reconnect with cached sessions)
    try:
        max_accounts = await db.get_max_accounts()
        for acc in max_accounts:
            if acc['is_authorized']:
                try:
                    result = await mm.start_auth(acc['id'], acc['phone'])
                    status = result.get('status', 'unknown')
                    print(f"[Max] Auto-start account {acc['id']} ({acc['name']}): {status}", flush=True)
                except Exception as e:
                    print(f"[Max] Failed to auto-start account {acc['id']}: {e}", flush=True)
    except Exception as e:
        print(f"[Max] Error auto-starting accounts: {e}", flush=True)

    if openrouter_key:
        try:
            init_summarizer(openrouter_key, ai_model)
            print(f"Summarizer initialized with model: {ai_model or 'google/gemini-2.5-flash-preview'}")
        except Exception as e:
            print(f"WARNING: Summarizer init skipped ({e}). Summarization will be available later.")

    # Start Telegram bot
    bot_token = os.getenv('BOT_TOKEN')
    bot_admin_id = os.getenv('BOT_ADMIN_ID')

    # Parse demo mode settings
    demo_user_ids_str = os.getenv('DEMO_USER_IDS', '')
    demo_user_ids = set()
    if demo_user_ids_str.strip():
        for uid in demo_user_ids_str.split(','):
            uid = uid.strip()
            if uid.isdigit():
                demo_user_ids.add(int(uid))

    demo_complex_id_str = os.getenv('DEMO_COMPLEX_ID', '')
    demo_complex_id = int(demo_complex_id_str) if demo_complex_id_str.strip().isdigit() else None

    print(f"[Bot] Token: {'set' if bot_token else 'NOT SET'}, Admin ID: {bot_admin_id}")
    if demo_user_ids:
        print(f"[Bot] Demo mode: {len(demo_user_ids)} users, complex_id={demo_complex_id}")

    if bot_token and bot_admin_id:
        try:
            await start_bot(bot_token, int(bot_admin_id), demo_user_ids, demo_complex_id)
            print(f"[Bot] Telegram bot started, admin_id={bot_admin_id}")
        except Exception as e:
            import traceback
            print(f"[Bot] WARNING: Bot start failed: {e}")
            traceback.print_exc()
    else:
        print("[Bot] Bot not started - BOT_TOKEN or BOT_ADMIN_ID not set in .env")

    yield

    # Shutdown
    try:
        await stop_bot()
    except:
        pass
    try:
        pm = get_proxy_manager()
        await pm.stop_background_monitor()
    except Exception:
        pass
    try:
        tm = get_telegram_manager()
        await tm.close_all()
    except RuntimeError:
        pass
    try:
        mm = get_max_manager()
        await mm.close_all()
    except RuntimeError:
        pass


app = FastAPI(
    title="Telegram Chat Summarizer",
    description="Приложение для мониторинга и суммаризации чатов Telegram",
    version="1.0.0",
    lifespan=lifespan
)

# Static files
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

# Templates
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

REPORT_PROGRESS_JOBS: dict[str, dict] = {}
REPORT_PROGRESS_TTL_SECONDS = 3600


def _cleanup_report_progress_jobs() -> None:
    now = datetime.now().timestamp()
    stale_ids = [
        job_id for job_id, job in REPORT_PROGRESS_JOBS.items()
        if now - job.get("updated_ts", now) > REPORT_PROGRESS_TTL_SECONDS
    ]
    for job_id in stale_ids:
        REPORT_PROGRESS_JOBS.pop(job_id, None)


def _public_report_progress_job(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in {"updated_ts", "task"}}


async def _set_report_progress(job_id: str, **updates) -> None:
    job = REPORT_PROGRESS_JOBS.get(job_id)
    if not job:
        return
    job.update(updates)
    job["updated_at"] = datetime.now().isoformat()
    job["updated_ts"] = datetime.now().timestamp()



# ============== HTML Pages ==============

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    from fastapi.responses import HTMLResponse as HR
    response = templates.TemplateResponse("index.html", {"request": request})
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


# ============== Account Endpoints ==============

@app.get("/api/accounts")
async def get_accounts():
    """Get all Telegram accounts"""
    accounts = await db.get_accounts()
    return {"accounts": accounts}


@app.post("/api/accounts")
async def create_account(data: AccountCreateRequest):
    """Create a new Telegram account"""
    account_id = await db.create_account(data.phone, data.name)
    return {"id": account_id, "message": "Account created"}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int):
    """Delete a Telegram account"""
    try:
        tm = get_telegram_manager()
        await tm.disconnect_account(account_id)
    except RuntimeError:
        pass
    await db.delete_account(account_id)
    return {"message": "Account deleted"}


@app.post("/api/accounts/{account_id}/auth/start")
async def start_auth(account_id: int):
    """Start Telegram authentication"""
    account = await db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        tm = get_telegram_manager()
        result = await tm.start_auth(account_id, account['phone'])
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/accounts/{account_id}/auth/verify")
async def verify_auth(account_id: int, data: AccountVerifyRequest):
    """Complete Telegram authentication with code"""
    try:
        tm = get_telegram_manager()
        result = await tm.complete_auth(account_id, data.code, data.password)

        if result['status'] == 'success':
            await db.update_account_authorized(account_id, True)

        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/accounts/{account_id}/sync")
async def sync_account_chats(account_id: int):
    """Sync chats from Telegram account"""
    account = await db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        tm = get_telegram_manager()
        dialogs = await tm.get_dialogs(account_id)

        synced = 0
        for dialog in dialogs:
            await db.upsert_chat(
                telegram_id=dialog['telegram_id'],
                account_id=account_id,
                original_title=dialog['title']
            )
            synced += 1

        return {"message": f"Synced {synced} chats", "count": synced}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/accounts/{account_id}/service-messages")
async def get_service_messages(account_id: int, limit: int = 20):
    """Get service messages from Telegram (login codes, security alerts, etc.)"""
    account = await db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    if not account['is_authorized']:
        raise HTTPException(status_code=400, detail="Account not authorized")

    try:
        tm = get_telegram_manager()
        client = await tm.get_client(account_id)

        if not client:
            raise HTTPException(status_code=400, detail="Session expired or invalid")

        # 777000 is Telegram's official user_id for service notifications
        messages = await client.get_messages(777000, limit=limit)

        result = []
        for msg in messages:
            if msg.text:
                result.append({
                    'id': msg.id,
                    'date': msg.date.isoformat(),
                    'text': msg.text
                })

        return {"messages": result, "account_phone": account['phone']}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============== Complex (ЖК) Endpoints ==============

@app.get("/api/complexes")
async def get_complexes():
    """Get all residential complexes"""
    complexes = await db.get_complexes()
    return {"complexes": complexes}


@app.post("/api/complexes")
async def create_complex(data: ComplexCreateRequest):
    """Create a new residential complex"""
    complex_id = await db.create_complex(data.name)
    return {"id": complex_id, "message": "Complex created"}


@app.put("/api/complexes/{complex_id}")
async def update_complex(complex_id: int, data: ComplexCreateRequest):
    """Update a residential complex"""
    await db.update_complex(complex_id, data.name)
    return {"message": "Complex updated"}


@app.delete("/api/complexes/{complex_id}")
async def delete_complex(complex_id: int):
    """Delete a residential complex"""
    await db.delete_complex(complex_id)
    return {"message": "Complex deleted"}


@app.put("/api/complexes/{complex_id}/sheet")
async def set_complex_sheet(complex_id: int, request: Request):
    """Save or clear the Google Sheet URL/ID linked to a complex"""
    from .sheets_client import extract_sheet_id
    data = await request.json()
    raw = (data.get("google_sheet_url") or "").strip()
    sheet_id = extract_sheet_id(raw) if raw else None
    await db.set_complex_sheet(complex_id, sheet_id)
    return {"message": "Google Sheet сохранена", "sheet_id": sheet_id}


@app.put("/api/complexes/{complex_id}/max-target")
async def set_complex_max_target(complex_id: int, data: ComplexMaxTargetRequest):
    """Save or clear the Max chat used for sending reports for a complex."""
    complex_row = await db.get_complex(complex_id)
    if not complex_row:
        raise HTTPException(status_code=404, detail="ЖК не найден")

    max_account_id = data.max_account_id
    max_chat_id = data.max_chat_id

    if max_account_id is None and max_chat_id is None:
        await db.set_complex_max_target(complex_id, None, None)
        return {"message": "MAX-чат для отправки очищен"}

    if not max_account_id or not max_chat_id:
        raise HTTPException(status_code=400, detail="Укажите Max аккаунт и chat_id")

    account = await db.get_max_account(max_account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max аккаунт не найден")

    await db.set_complex_max_target(
        complex_id=complex_id,
        max_account_id=max_account_id,
        max_chat_id=max_chat_id,
        chat_title=(data.chat_title or "").strip() or None,
    )
    return {"message": "MAX-чат для отправки сохранен"}


@app.get("/api/sheets/status")
async def sheets_status():
    """Return Google Sheets configuration status"""
    from .sheets_client import is_configured, get_service_account_email
    configured = is_configured()
    email = get_service_account_email() if configured else None
    return {"configured": configured, "service_account_email": email}


REPORT_ARCHIVE_DOC_KEY = "report_archive_google_doc_id"


@app.get("/api/report-archive-doc")
async def get_report_archive_doc():
    """Return the Google Doc archive link/ID used for saved reports."""
    from .docs_client import extract_doc_id
    raw_doc_id = await db.get_setting(REPORT_ARCHIVE_DOC_KEY)
    doc_id = extract_doc_id(raw_doc_id or "") if raw_doc_id else ""
    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit" if doc_id else ""
    return {"google_doc_id": doc_id, "google_doc_url": doc_url}


@app.put("/api/report-archive-doc")
async def set_report_archive_doc(request: Request):
    """Save or clear the Google Doc archive link/ID."""
    from .docs_client import extract_doc_id
    data = await request.json()
    raw = (data.get("google_doc_url") or data.get("google_doc_id") or "").strip()
    doc_id = extract_doc_id(raw) if raw else ""
    if doc_id:
        await db.set_setting(REPORT_ARCHIVE_DOC_KEY, doc_id)
        return {
            "message": "Google Doc для базы сводок сохранен",
            "google_doc_id": doc_id,
            "google_doc_url": f"https://docs.google.com/document/d/{doc_id}/edit",
        }
    await db.set_setting(REPORT_ARCHIVE_DOC_KEY, "")
    return {"message": "Google Doc для базы сводок очищен", "google_doc_id": "", "google_doc_url": ""}


async def _save_report_payload(data: SaveReportRequest, archive_to_google_doc: bool | None = None) -> dict:
    if not data.complex_name.strip():
        raise HTTPException(status_code=400, detail="Название ЖК не указано")
    if not (data.edited_text or data.original_text).strip():
        raise HTTPException(status_code=400, detail="Сводка пустая")

    google_doc_status = "skipped"
    google_doc_error = None
    report_id = await db.save_report_history(
        report_type=data.report_type,
        complex_id=data.complex_id,
        complex_name=data.complex_name.strip(),
        period_start=data.period_start,
        period_end=data.period_end,
        original_text=data.original_text,
        edited_text=data.edited_text,
        google_doc_status=google_doc_status,
        google_doc_error=google_doc_error,
    )

    should_archive = data.archive_to_google_doc if archive_to_google_doc is None else archive_to_google_doc
    doc_id = await db.get_setting(REPORT_ARCHIVE_DOC_KEY)
    if should_archive and doc_id:
        try:
            from .docs_client import append_report_to_doc
            await append_report_to_doc(
                document_id=doc_id,
                report_type=data.report_type,
                complex_name=data.complex_name.strip(),
                period_start=data.period_start,
                period_end=data.period_end,
                text=data.edited_text or data.original_text,
            )
            google_doc_status = "success"
            await db.update_report_history_doc_status(report_id, google_doc_status, None)
        except Exception as e:
            google_doc_status = "error"
            google_doc_error = str(e)
            await db.update_report_history_doc_status(report_id, google_doc_status, google_doc_error)

    return {
        "report_history_id": report_id,
        "google_doc_status": google_doc_status,
        "google_doc_error": google_doc_error,
    }


def _format_daily_max_message(data: SendReportToMaxRequest) -> str:
    text = (data.edited_text or data.original_text or "").strip()
    if data.report_type != "daily":
        return text
    if text.lower().startswith("сводка за "):
        return text

    try:
        period_start = datetime.fromisoformat(str(data.period_start).replace("Z", "+00:00"))
        date_str = period_start.strftime("%d.%m")
    except Exception:
        date_str = str(data.period_start)[:10]

    return f"сводка за {date_str} по {data.complex_name}:\n\n{text}"


@app.post("/api/reports/save")
async def save_report(data: SaveReportRequest):
    """Save an edited report locally and optionally append it to the Google Doc archive."""
    result = await _save_report_payload(data)
    return {"message": "Сводка сохранена", **result}


@app.post("/api/reports/send-to-max")
async def send_report_to_max(data: SendReportToMaxRequest):
    """Save an edited report locally, then send it to Max."""
    if not data.complex_id:
        raise HTTPException(status_code=400, detail="complex_id не указан")

    complex_row = await db.get_complex(data.complex_id)
    if not complex_row:
        raise HTTPException(status_code=404, detail="ЖК не найден")

    max_account_id = complex_row.get("max_target_account_id")
    max_chat_id = complex_row.get("max_target_chat_id")
    if not max_account_id or not max_chat_id:
        raise HTTPException(
            status_code=400,
            detail="Для этого ЖК не задан MAX-чат отправки. Укажите Max аккаунт и chat_id в карточке ЖК."
        )

    save_result = await _save_report_payload(data, archive_to_google_doc=False)
    report_history_id = save_result["report_history_id"]

    account = await db.get_max_account(int(max_account_id))
    if not account:
        raise HTTPException(status_code=404, detail="Max аккаунт отправки не найден")

    try:
        mm = get_max_manager()
        connected = await mm.ensure_connected(int(max_account_id), account["phone"])
        if not connected:
            raise RuntimeError(
                "Max аккаунт временно не подключился. Подождите несколько секунд и повторите отправку; "
                "переавторизация нужна только если ошибка повторяется постоянно."
            )

        text = _format_daily_max_message(data)
        message_ids = await mm.send_text(
            account_id=int(max_account_id),
            chat_id=int(max_chat_id),
            text=text,
            notify=True,
        )
        await db.log_max_delivery(
            report_history_id=report_history_id,
            complex_id=data.complex_id,
            max_account_id=int(max_account_id),
            max_chat_id=int(max_chat_id),
            status="success",
            sent_message_ids=json.dumps(message_ids, ensure_ascii=False),
        )
        return {
            "message": "Сводка сохранена локально и отправлена в MAX",
            "sent_message_ids": message_ids,
            **save_result,
        }
    except Exception as e:
        await db.log_max_delivery(
            report_history_id=report_history_id,
            complex_id=data.complex_id,
            max_account_id=int(max_account_id),
            max_chat_id=int(max_chat_id),
            status="error",
            error_message=str(e),
        )
        raise HTTPException(status_code=500, detail=f"Не удалось отправить в MAX: {e}")


@app.post("/api/reports/export-to-sheets")
async def export_report_to_sheets(request: Request):
    """
    Export generated report summaries to Google Sheets.

    Uses AI to parse the free-form summary into structured rows matching the
    sheet column layout:
      A: Дата | B: No | C: Тревожные темы | D: Чат, где обсуждают |
      E: Доп. информация | F: Риски/реакция | G: Основные (фоновые) темы

    Body: {
      "date_str": "14.04.2026",
      "complexes": [
        {"complex_id": 1, "complex_name": "ЖК Солнечный", "summary": "..."}
      ]
    }
    """
    from .sheets_client import is_configured, export_to_sheet

    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail="Google Sheets не настроен. Добавьте GOOGLE_SHEETS_CREDENTIALS_FILE в .env файл."
        )

    try:
        summarizer = get_summarizer()
    except RuntimeError:
        raise HTTPException(
            status_code=400,
            detail="AI-суммаризатор не настроен. Добавьте OPENROUTER_API_KEY в .env файл."
        )

    data = await request.json()
    date_str = data.get("date_str", "").strip()
    complexes_payload = data.get("complexes", [])

    if not date_str:
        raise HTTPException(status_code=400, detail="date_str не указан")
    if not complexes_payload:
        raise HTTPException(status_code=400, detail="complexes пустой")

    results = []
    for item in complexes_payload:
        complex_id = item.get("complex_id")
        summary = item.get("summary", "").strip()
        complex_name = item.get("complex_name", str(complex_id))

        complex_row = await db.get_complex(complex_id)
        if not complex_row:
            results.append({
                "complex_id": complex_id,
                "complex_name": complex_name,
                "success": False,
                "message": "ЖК не найден в базе",
            })
            continue

        sheet_id = complex_row.get("google_sheet_id")
        complex_name = complex_row.get("name", complex_name)

        if not sheet_id:
            results.append({
                "complex_id": complex_id,
                "complex_name": complex_name,
                "success": False,
                "message": "Google Таблица не привязана к этому ЖК. Добавьте ссылку на таблицу в настройках ЖК.",
            })
            continue

        if not summary:
            results.append({
                "complex_id": complex_id,
                "complex_name": complex_name,
                "success": False,
                "message": "Сводка пустая — нечего экспортировать",
            })
            continue

        # Ask AI to parse the free-form summary into structured rows
        print(f"[Sheets] Structuring summary for {complex_name}...", flush=True)
        structured_rows = await summarizer.extract_for_sheets(
            complex_name=complex_name,
            summary_text=summary,
            date_str=date_str,
        )

        # Write rows to the Google Sheet
        print(f"[Sheets] Writing {len(structured_rows)} rows to sheet for {complex_name}...", flush=True)
        result = await export_to_sheet(sheet_id, date_str, structured_rows)
        results.append({
            "complex_id": complex_id,
            "complex_name": complex_name,
            **result,
        })

    return {"results": results}


@app.post("/api/weekly-reports/generate")
async def generate_weekly_report(data: GenerateWeeklyReportRequest):
    """Generate weekly risk summaries from linked Google Sheets rows."""
    from .sheets_client import is_configured, read_weekly_rows

    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail="Google Sheets не настроен. Добавьте GOOGLE_SHEETS_CREDENTIALS_FILE в .env файл."
        )

    try:
        summarizer = get_summarizer()
    except RuntimeError:
        raise HTTPException(
            status_code=400,
            detail="AI-суммаризатор не настроен. Добавьте OPENROUTER_API_KEY в .env файл."
        )

    try:
        start_date = data.get_start_date()
        end_date = data.get_end_date()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Неверный формат даты: {e}")

    if end_date < start_date:
        raise HTTPException(status_code=400, detail="Конец периода раньше начала периода")
    if not data.complex_ids:
        raise HTTPException(status_code=400, detail="Выберите хотя бы один ЖК")

    model = {"key": "gemini_pro", "label": "Gemini 3 Pro", "id": "google/gemini-3.1-pro-preview"}
    rules = await db.get_setting(WEEKLY_REPORT_RULES_KEY)
    if rules is None:
        rules = get_default_weekly_report_rules()

    summarizer.reset_usage()
    results = []

    for complex_id in data.complex_ids:
        complex_row = await db.get_complex(complex_id)
        if not complex_row:
            results.append({
                "complex_id": complex_id,
                "complex_name": str(complex_id),
                "success": False,
                "message": "ЖК не найден в базе",
                "rows_count": 0,
                "summaries": [],
            })
            continue

        complex_name = complex_row.get("name") or str(complex_id)
        sheet_id = complex_row.get("google_sheet_id")
        if not sheet_id:
            results.append({
                "complex_id": complex_id,
                "complex_name": complex_name,
                "success": False,
                "message": "Google Таблица не привязана к этому ЖК",
                "rows_count": 0,
                "summaries": [],
            })
            continue

        try:
            weekly_rows = await read_weekly_rows(sheet_id, start_date, end_date)
        except Exception as e:
            results.append({
                "complex_id": complex_id,
                "complex_name": complex_name,
                "success": False,
                "message": f"Не удалось прочитать Google Таблицу: {e}",
                "rows_count": 0,
                "summaries": [],
            })
            continue

        if not weekly_rows:
            no_data_summary = (
                f"{complex_name.upper()} – 1/10\n\n"
                "Обоснование: за выбранный период в таблице нет значимых строк для анализа.\n\n"
                "По данным Google Таблицы за неделю не найдено тревожных тем, рисков или реакций жителей."
            )
            results.append({
                "complex_id": complex_id,
                "complex_name": complex_name,
                "success": True,
                "message": "За период нет строк в таблице",
                "rows_count": 0,
                "summaries": [{**model, "summary": no_data_summary, "success": True}],
            })
            continue

        summaries = []
        try:
            print(
                f"[Weekly] Summarizing {complex_name} with {model['id']}: {len(weekly_rows)} sheet rows",
                flush=True,
            )
            summary = await summarizer.summarize_weekly_complex(
                complex_name=complex_name,
                weekly_rows=weekly_rows,
                start_date=start_date,
                end_date=end_date,
                model=model["id"],
                rules=rules,
            )
            summaries.append({**model, "summary": summary, "success": True})
        except Exception as e:
            print(f"[Weekly] Error for {complex_name} / {model['id']}: {e}", flush=True)
            summaries.append({**model, "summary": f"Ошибка генерации: {e}", "success": False})

        results.append({
            "complex_id": complex_id,
            "complex_name": complex_name,
            "success": True,
            "message": f"Прочитано строк: {len(weekly_rows)}",
            "rows_count": len(weekly_rows),
            "summaries": summaries,
        })

    usage = summarizer.get_usage_summary()
    print(
        "[Weekly] AI usage total: "
        f"calls={usage['calls']}, "
        f"input={usage['prompt_tokens']}, "
        f"output={usage['completion_tokens']}, "
        f"actual_cost=${usage['actual_cost_usd']:.4f}",
        flush=True,
    )

    return {
        "generated_at": datetime.now().isoformat(),
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "models": [model],
        "results": results,
        "usage": usage,
    }


# ============== Chat Endpoints ==============

@app.get("/api/chats")
async def get_chats(account_id: Optional[int] = None, complex_id: Optional[int] = None,
                    account_key: Optional[str] = None,
                    monitored_only: bool = False):
    """Get chats with optional filters"""
    chats = await db.get_chats(account_id, complex_id, account_key, monitored_only)
    return {"chats": chats}


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: int):
    """Get a specific chat"""
    chat = await db.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@app.put("/api/chats/{chat_id}")
async def update_chat(chat_id: int, data: ChatUpdateRequest):
    """Update chat settings"""
    await db.update_chat(
        chat_id,
        custom_name=data.custom_name,
        complex_id=data.complex_id,
        is_monitored=data.is_monitored,
        content_filter=data.content_filter,
        selected_topics=data.selected_topics
    )
    return {"message": "Chat updated"}


@app.get("/api/chats/{chat_id}/topics")
async def get_chat_topics(chat_id: int):
    """Get forum topics for a chat (if it's a forum)"""
    chat = await db.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    try:
        tm = get_telegram_manager()
        topics = await tm.get_forum_topics(chat['account_id'], chat['telegram_id'])
        is_forum = len(topics) > 0
        return {"is_forum": is_forum, "topics": topics}
    except RuntimeError:
        return {"is_forum": False, "topics": []}


# ============== Report Generation ==============

async def _generate_report_payload(
        data: GenerateReportRequest,
        progress: Optional[Callable[[dict], Awaitable[None]]] = None,
):
    """Generate a report for selected complexes — AI summary if available, raw messages otherwise"""

    async def emit(**updates):
        if progress:
            await progress(updates)

    await emit(
        status="running",
        stage="prepare",
        percent=2,
        title="Подготовка генерации",
        detail="Проверяю период, выбранные ЖК и доступность AI.",
    )

    # Parse dates
    try:
        start_date = data.get_start_date()
        end_date = data.get_end_date()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Неверный формат даты: {e}")

    # Check if AI summarizer is available
    summarizer = None
    try:
        summarizer = get_summarizer()
    except RuntimeError:
        pass

    if summarizer:
        summarizer.reset_usage()

    # Get all monitored chats grouped by complex
    chats_by_complex = await db.get_monitored_chats_by_complex()

    # Filter by requested complexes
    if data.complex_ids:
        chats_by_complex = {
            k: v for k, v in chats_by_complex.items()
            if k in data.complex_ids
        }

    if not chats_by_complex:
        raise HTTPException(status_code=400, detail="No monitored chats found")

    total_chats = sum(len(chats) for chats in chats_by_complex.values())
    total_complexes = len(chats_by_complex)
    total_work_units = max(1, total_chats + (total_complexes if summarizer else 0))
    completed_work_units = 0
    await emit(
        status="running",
        stage="prepare",
        percent=5,
        title="Нашел чаты для обработки",
        detail=f"Выбрано ЖК: {total_complexes}, чатов: {total_chats}.",
        total_complexes=total_complexes,
        total_chats=total_chats,
        current_complex_index=0,
        completed_chats=0,
    )

    report = {
        'generated_at': datetime.now().isoformat(),
        'period_start': start_date.isoformat(),
        'period_end': end_date.isoformat(),
        'mode': 'ai' if summarizer else 'raw',
        'complexes': []
    }

    complex_index = 0
    total_complexes = len(chats_by_complex)

    for complex_id, chats in chats_by_complex.items():
        complex_index += 1
        complex_name = chats[0]['complex_name'] if chats else 'Unknown'
        print(f"[Report] Processing {complex_index}/{total_complexes}: {complex_name} ({len(chats)} chats)", flush=True)

        complex_data = {
            'complex_id': complex_id,
            'complex_name': complex_name,
            'chats': []
        }

        chats_with_messages = []

        # Sort chats: buildings first (by number), then general chats
        sorted_chats = sort_chats_by_building(chats)

        for chat in sorted_chats:
            # Parse selected topics if any
            topic_ids = None
            if chat.get('selected_topics'):
                try:
                    import json
                    topic_ids = json.loads(chat['selected_topics'])
                except:
                    pass

            chat_name = chat["custom_name"] or chat["original_title"]
            source = normalize_source_name(chat.get("source"))
            source_label = get_source_label(source)
            await emit(
                status="running",
                stage="fetch",
                percent=min(94, 5 + int((completed_work_units / total_work_units) * 85)),
                title=f"{complex_name}: читаю чат",
                detail=f"{source_label} • {chat_name}",
                current_complex=complex_name,
                current_complex_index=complex_index,
                current_chat=chat_name,
                current_source=source_label,
                completed_work_units=completed_work_units,
                total_work_units=total_work_units,
            )

            try:
                messages = await fetch_chat_messages(
                    chat=chat,
                    start_date=start_date,
                    end_date=end_date,
                    topic_ids=topic_ids,
                )
                completed_work_units += 1
                await emit(
                    status="running",
                    stage="fetch_done",
                    percent=min(94, 5 + int((completed_work_units / total_work_units) * 85)),
                    title=f"{complex_name}: чат прочитан",
                    detail=f"{source_label} • {chat_name}: {len(messages)} сообщений",
                    current_complex=complex_name,
                    current_complex_index=complex_index,
                    current_chat=chat_name,
                    current_source=source_label,
                    current_chat_messages=len(messages),
                    completed_work_units=completed_work_units,
                    total_work_units=total_work_units,
                )
            except SourceMessageFetchError as e:
                chat_name = chat['custom_name'] or chat['original_title']
                source = normalize_source_name(chat.get('source'))
                source_label = get_source_label(source)
                print(
                    f"[Report] ERROR fetching {source_label} messages from {chat_name}: {e}\n"
                    f"[Report] Context: {format_chat_log_context(chat, start_date, end_date, topic_ids)}",
                    flush=True
                )
                traceback.print_exc()
                raise HTTPException(
                    status_code=500,
                    detail=f"Не удалось получить сообщения из {source_label} чата '{chat_name}': {e}. Проверьте подключение {source_label} аккаунта."
                )

            chat_name = chat['custom_name'] or chat['original_title']
            report_chat_name = build_report_chat_name(chat)
            content_filter = chat.get('content_filter', '')

            chat_data = {
                'chat_id': chat['id'],
                'chat_name': chat_name,
                'original_title': chat['original_title'],
                'message_count': len(messages)
            }

            complex_data['chats'].append(chat_data)
            chats_with_messages.append({
                'chat_name': chat_name,
                'report_chat_name': report_chat_name,
                'messages': messages,
                'content_filter': content_filter
            })

        # If AI is available, generate summary for the whole complex
        if summarizer:
            # Load custom rules from DB
            rules = await db.get_setting(REPORT_RULES_KEY)
            if rules is None:
                rules = get_default_report_rules()

            total_msgs = sum(c['message_count'] for c in complex_data['chats'])
            print(f"[Report] Summarizing {complex_name}: {len(chats_with_messages)} chats, {total_msgs} messages")

            await emit(
                    status="running",
                    stage="summarize",
                    percent=min(96, 5 + int((completed_work_units / total_work_units) * 85)),
                    title=f"{complex_name}: AI готовит сводку",
                    detail=f"Передаю в модель {len(chats_with_messages)} чатов, {total_msgs} сообщений.",
                    current_complex=complex_name,
                    current_complex_index=complex_index,
                    current_chat=None,
                    current_source="AI",
                    completed_work_units=completed_work_units,
                    total_work_units=total_work_units,
            )

            try:
                summary_text = await summarizer.summarize_complex(
                    complex_name=complex_name,
                    chats_with_messages=chats_with_messages,
                    start_date=start_date,
                    end_date=end_date,
                    rules=rules
                )
                complex_data['summary'] = summary_text
                print(f"[Report] Done: {complex_name}")
            except Exception as e:
                print(f"[Report] Error for {complex_name}: {e}")
                complex_data['summary'] = f'Ошибка AI-суммаризации: {str(e)}'

            completed_work_units += 1
            await emit(
                status="running",
                stage="summarize_done",
                percent=min(98, 5 + int((completed_work_units / total_work_units) * 85)),
                title=f"{complex_name}: сводка готова",
                detail=f"ЖК {complex_index}/{total_complexes} обработан.",
                current_complex=complex_name,
                current_complex_index=complex_index,
                current_chat=None,
                current_source="AI",
                completed_work_units=completed_work_units,
                total_work_units=total_work_units,
            )

        report['complexes'].append(complex_data)

    if summarizer:
        usage = summarizer.get_usage_summary()
        print(
            "[Report] AI usage total: "
            f"calls={usage['calls']}, "
            f"input={usage['prompt_tokens']}, "
            f"output={usage['completion_tokens']}, "
            f"actual_cost=${usage['actual_cost_usd']:.4f}",
            flush=True,
        )

    await emit(
        status="running",
        stage="finish",
        percent=99,
        title="Собираю результат",
        detail="Формирую итоговую сводку для отображения на сайте.",
        completed_work_units=completed_work_units,
        total_work_units=total_work_units,
    )

    return report


@app.post("/api/reports/generate")
async def generate_report(data: GenerateReportRequest):
    """Generate a report synchronously for backwards compatibility."""
    return await _generate_report_payload(data)


@app.post("/api/reports/generate/start")
async def start_report_generation(data: GenerateReportRequest):
    """Start report generation in the background and expose live progress."""
    _cleanup_report_progress_jobs()
    job_id = str(uuid.uuid4())
    REPORT_PROGRESS_JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "stage": "queued",
        "percent": 0,
        "title": "Сводка поставлена в очередь",
        "detail": "Сейчас начну сбор сообщений.",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "updated_ts": datetime.now().timestamp(),
        "result": None,
        "error": None,
    }

    async def progress_update(updates: dict) -> None:
        await _set_report_progress(job_id, **updates)

    async def run_job() -> None:
        try:
            await _set_report_progress(
                job_id,
                status="running",
                stage="start",
                percent=1,
                title="Запускаю генерацию",
                detail="Подготавливаю список ЖК и чатов.",
            )
            result = await _generate_report_payload(data, progress=progress_update)
            await _set_report_progress(
                job_id,
                status="done",
                stage="done",
                percent=100,
                title="Сводка готова",
                detail="Генерация завершена, результат открыт ниже.",
                result=result,
            )
        except HTTPException as e:
            await _set_report_progress(
                job_id,
                status="error",
                stage="error",
                percent=100,
                title="Ошибка генерации",
                detail=str(e.detail),
                error=str(e.detail),
            )
        except Exception as e:
            traceback.print_exc()
            await _set_report_progress(
                job_id,
                status="error",
                stage="error",
                percent=100,
                title="Ошибка генерации",
                detail=str(e),
                error=str(e),
            )

    REPORT_PROGRESS_JOBS[job_id]["task"] = asyncio.create_task(run_job())
    return _public_report_progress_job(REPORT_PROGRESS_JOBS[job_id])


@app.get("/api/reports/generate/progress/{job_id}")
async def get_report_generation_progress(job_id: str):
    _cleanup_report_progress_jobs()
    job = REPORT_PROGRESS_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача генерации не найдена или уже очищена")
    return _public_report_progress_job(job)


# ============== Report Rules ==============

REPORT_RULES_KEY = "report_rules"
WEEKLY_REPORT_RULES_KEY = "weekly_report_rules"


@app.get("/api/report-rules")
async def get_rules():
    """Get the report generation rules text (from DB or default)"""
    rules = await db.get_setting(REPORT_RULES_KEY)
    if rules is None:
        rules = get_default_report_rules()
    return {"rules": rules}


@app.put("/api/report-rules")
async def update_rules(request: Request):
    """Update the report generation rules"""
    data = await request.json()
    rules = data.get("rules", "").strip()
    if not rules:
        raise HTTPException(status_code=400, detail="Rules cannot be empty")
    await db.set_setting(REPORT_RULES_KEY, rules)
    return {"message": "Rules updated"}


@app.post("/api/report-rules/reset")
async def reset_rules():
    """Reset rules to default"""
    default_rules = get_default_report_rules()
    await db.set_setting(REPORT_RULES_KEY, default_rules)
    return {"rules": default_rules, "message": "Rules reset to default"}


@app.get("/api/weekly-report-rules")
async def get_weekly_rules():
    """Get the weekly report generation rules text."""
    rules = await db.get_setting(WEEKLY_REPORT_RULES_KEY)
    if rules is None:
        rules = get_default_weekly_report_rules()
    return {"rules": rules}


@app.put("/api/weekly-report-rules")
async def update_weekly_rules(request: Request):
    """Update weekly report generation rules."""
    data = await request.json()
    rules = data.get("rules", "").strip()
    if not rules:
        raise HTTPException(status_code=400, detail="Rules cannot be empty")
    await db.set_setting(WEEKLY_REPORT_RULES_KEY, rules)
    return {"message": "Weekly rules updated"}


@app.post("/api/weekly-report-rules/reset")
async def reset_weekly_rules():
    """Reset weekly report rules to default."""
    default_rules = get_default_weekly_report_rules()
    await db.set_setting(WEEKLY_REPORT_RULES_KEY, default_rules)
    return {"rules": default_rules, "message": "Weekly rules reset to default"}


# ============== Negativists Analysis ==============

@app.post("/api/negativists/analyze")
async def analyze_negativists(data: AnalyzeNegativistsRequest):
    """Analyze chats to identify negativists and provocateurs"""
    try:
        summarizer = get_summarizer()
    except RuntimeError:
        raise HTTPException(
            status_code=400,
            detail="AI-анализ не настроен. Добавьте OPENROUTER_API_KEY в .env файл."
        )

    # Parse dates
    try:
        start_date = data.get_start_date()
        end_date = data.get_end_date()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Неверный формат даты: {e}")

    # Get selected chats
    chats_with_messages = []

    for chat_id in data.chat_ids:
        chat = await db.get_chat(chat_id)
        if not chat:
            continue

        # Parse selected topics if any
        topic_ids = None
        if chat.get('selected_topics'):
            try:
                import json
                topic_ids = json.loads(chat['selected_topics'])
            except:
                pass

        try:
            messages = await fetch_chat_messages(
                chat=chat,
                start_date=start_date,
                end_date=end_date,
                topic_ids=topic_ids,
            )
        except SourceMessageFetchError as e:
            chat_name = chat['custom_name'] or chat['original_title']
            source = normalize_source_name(chat.get('source'))
            source_label = get_source_label(source)
            print(
                f"[Negativists] ERROR fetching {source_label} messages from {chat_name}: {e}\n"
                f"[Negativists] Context: {format_chat_log_context(chat, start_date, end_date, topic_ids)}",
                flush=True
            )
            traceback.print_exc()
            raise HTTPException(
                status_code=500,
                detail=f"Не удалось получить сообщения из {source_label} чата '{chat_name}': {e}. Проверьте подключение {source_label} аккаунта."
            )

        chat_name = chat['custom_name'] or chat['original_title']
        report_chat_name = build_report_chat_name(chat)
        content_filter = chat.get('content_filter', '')
        chats_with_messages.append({
            'chat_id': chat['id'],
            'chat_name': chat_name,
            'report_chat_name': report_chat_name,
            'source': normalize_source_name(chat.get('source')),
            'messages': messages,
            'content_filter': content_filter
        })

    # Load custom rules from DB
    rules = await db.get_setting(NEGATIVISTS_RULES_KEY)

    # Analyze with AI
    result = await summarizer.analyze_negativists(
        chats_with_messages=chats_with_messages,
        start_date=start_date,
        end_date=end_date,
        rules=rules
    )

    return {
        'period_start': start_date.isoformat(),
        'period_end': end_date.isoformat(),
        'negativists': result.get('negativists', []),
        'analysis_notes': result.get('analysis_notes'),
        'diagnostics': result.get('diagnostics', {}),
    }


# ============== Negativists Rules ==============

NEGATIVISTS_RULES_KEY = "negativists_rules"


@app.get("/api/negativists-rules")
async def get_negativists_rules():
    """Get the negativists analysis rules text (from DB or default)"""
    rules = await db.get_setting(NEGATIVISTS_RULES_KEY)
    if rules is None:
        rules = get_default_negativists_rules()
    return {"rules": rules}


@app.put("/api/negativists-rules")
async def update_negativists_rules(request: Request):
    """Update the negativists analysis rules"""
    data = await request.json()
    rules = data.get("rules", "").strip()
    if not rules:
        raise HTTPException(status_code=400, detail="Rules cannot be empty")
    await db.set_setting(NEGATIVISTS_RULES_KEY, rules)
    return {"message": "Rules updated"}


@app.post("/api/negativists-rules/reset")
async def reset_negativists_rules():
    """Reset negativists rules to default"""
    default_rules = get_default_negativists_rules()
    await db.set_setting(NEGATIVISTS_RULES_KEY, default_rules)
    return {"rules": default_rules, "message": "Rules reset to default"}


# ============== Max Messenger Endpoints ==============

@app.get("/api/max/accounts")
async def get_max_accounts():
    """Get all Max messenger accounts"""
    accounts = await db.get_max_accounts()
    return {"accounts": accounts}


@app.get("/api/max/users/{user_id}")
async def lookup_max_user(user_id: int):
    """Resolve a Max user ID through any available authorized account."""
    if user_id <= 0:
        raise HTTPException(status_code=400, detail="MAX ID должен быть положительным числом")

    accounts = [
        account for account in await db.get_max_accounts()
        if account.get('is_authorized')
    ]
    if not accounts:
        raise HTTPException(status_code=400, detail="Нет авторизованных MAX-аккаунтов для поиска")

    mm = get_max_manager()
    connected_accounts = []
    disconnected_accounts = []
    for account in accounts:
        if await mm.check_connected(account['id']):
            connected_accounts.append(account)
        else:
            disconnected_accounts.append(account)
    ordered_accounts = sorted(
        connected_accounts,
        key=lambda account: bool(account.get('is_send_only')),
    ) + sorted(
        disconnected_accounts,
        key=lambda account: bool(account.get('is_send_only')),
    )

    errors = []
    for account in ordered_accounts:
        try:
            connected = await mm.ensure_connected(account['id'], account['phone'])
            if not connected:
                errors.append(f"{account['name']}: не подключился")
                continue
            profile = await mm.get_user_profile(account['id'], user_id)
            if profile:
                return {
                    **profile,
                    'resolved_via_account_id': account['id'],
                    'resolved_via_account_name': account['name'],
                }
        except Exception as e:
            errors.append(f"{account['name']}: {type(e).__name__}: {str(e) or 'ошибка MAX API'}")

    detail = f"Пользователь MAX с ID {user_id} не найден или его профиль недоступен"
    if errors:
        detail += ". " + "; ".join(errors)
    raise HTTPException(status_code=404, detail=detail)


@app.post("/api/max/accounts")
async def create_max_account(data: MaxAccountCreateRequest):
    """Create a new Max messenger account"""
    account_id = await db.create_max_account(data.phone, data.name, data.is_send_only)
    return {"id": account_id, "message": "Max account created"}


@app.delete("/api/max/accounts/{account_id}")
async def delete_max_account(account_id: int):
    """Delete a Max messenger account"""
    try:
        mm = get_max_manager()
        await mm.disconnect_account(account_id)
    except RuntimeError:
        pass
    await db.delete_max_account(account_id)
    return {"message": "Max account deleted"}


@app.post("/api/max/accounts/{account_id}/auth/start")
async def start_max_auth(account_id: int, force_code: bool = False):
    """Start Max messenger authentication - launches client in background"""
    account = await db.get_max_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max account not found")

    try:
        mm = get_max_manager()
        result = await mm.start_auth(account_id, account['phone'], force_code=force_code)

        # If connected immediately (cached session)
        if result.get('status') == 'success':
            await db.update_max_account_authorized(account_id, True)
        elif force_code:
            await db.update_max_account_authorized(account_id, False)

        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/max/accounts/{account_id}/auth/confirm")
async def confirm_max_auth(account_id: int):
    """Check if Max client has connected after auth"""
    account = await db.get_max_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max account not found")

    try:
        mm = get_max_manager()
        connected = await mm.check_connected(account_id)
        if connected:
            await db.update_max_account_authorized(account_id, True)
            return {"status": "success", "message": "Max аккаунт авторизован!"}
        return {"status": "error", "message": "Клиент ещё не подключен. Проверьте терминал — возможно нужно ввести код."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/max/accounts/{account_id}/auth/verify")
async def verify_max_auth(account_id: int, data: AccountVerifyRequest):
    """Complete Max SMS-code authentication."""
    account = await db.get_max_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max account not found")

    try:
        mm = get_max_manager()
        result = await mm.complete_auth_code(account_id, data.code, data.password)
        if result.get("status") == "success":
            await db.update_max_account_authorized(account_id, True)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/max/accounts/{account_id}/auth/resend-code")
async def resend_max_auth_code(account_id: int):
    """Resend Max SMS-code for the active authentication flow."""
    account = await db.get_max_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max account not found")

    try:
        mm = get_max_manager()
        return await mm.resend_auth_code(account_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/max/accounts/{account_id}/sync")
async def sync_max_chats(account_id: int):
    """Sync chats from Max account (like Telegram sync)"""
    account = await db.get_max_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max account not found")
    if account.get("is_send_only"):
        raise HTTPException(
            status_code=403,
            detail="Этот Max аккаунт помечен как 'только отправка': обычная синхронизация заблокирована. Используйте 'Синхр. чаты отправки'."
        )

    try:
        mm = get_max_manager()

        # Auto-connect if client is not running
        connected = await mm.ensure_connected(account_id, account['phone'])
        if not connected:
            raise HTTPException(
                status_code=400,
                detail="Клиент Max не подключен. Нажмите 'Авторизовать' и дождитесь подключения."
            )

        dialogs = await mm.get_dialogs(account_id)

        synced = 0
        for dialog in dialogs:
            await db.upsert_max_chat(
                max_chat_id=dialog['chat_id'],
                max_account_id=account_id,
                original_title=dialog['title']
            )
            synced += 1

        return {"message": f"Синхронизировано чатов: {synced}", "count": synced}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/max/delivery-chats")
async def get_all_max_delivery_chats():
    """Return cached Max delivery chats for send-only accounts."""
    chats = await db.get_max_delivery_chats()
    return {"chats": chats}


@app.get("/api/max/accounts/{account_id}/delivery-chats")
async def get_max_delivery_chats(account_id: int):
    """Return cached delivery-only chats for one send-only Max account."""
    account = await db.get_max_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max account not found")
    if not account.get("is_send_only"):
        raise HTTPException(
            status_code=403,
            detail="Delivery-синхронизация доступна только для служебных Max аккаунтов."
        )
    chats = await db.get_max_delivery_chats(account_id)
    return {"chats": chats}


@app.post("/api/max/accounts/{account_id}/delivery-chats/sync")
async def sync_max_delivery_chats(account_id: int):
    """Sync only group chat id/title metadata for a send-only Max account."""
    account = await db.get_max_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max account not found")
    if not account.get("is_send_only"):
        raise HTTPException(
            status_code=403,
            detail="Этот endpoint только для служебных Max аккаунтов."
        )
    if not account["is_authorized"]:
        raise HTTPException(status_code=400, detail="Max account not authorized")

    try:
        mm = get_max_manager()
        connected = await mm.ensure_connected(account_id, account["phone"])
        if not connected:
            await db.update_max_account_authorized(account_id, False)
            raise HTTPException(
                status_code=400,
                detail="Сессия Max не подключилась. Нажмите 'Переавторизовать', введите новый SMS-код и повторите синхронизацию."
            )

        dialogs = await mm.get_dialogs(account_id, include_private=False)
        count = await db.replace_max_delivery_chats(account_id, dialogs)
        return {"message": f"Синхронизировано чатов отправки: {count}", "count": count}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/max/accounts/{account_id}/service-messages")
async def get_max_service_messages(account_id: int, limit: int = 20):
    """Get recent private Max messages to retrieve login codes."""
    account = await db.get_max_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max account not found")
    if account.get("is_send_only"):
        raise HTTPException(
            status_code=403,
            detail="Этот Max аккаунт помечен как 'только отправка': просмотр сообщений заблокирован."
        )

    if not account['is_authorized']:
        raise HTTPException(status_code=400, detail="Max account not authorized")

    try:
        mm = get_max_manager()
        connected = await mm.ensure_connected(account_id, account['phone'])
        if not connected:
            raise HTTPException(status_code=400, detail="Клиент Max не подключен")

        messages = await mm.get_service_messages(account_id, limit=limit)
        return {"messages": messages, "account_phone": account['phone']}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/max/accounts/{account_id}/chats")
async def add_max_chat(account_id: int, data: MaxChatAddRequest):
    """Manually add a Max chat to monitor"""
    account = await db.get_max_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max account not found")
    if account.get("is_send_only"):
        raise HTTPException(
            status_code=403,
            detail="Этот Max аккаунт помечен как 'только отправка': добавление чатов мониторинга заблокировано."
        )

    chat_id = await db.upsert_max_chat(
        max_chat_id=data.max_chat_id,
        max_account_id=account_id,
        original_title=data.title
    )
    return {"id": chat_id, "message": "Max chat added"}


# ============== VK Messenger Endpoints ==============

@app.get("/api/vk/accounts")
async def get_vk_accounts():
    accounts = await db.get_vk_accounts()
    return {"accounts": accounts}


@app.post("/api/vk/accounts")
async def create_vk_account(data: VkAccountCreateRequest):
    account_id = await db.create_vk_account(data.name)
    return {"id": account_id, "message": "VK account created"}


@app.put("/api/vk/accounts/{account_id}/token")
async def set_vk_account_token(account_id: int, data: VkTokenUpdateRequest):
    account = await db.get_vk_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="VK account not found")

    access_token = extract_vk_access_token(data.access_token)
    if not access_token:
        raise HTTPException(status_code=400, detail="VK access token is required")

    vk = get_vk_manager()
    try:
        current_user = await vk.get_current_user(access_token)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Не удалось проверить VK token: {e}")

    display_name = " ".join(filter(None, [current_user.get("first_name"), current_user.get("last_name")])).strip()
    if display_name:
        await db.update_vk_account_name(account_id, display_name)

    await db.update_vk_account_tokens(
        account_id,
        vk_user_id=current_user.get("id"),
        access_token=access_token,
        refresh_token=None,
        token_expires_at=None,
        is_authorized=True,
    )

    return {
        "message": "VK token сохранён",
        "vk_user_id": current_user.get("id"),
        "name": display_name or account["name"],
    }


@app.delete("/api/vk/accounts/{account_id}")
async def delete_vk_account(account_id: int):
    account = await db.get_vk_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="VK account not found")
    await db.delete_vk_account(account_id)
    return {"message": "VK account deleted"}


@app.post("/api/vk/accounts/{account_id}/auth/start")
async def start_vk_auth(account_id: int, request: Request):
    account = await db.get_vk_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="VK account not found")

    vk = get_vk_manager()
    if not vk.oauth_enabled:
        raise HTTPException(status_code=400, detail="VK OAuth сейчас не настроен. Используйте ручной ввод access token.")

    auth_url = vk.build_auth_url(account_id, str(request.base_url))
    return {"auth_url": auth_url}


@app.get("/api/vk/auth/callback")
async def vk_auth_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    app_root = str(request.base_url).rstrip("/") + "/"

    if error:
        return RedirectResponse(url=f"{app_root}?vk_auth=error&vk_message={error}")

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing VK OAuth code/state")

    vk = get_vk_manager()
    account_id = vk.pop_pending_account_id(state)
    if not account_id:
        raise HTTPException(status_code=400, detail="VK auth state is expired or invalid")

    redirect_uri = vk.build_redirect_uri(str(request.base_url))
    token_payload = await vk.exchange_code(code=code, redirect_uri=redirect_uri)
    access_token = token_payload.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="VK did not return access_token")

    current_user = await vk.get_current_user(access_token)
    display_name = " ".join(filter(None, [current_user.get("first_name"), current_user.get("last_name")])).strip()
    if display_name:
        await db.update_vk_account_name(account_id, display_name)

    await db.update_vk_account_tokens(
        account_id,
        vk_user_id=current_user.get("id"),
        access_token=access_token,
        refresh_token=token_payload.get("refresh_token"),
        token_expires_at=vk.token_expiry_iso(token_payload.get("expires_in")),
        is_authorized=True,
    )

    return RedirectResponse(url=f"{app_root}?vk_auth=success&vk_account_id={account_id}")


@app.post("/api/vk/accounts/{account_id}/sync")
async def sync_vk_chats(account_id: int):
    account = await db.get_vk_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="VK account not found")
    if not account.get("access_token"):
        raise HTTPException(status_code=400, detail="VK account not authorized")

    vk = get_vk_manager()
    dialogs = await vk.get_dialogs(account["access_token"])

    synced = 0
    for dialog in dialogs:
        await db.upsert_vk_chat(
            vk_chat_id=dialog["chat_id"],
            vk_account_id=account_id,
            original_title=dialog["title"],
        )
        synced += 1

    return {"message": f"Синхронизировано чатов VK: {synced}", "count": synced}


# ============== Proxy Management ==============

@app.get("/api/proxies")
async def get_proxies():
    """Get list of all proxies with their status"""
    pm = get_proxy_manager()
    proxies = []
    for i, proxy in enumerate(pm.proxies):
        proxies.append({
            'index': i,
            'type': proxy.type,
            'host': proxy.host,
            'port': proxy.port,
            'is_working': proxy.is_working,
            'latency': round(proxy.latency, 3) if proxy.latency else None,
            'is_current': proxy is pm.current_proxy
        })
    return {
        'proxies': proxies,
        'current_index': next((i for i, p in enumerate(pm.proxies) if p is pm.current_proxy), None)
    }


@app.post("/api/proxies/recheck")
async def recheck_proxies():
    """Recheck all proxies and find the best one"""
    pm = get_proxy_manager()
    best = await pm.get_best_proxy(force_recheck=True)
    return {
        'message': f'Проверено {len(pm.proxies)} прокси',
        'best': str(best) if best else None
    }


@app.post("/api/proxies/{index}/select")
async def select_proxy(index: int):
    """Manually select a specific proxy"""
    pm = get_proxy_manager()
    if index < 0 or index >= len(pm.proxies):
        raise HTTPException(status_code=404, detail="Proxy not found")
    proxy = pm.proxies[index]
    if not pm._is_supported_proxy(proxy):
        raise HTTPException(status_code=400, detail="Прокси не поддерживается текущей версией Telethon")
    is_working = await pm.check_proxy(proxy)
    if not is_working:
        raise HTTPException(status_code=400, detail="Прокси не отвечает и не может быть выбран")
    pm._current_proxy = proxy
    return {'message': f'Выбран прокси: {proxy}', 'proxy': str(proxy)}


# ============== Health Check ==============

@app.get("/api/health")
async def health_check():
    """Check application health"""
    status = {
        'status': 'ok',
        'telegram_configured': False,
        'max_configured': False,
        'vk_configured': False,
        'summarizer_configured': False,
        'proxy': {
            'configured': False,
            'current': None,
            'working_count': 0,
        },
        'accounts': {
            'telegram_total': 0,
            'telegram_authorized': 0,
            'telegram_connected': 0,
            'max_total': 0,
            'max_authorized': 0,
            'max_connected': 0,
            'vk_total': 0,
            'vk_authorized': 0,
        }
    }

    try:
        tm = get_telegram_manager()
        status['telegram_configured'] = True
        tg_accounts = await db.get_accounts()
        status['accounts']['telegram_total'] = len(tg_accounts)
        status['accounts']['telegram_authorized'] = sum(1 for acc in tg_accounts if acc['is_authorized'])
        status['accounts']['telegram_connected'] = sum(
            1 for acc_id in getattr(tm, '_clients', {})
            if getattr(tm._clients.get(acc_id), 'is_connected', lambda: False)()
        )
    except RuntimeError:
        pass

    try:
        mm = get_max_manager()
        status['max_configured'] = True
        max_accounts = await db.get_max_accounts()
        status['accounts']['max_total'] = len(max_accounts)
        status['accounts']['max_authorized'] = sum(1 for acc in max_accounts if acc['is_authorized'])
        status['accounts']['max_connected'] = sum(
            1 for acc_id in getattr(mm, '_clients', {})
            if bool(getattr(mm._clients.get(acc_id), 'is_connected', False))
        )
    except RuntimeError:
        pass

    try:
        get_vk_manager()
        status['vk_configured'] = True
        vk_accounts = await db.get_vk_accounts()
        status['accounts']['vk_total'] = len(vk_accounts)
        status['accounts']['vk_authorized'] = sum(1 for acc in vk_accounts if acc['is_authorized'])
    except RuntimeError:
        status['vk_configured'] = False

    try:
        get_summarizer()
        status['summarizer_configured'] = True
    except RuntimeError:
        pass

    try:
        pm = get_proxy_manager()
        status['proxy']['configured'] = True
        status['proxy']['working_count'] = sum(1 for proxy in pm.proxies if proxy.is_working)
        if pm.current_proxy:
            status['proxy']['current'] = {
                'type': pm.current_proxy.type,
                'host': pm.current_proxy.host,
                'port': pm.current_proxy.port,
                'is_working': pm.current_proxy.is_working,
                'latency': round(pm.current_proxy.latency, 3) if pm.current_proxy.latency else None,
            }
    except Exception:
        pass

    if status['telegram_configured'] and status['accounts']['telegram_authorized'] and status['accounts']['telegram_connected'] == 0:
        status['status'] = 'degraded'
    if status['max_configured'] and status['accounts']['max_authorized'] and status['accounts']['max_connected'] == 0:
        status['status'] = 'degraded'
    if status.get('vk_configured') and status['accounts']['vk_authorized'] == 0 and status['accounts']['vk_total'] > 0:
        status['status'] = 'degraded'
    if status['proxy']['configured'] and status['accounts']['telegram_authorized'] and status['proxy']['working_count'] == 0:
        status['status'] = 'degraded'

    return status
