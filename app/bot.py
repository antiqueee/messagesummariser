# -*- coding: utf-8 -*-
import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from aiogram.enums import ParseMode

from . import database as db
from .telegram_client import get_telegram_manager
from .summarizer import get_summarizer, get_default_report_rules, get_default_negativists_rules

router = Router()

# Will be set during init
ADMIN_ID: int = 0
REPORT_RULES_KEY = "report_rules"
NEGATIVISTS_RULES_KEY = "negativists_rules"


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def extract_building_number(chat_name: str) -> tuple:
    import re
    if not chat_name:
        return (1, 0, '')
    name_lower = chat_name.lower()
    patterns = [
        r'корпус\s*(\d+)', r'корп\.?\s*(\d+)', r'к\.?\s*(\d+)',
        r'секция\s*(\d+)', r'секц\.?\s*(\d+)', r'^(\d+)\s*корп',
    ]
    for pattern in patterns:
        match = re.search(pattern, name_lower)
        if match:
            return (0, int(match.group(1)), chat_name)
    return (1, 0, chat_name)


def sort_chats_by_building(chats: list[dict]) -> list[dict]:
    return sorted(chats, key=lambda c: extract_building_number(
        c.get('custom_name') or c.get('original_title') or ''))


# ============== Helpers ==============

def split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split long text into chunks for Telegram's message limit"""
    if len(text) <= max_len:
        return [text]

    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        # Try to split at newline
        split_pos = text.rfind('\n', 0, max_len)
        if split_pos == -1:
            split_pos = max_len
        parts.append(text[:split_pos])
        text = text[split_pos:].lstrip('\n')
    return parts


def period_keyboard(prefix: str) -> InlineKeyboardMarkup:
    """Inline keyboard for period selection"""
    buttons = [
        [
            InlineKeyboardButton(text="Вчера", callback_data=f"{prefix}:yesterday"),
            InlineKeyboardButton(text="Сегодня", callback_data=f"{prefix}:today"),
        ],
        [
            InlineKeyboardButton(text="3 дня", callback_data=f"{prefix}:3days"),
            InlineKeyboardButton(text="Неделя", callback_data=f"{prefix}:week"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_period_dates(period: str) -> tuple[datetime, datetime]:
    """Get start/end dates for a period string"""
    now = datetime.now()
    end = now.replace(hour=23, minute=59, second=59)

    if period == "yesterday":
        yesterday = now - timedelta(days=1)
        start = yesterday.replace(hour=0, minute=0, second=0)
        end = yesterday.replace(hour=23, minute=59, second=59)
    elif period == "today":
        start = now.replace(hour=0, minute=0, second=0)
    elif period == "3days":
        start = (now - timedelta(days=3)).replace(hour=0, minute=0, second=0)
    elif period == "week":
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0)
    elif period == "2weeks":
        start = (now - timedelta(days=14)).replace(hour=0, minute=0, second=0)
    else:
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0)

    return start, end


def format_period(start: datetime, end: datetime) -> str:
    return f"{start.strftime('%d.%m.%Y %H:%M')} — {end.strftime('%d.%m.%Y %H:%M')}"


# ============== Commands ==============

@router.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "AI-агент охранки\n\n"
        "Команды:\n"
        "/report — Сформировать сводку\n"
        "/negativists — Выявить негативщиков\n"
        "/chats — Список отслеживаемых чатов\n"
        "/help — Помощь"
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "Как пользоваться:\n\n"
        "1. /report — выберите период, бот сформирует сводку по всем ЖК\n"
        "2. /negativists — выберите период, бот проанализирует негативщиков\n"
        "3. /chats — покажет список ЖК и подключённых чатов\n\n"
        "Настройка чатов, ЖК и аккаунтов — через веб-интерфейс."
    )


@router.message(Command("chats"))
async def cmd_chats(message: Message):
    if not is_admin(message.from_user.id):
        return

    complexes = await db.get_complexes()
    if not complexes:
        await message.answer("Нет добавленных ЖК. Настройте через веб-интерфейс.")
        return

    text_parts = ["Отслеживаемые ЖК и чаты:\n"]

    for comp in complexes:
        chats = await db.get_chats_by_complex(comp['id'])
        monitored = [c for c in chats if c['is_monitored']]
        text_parts.append(f"\n{comp['name']} ({len(monitored)} чатов)")
        for chat in monitored:
            name = chat['custom_name'] or chat['original_title']
            text_parts.append(f"  • {name}")

    await message.answer("\n".join(text_parts))


