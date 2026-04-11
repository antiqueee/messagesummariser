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
from .telegram_client import init_telegram_manager, get_telegram_manager
from .max_client import init_max_manager, get_max_manager
from .proxy_manager import get_proxy_manager
from .summarizer import init_summarizer, get_summarizer, get_default_report_rules, get_default_negativists_rules
from .bot import start_bot, stop_bot
from .models import (
    AccountCreateRequest, AccountVerifyRequest,
    ComplexCreateRequest, ChatUpdateRequest, GenerateReportRequest,
    AnalyzeNegativistsRequest, MaxAccountCreateRequest, MaxChatAddRequest
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

    # Initialize Max messenger manager
    mm = init_max_manager()
    print("[Max] Max messenger manager initialized")

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

# Templates
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


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

@app.post("/api/reports/generate")
async def generate_report(data: GenerateReportRequest):
    """Generate a report for selected complexes — AI summary if available, raw messages otherwise"""
    try:
        tm = get_telegram_manager()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

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

            # Get messages from appropriate source
            source = chat.get('source') or 'telegram'
            if source == 'max':
                try:
                    mm = get_max_manager()
                    # Auto-reconnect if connection dropped
                    max_acc = await db.get_max_account(chat['max_account_id'])
                    if max_acc:
                        await mm.ensure_connected(chat['max_account_id'], max_acc['phone'])
                    messages = await mm.get_messages(
                        account_id=chat['max_account_id'],
                        chat_id=chat['telegram_id'],
                        start_date=start_date,
                        end_date=end_date,
                    )
                except Exception as e:
                    chat_name = chat['custom_name'] or chat['original_title']
                    print(f"[Report] ERROR fetching Max messages from {chat_name}: {e}", flush=True)
                    raise HTTPException(
                        status_code=500,
                        detail=f"Не удалось получить сообщения из Max чата '{chat_name}': {e}. Проверьте подключение Max аккаунта."
                    )
            else:
                try:
                    messages = await tm.get_messages(
                        account_id=chat['account_id'],
                        chat_telegram_id=chat['telegram_id'],
                        start_date=start_date,
                        end_date=end_date,
                        topic_ids=topic_ids
                    )
                except Exception as e:
                    chat_name = chat['custom_name'] or chat['original_title']
                    print(f"[Report] ERROR fetching messages from {chat_name}: {e}", flush=True)
                    raise HTTPException(
                        status_code=500,
                        detail=f"Не удалось получить сообщения из чата '{chat_name}': {e}. Проверьте подключение Telegram аккаунта."
                    )

            chat_name = chat['custom_name'] or chat['original_title']
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

        # Get messages from appropriate source
        source = chat.get('source') or 'telegram'
        if source == 'max':
            try:
                mm = get_max_manager()
                max_acc = await db.get_max_account(chat['max_account_id'])
                if max_acc:
                    await mm.ensure_connected(chat['max_account_id'], max_acc['phone'])
                messages = await mm.get_messages(
                    account_id=chat['max_account_id'],
                    chat_id=chat['telegram_id'],
                    start_date=start_date,
                    end_date=end_date,
                )
            except Exception as e:
                chat_name = chat['custom_name'] or chat['original_title']
                print(f"[Negativists] ERROR fetching Max messages from {chat_name}: {e}", flush=True)
                raise HTTPException(
                    status_code=500,
                    detail=f"Не удалось получить сообщения из Max чата '{chat_name}': {e}. Проверьте подключение Max аккаунта."
                )
        else:
            try:
                messages = await tm.get_messages(
                    account_id=chat['account_id'],
                    chat_telegram_id=chat['telegram_id'],
                    start_date=start_date,
                    end_date=end_date,
                    topic_ids=topic_ids
                )
            except Exception as e:
                chat_name = chat['custom_name'] or chat['original_title']
                print(f"[Negativists] ERROR fetching messages from {chat_name}: {e}", flush=True)
                raise HTTPException(
                    status_code=500,
                    detail=f"Не удалось получить сообщения из чата '{chat_name}': {e}. Проверьте подключение Telegram аккаунта."
                )

        chat_name = chat['custom_name'] or chat['original_title']
        content_filter = chat.get('content_filter', '')
        chats_with_messages.append({
            'chat_name': chat_name,
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


# ============== Max Messenger Endpoints ==============

@app.get("/api/max/accounts")
async def get_max_accounts():
    """Get all Max messenger accounts"""
    accounts = await db.get_max_accounts()
    return {"accounts": accounts}


@app.post("/api/max/accounts")
async def create_max_account(data: MaxAccountCreateRequest):
    """Create a new Max messenger account"""
    account_id = await db.create_max_account(data.phone, data.name)
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
async def start_max_auth(account_id: int):
    """Start Max messenger authentication - launches client in background"""
    account = await db.get_max_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max account not found")

    try:
        mm = get_max_manager()
        result = await mm.start_auth(account_id, account['phone'])

        # If connected immediately (cached session)
        if result.get('status') == 'success':
            await db.update_max_account_authorized(account_id, True)

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


@app.post("/api/max/accounts/{account_id}/sync")
async def sync_max_chats(account_id: int):
    """Sync chats from Max account (like Telegram sync)"""
    account = await db.get_max_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max account not found")

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


@app.post("/api/max/accounts/{account_id}/chats")
async def add_max_chat(account_id: int, data: MaxChatAddRequest):
    """Manually add a Max chat to monitor"""
    account = await db.get_max_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Max account not found")

    chat_id = await db.upsert_max_chat(
        max_chat_id=data.max_chat_id,
        max_account_id=account_id,
        original_title=data.title
    )
    return {"id": chat_id, "message": "Max chat added"}


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
        'summarizer_configured': False
    }

    try:
        get_telegram_manager()
        status['telegram_configured'] = True
    except RuntimeError:
        pass

    try:
        get_max_manager()
        status['max_configured'] = True
    except RuntimeError:
        pass

    try:
        get_summarizer()
        status['summarizer_configured'] = True
    except RuntimeError:
        pass

    return status
