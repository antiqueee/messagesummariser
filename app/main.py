import os
import re
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

from . import database as db


def extract_building_number(chat_name: str) -> tuple[int, str]:
    """Extract building number for sorting. Returns (sort_key, name).
    Building chats (корпус X, секция X) get priority, general chats go last."""
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
    return sorted(chats, key=lambda c: extract_building_number(c.get('chat_name', c.get('custom_name', c.get('original_title', '')))))
from .telegram_client import init_telegram_manager, get_telegram_manager
from .summarizer import init_summarizer, get_summarizer, get_default_report_rules, get_default_negativists_rules
from .models import (
    AccountCreateRequest, AccountVerifyRequest,
    ComplexCreateRequest, ChatUpdateRequest, GenerateReportRequest,
    AnalyzeNegativistsRequest
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

    if api_id and api_hash:
        init_telegram_manager(int(api_id), api_hash)

    if openrouter_key:
        try:
            init_summarizer(openrouter_key, ai_model)
            print(f"Summarizer initialized with model: {ai_model or 'google/gemini-2.5-flash-preview'}")
        except Exception as e:
            print(f"WARNING: Summarizer init skipped ({e}). Summarization will be available later.")

    yield

    # Shutdown
    try:
        tm = get_telegram_manager()
        await tm.close_all()
    except RuntimeError:
        pass


app = FastAPI(
    title="Telegram Chat Summarizer",
    description="Приложение для мониторинга и суммаризации чатов Telegram",
    version="1.0.0",
    lifespan=lifespan
)

# Templates
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


# ============== HTML Pages ==============

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


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


# ============== Chat Endpoints ==============

@app.get("/api/chats")
async def get_chats(account_id: Optional[int] = None, complex_id: Optional[int] = None,
                    monitored_only: bool = False):
    """Get chats with optional filters"""
    chats = await db.get_chats(account_id, complex_id, monitored_only)
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
        is_monitored=data.is_monitored
    )
    return {"message": "Chat updated"}


# ============== Report Generation ==============

@app.post("/api/reports/generate")
async def generate_report(data: GenerateReportRequest):
    """Generate a report for selected complexes — AI summary if available, raw messages otherwise"""
    try:
        tm = get_telegram_manager()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Check if AI summarizer is available
    summarizer = None
    try:
        summarizer = get_summarizer()
    except RuntimeError:
        pass

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

    report = {
        'generated_at': datetime.now().isoformat(),
        'period_start': data.start_date.isoformat(),
        'period_end': data.end_date.isoformat(),
        'mode': 'ai' if summarizer else 'raw',
        'complexes': []
    }

    for complex_id, chats in chats_by_complex.items():
        complex_name = chats[0]['complex_name'] if chats else 'Unknown'

        complex_data = {
            'complex_id': complex_id,
            'complex_name': complex_name,
            'chats': []
        }

        chats_with_messages = []

        # Sort chats: buildings first (by number), then general chats
        sorted_chats = sort_chats_by_building(chats)

        for chat in sorted_chats:
            # Get messages from Telegram
            messages = await tm.get_messages(
                account_id=chat['account_id'],
                chat_telegram_id=chat['telegram_id'],
                start_date=data.start_date,
                end_date=data.end_date
            )

            chat_name = chat['custom_name'] or chat['original_title']

            chat_data = {
                'chat_id': chat['id'],
                'chat_name': chat_name,
                'original_title': chat['original_title'],
                'message_count': len(messages)
            }

            complex_data['chats'].append(chat_data)
            chats_with_messages.append({
                'chat_name': chat_name,
                'messages': messages
            })

        # If AI is available, generate summary for the whole complex
        if summarizer:
            # Load custom rules from DB
            rules = await db.get_setting(REPORT_RULES_KEY)
            if rules is None:
                rules = get_default_report_rules()

            try:
                summary_text = await summarizer.summarize_complex(
                    complex_name=complex_name,
                    chats_with_messages=chats_with_messages,
                    start_date=data.start_date,
                    end_date=data.end_date,
                    rules=rules
                )
                complex_data['summary'] = summary_text
            except Exception as e:
                complex_data['summary'] = f'Ошибка AI-суммаризации: {str(e)}'

        report['complexes'].append(complex_data)

    return report


# ============== Report Rules ==============

REPORT_RULES_KEY = "report_rules"


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

    try:
        tm = get_telegram_manager()
    except RuntimeError:
        raise HTTPException(status_code=400, detail="Telegram не настроен")

    # Get selected chats
    chats_with_messages = []

    for chat_id in data.chat_ids:
        chat = await db.get_chat(chat_id)
        if not chat:
            continue

        # Get messages from Telegram
        messages = await tm.get_messages(
            account_id=chat['account_id'],
            chat_telegram_id=chat['telegram_id'],
            start_date=data.start_date,
            end_date=data.end_date
        )

        chat_name = chat['custom_name'] or chat['original_title']
        chats_with_messages.append({
            'chat_name': chat_name,
            'messages': messages
        })

    # Load custom rules from DB
    rules = await db.get_setting(NEGATIVISTS_RULES_KEY)

    # Analyze with AI
    result = await summarizer.analyze_negativists(
        chats_with_messages=chats_with_messages,
        start_date=data.start_date,
        end_date=data.end_date,
        rules=rules
    )

    return {
        'period_start': data.start_date.isoformat(),
        'period_end': data.end_date.isoformat(),
        'negativists': result.get('negativists', []),
        'analysis_notes': result.get('analysis_notes')
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


# ============== Health Check ==============

@app.get("/api/health")
async def health_check():
    """Check application health"""
    status = {
        'status': 'ok',
        'telegram_configured': False,
        'summarizer_configured': False
    }

    try:
        get_telegram_manager()
        status['telegram_configured'] = True
    except RuntimeError:
        pass

    try:
        get_summarizer()
        status['summarizer_configured'] = True
    except RuntimeError:
        pass

    return status