# ============== Report ==============

@router.message(Command("report"))
async def cmd_report(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer("Выберите период для сводки:", reply_markup=period_keyboard("report"))


@router.callback_query(F.data.startswith("report:"))
async def cb_report(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    period = callback.data.split(":")[1]
    start_date, end_date = get_period_dates(period)

    await callback.message.edit_text(
        f"Генерация сводки...\nПериод: {format_period(start_date, end_date)}\n\nЭто может занять несколько минут."
    )

    try:
        report = await generate_full_report(start_date, end_date)
        if not report:
            await callback.message.edit_text("Нет данных для отчёта. Проверьте что есть ЖК с отслеживаемыми чатами.")
            return

        # Send report for each complex
        for complex_report in report:
            text = f"📋 {complex_report['name']}\n"
            text += f"Период: {format_period(start_date, end_date)}\n"
            text += f"{'—' * 30}\n\n"

            if complex_report.get('summary'):
                text += complex_report['summary']
            else:
                text += "Нет сообщений за указанный период."

            for part in split_message(text):
                await callback.message.answer(part)

        await callback.message.delete()

    except Exception as e:
        await callback.message.edit_text(f"Ошибка: {str(e)}")


async def generate_full_report(start_date: datetime, end_date: datetime) -> list[dict]:
    """Generate report for all complexes with monitored chats"""
    try:
        tm = get_telegram_manager()
    except RuntimeError:
        raise Exception("Telegram менеджер не инициализирован")

    try:
        summarizer = get_summarizer()
    except RuntimeError:
        summarizer = None

    complexes = await db.get_complexes()
    results = []

    # Load rules
    rules = await db.get_setting(REPORT_RULES_KEY)
    if rules is None:
        rules = get_default_report_rules()

    for comp in complexes:
        chats = await db.get_chats_by_complex(comp['id'])
        monitored = [c for c in chats if c['is_monitored']]

        if not monitored:
            continue

        sorted_chats = sort_chats_by_building(monitored)
        chats_with_messages = []

        for chat in sorted_chats:
            # Parse selected topics
            topic_ids = None
            if chat.get('selected_topics'):
                try:
                    topic_ids = json.loads(chat['selected_topics'])
                except:
                    pass

            messages = await tm.get_messages(
                account_id=chat['account_id'],
                chat_telegram_id=chat['telegram_id'],
                start_date=start_date,
                end_date=end_date,
                topic_ids=topic_ids
            )

            chat_name = chat['custom_name'] or chat['original_title']
            content_filter = chat.get('content_filter', '')
            chats_with_messages.append({
                'chat_name': chat_name,
                'messages': messages,
                'content_filter': content_filter
            })

        complex_result = {'name': comp['name'], 'summary': None}

        if summarizer and chats_with_messages:
            try:
                summary = await summarizer.summarize_complex(
                    complex_name=comp['name'],
                    chats_with_messages=chats_with_messages,
                    start_date=start_date,
                    end_date=end_date,
                    rules=rules
                )
                complex_result['summary'] = summary
            except Exception as e:
                complex_result['summary'] = f"Ошибка AI: {str(e)}"
        elif not summarizer:
            complex_result['summary'] = "AI-суммаризатор не настроен (нет OPENROUTER_API_KEY)"

        results.append(complex_result)

    return results


# ============== Negativists ==============

@router.message(Command("negativists"))
async def cmd_negativists(message: Message):
    if not is_admin(message.from_user.id):
        return

    buttons = [
        [
            InlineKeyboardButton(text="3 дня", callback_data="neg:3days"),
            InlineKeyboardButton(text="Неделя", callback_data="neg:week"),
        ],
        [
            InlineKeyboardButton(text="2 недели", callback_data="neg:2weeks"),
        ],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Выберите период для анализа негативщиков:", reply_markup=kb)


@router.callback_query(F.data.startswith("neg:"))
async def cb_negativists(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    period = callback.data.split(":")[1]
    start_date, end_date = get_period_dates(period)

    await callback.message.edit_text(
        f"Анализ негативщиков...\nПериод: {format_period(start_date, end_date)}\n\nЭто может занять несколько минут."
    )

    try:
        result = await analyze_all_negativists(start_date, end_date)

        if not result or not result.get('negativists'):
            text = f"За период {format_period(start_date, end_date)} негативщиков не выявлено."
            if result and result.get('analysis_notes'):
                text += f"\n\nЗаметки: {result['analysis_notes']}"
            await callback.message.edit_text(text)
            return

        # Format negativists
        text = f"Негативщики за {format_period(start_date, end_date)}:\n\n"

        threat_emoji = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}

        for i, neg in enumerate(result['negativists'], 1):
            emoji = threat_emoji.get(neg.get('threat_level', 'low'), '⚪')
            name = neg.get('name', 'Неизвестный')
            username = neg.get('username')
            status = neg.get('status', '')
            tags = neg.get('tags', [])

            text += f"{emoji} {i}. {name}"
            if username:
                text += f" (@{username})"
            if tags:
                text += f" [{', '.join(tags)}]"
            text += f"\n{status}\n\n"

        if result.get('analysis_notes'):
            text += f"\nЗаметки: {result['analysis_notes']}"

        for part in split_message(text):
            await callback.message.answer(part)

        await callback.message.delete()

    except Exception as e:
        await callback.message.edit_text(f"Ошибка: {str(e)}")


async def analyze_all_negativists(start_date: datetime, end_date: datetime) -> dict:
    """Analyze negativists across all monitored chats"""
    try:
        tm = get_telegram_manager()
    except RuntimeError:
        raise Exception("Telegram менеджер не инициализирован")

    try:
        summarizer = get_summarizer()
    except RuntimeError:
        raise Exception("AI-суммаризатор не настроен")

    # Get all monitored chats
    complexes = await db.get_complexes()
    chats_with_messages = []

    for comp in complexes:
        chats = await db.get_chats_by_complex(comp['id'])
        monitored = [c for c in chats if c['is_monitored']]

        for chat in monitored:
            topic_ids = None
            if chat.get('selected_topics'):
                try:
                    topic_ids = json.loads(chat['selected_topics'])
                except:
                    pass

            messages = await tm.get_messages(
                account_id=chat['account_id'],
                chat_telegram_id=chat['telegram_id'],
                start_date=start_date,
                end_date=end_date,
                topic_ids=topic_ids
            )

            chat_name = chat['custom_name'] or chat['original_title']
            content_filter = chat.get('content_filter', '')
            chats_with_messages.append({
                'chat_name': chat_name,
                'messages': messages,
                'content_filter': content_filter
            })

    if not chats_with_messages:
        return {"negativists": [], "analysis_notes": "Нет отслеживаемых чатов"}

    # Load rules
    rules = await db.get_setting(NEGATIVISTS_RULES_KEY)
    if rules is None:
        rules = get_default_negativists_rules()

    return await summarizer.analyze_negativists(
        chats_with_messages=chats_with_messages,
        start_date=start_date,
        end_date=end_date,
        rules=rules
    )


# ============== Bot Init ==============

_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None


async def start_bot(token: str, admin_id: int):
    """Start the bot polling in background"""
    global _bot, _dp, ADMIN_ID
    ADMIN_ID = admin_id

    _bot = Bot(token=token)
    _dp = Dispatcher()
    _dp.include_router(router)

    print(f"[Bot] Starting bot, admin_id={admin_id}")

    # Start polling in background task
    asyncio.create_task(_run_polling())


async def _run_polling():
    """Run bot polling (called as background task)"""
    try:
        await _dp.start_polling(_bot, handle_signals=False)
    except Exception as e:
        print(f"[Bot] Polling error: {e}")


async def stop_bot():
    """Stop the bot"""
    global _bot, _dp
    if _dp:
        await _dp.stop_polling()
    if _bot:
        await _bot.session.close()
    print("[Bot] Stopped")
