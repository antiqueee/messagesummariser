# CLAUDE.md — Контекст проекта для AI-ассистента

## Что это за проект

**AI-агент охранки** — веб-приложение для мониторинга и AI-суммаризации чатов из Telegram и Max мессенджеров.
Целевая аудитория — управляющие компании жилых комплексов (ЖК). Приложение собирает сообщения из чатов жильцов, генерирует AI-сводки и выявляет негативщиков.

## Архитектура

- **Backend**: FastAPI + aiosqlite (SQLite), один файл `app/main.py` со всеми эндпоинтами
- **Frontend**: Single-page HTML (`app/templates/index.html`) с vanilla JS, без фреймворков
- **Telegram**: Telethon (userbot API), прокси через python-socks
- **Max мессенджер**: maxapi-python (pymax), SocketMaxClient через WebSocket
- **AI**: OpenRouter API (httpx), модели Gemini Flash
- **Бот**: aiogram — Telegram бот для отправки сводок подписчикам

## Структура файлов

```
app/
  main.py           — FastAPI эндпоинты (Telegram, Max, отчёты, прокси)
  database.py        — SQLite через aiosqlite (accounts, max_accounts, chats, complexes, settings)
  models.py          — Pydantic модели запросов
  summarizer.py      — OpenRouter API клиент с автоподбором модели
  telegram_client.py — Telethon менеджер сессий
  max_client.py      — pymax менеджер сессий
  proxy_manager.py   — MTProto/SOCKS5 прокси менеджер
  bot.py             — Telegram бот (aiogram)
  templates/
    index.html       — Весь фронтенд (HTML + CSS + JS)
sessions/            — Файлы сессий Telethon и pymax (НЕ коммитить)
data/                — SQLite база (НЕ коммитить)
```

## Критические правила (чтобы не повторять ошибки)

### OpenRouter / AI модели
- **Модели на OpenRouter постоянно переименовываются.** НЕ хардкодить одну модель.
  Используется автоподбор: `summarizer.py` → `_find_working_model()` запрашивает `/api/v1/models`
  и ищет рабочую Gemini Flash модель автоматически.
- Если пользователь задал `AI_MODEL` в `.env`, использовать её, но с фоллбэком при ошибке.

### pymax (Max мессенджер)
- Использовать **`SocketMaxClient`**, НЕ `MaxClient` (MaxClient только для device_type=WEB).
- **`reconnect=False`** обязательно! При reconnect=True pymax дублирует `client.chats` и `client.dialogs`
  при каждом переподключении (3→6→9→...), что ведёт к крашу Python.
- `client.chats` — группы (есть `.id`, `.title`), `client.dialogs` — личные (есть `.id`, нет `.title`).
- Синхронизировать только группы (`client.chats`), личные диалоги пользователю не нужны.
- Дедупликация по `chat.id` обязательна (pymax может добавлять дубли).
- `on_start` callback вызывается после sync — добавить `await asyncio.sleep(1.0)` перед `ready.set()`,
  чтобы pymax успел заполнить `client.chats`.
- Python 3.12 + pymax SSL = нестабильно. Отключение reconnect решает проблему.

### База данных (SQLite)
- Колонка `source` в таблице `chats` может быть **NULL** для старых Telegram-чатов.
  Всегда использовать `chat.get('source') or 'telegram'`, НЕ `chat.get('source', 'telegram')`.
  (`dict.get` возвращает `None` когда ключ есть но значение NULL.)
- JOINы в SQL: использовать `(c.source IS NULL OR c.source = 'telegram')` для Telegram-чатов.
- Max-чаты хранятся с `account_id=0` и `source='max'`, реальный аккаунт в `max_account_id`.
- UNIQUE constraint на `(telegram_id, account_id)` — Max-чаты не конфликтуют т.к. `account_id=0`.

### Прокси
- Прокси — **только для Telegram**. Max подключается напрямую через WebSocket.
- Модуль `pysocks` может быть не установлен — обработать `ImportError` gracefully.
- При отсутствии `pysocks` подключаться к Telegram напрямую без прокси.

### Фронтенд
- **HTML кэшируется браузером.** На эндпоинте `/` выставлять `Cache-Control: no-cache, no-store`.
- Ошибки 422 от FastAPI приходят как `detail: [{msg: "..."}]` (массив), не строка.
  Функция `api()` в JS должна обрабатывать оба формата.
- Даты из `<input type="datetime-local">` — парсить на бэкенде через `parse_flexible_datetime()`,
  не полагаться на формат фронтенда.

### Общее
- Автозапуск авторизованных Max-аккаунтов при старте сервера (из кэшированных сессий).
- Пользователь общается на русском, весь UI на русском.
- НЕ коммитить: `sessions/`, `data/`, `.env`, `__pycache__/`.

## Запуск

```bash
source venv/bin/activate  # если есть venv
pip install -r requirements.txt
python run.py
# Открыть http://localhost:8000
```

## Git

- Основная рабочая ветка: `claude/add-max-api-integration-oIWFA`
- После завершения работы — мержить в main, чтобы фиксы не терялись между сессиями.
