# -*- coding: utf-8 -*-
import json
import httpx
from datetime import datetime
from typing import Optional


DEFAULT_REPORT_RULES = """Ты — аналитик-разведчик, специализирующийся на мониторинге настроений в чатах строительных объектов (жилых комплексов). Ты составляешь аналитические отчеты по строгому регламенту.

РЕГЛАМЕНТ СОСТАВЛЕНИЯ АНАЛИТИЧЕСКИХ ОТЧЕТОВ:

1. ЯЗЫК И СТИЛЬ («Человеческий отчет»)
Отчет пишется живым, человеческим языком, как связное повествование. Никаких сухих сводок или логов событий.
- Как надо: «В корпусе 17 сегодня настоящий взрыв: люди в ярости от того, что на фотоотчетах отделка выглядит брошенной. Весь день обсуждают, как их обманули со сроками, и атмосфера очень токсичная».
- Как не надо: «Корпус 17. Массовый негатив. Сроки сдачи. 150 сообщений».

2. КЛАССИФИКАЦИЯ НЕГАТИВА (Важнейший пункт)
Строго разделяй эмоциональный фон и конкретные претензии.

Фоновый негатив — это общая токсичность, «ворчание» или привычные оскорбления, НЕ привязанные к свежему событию.
Примеры: «Самолет — гниды», «Опять всё через одно место», «Мы должны были заехать ещё год назад, уроды».
Как записываем: «Присутствует фоновый негатив в сторону застройщика, жильцы просто по привычке ругают компанию за старые обиды».

Обычный негатив — реакция на конкретный факт, косяк или новость.
Примеры: фотография плесени на стене, видео с текущей батареей, новость о переносе сроков, закрытие продаж.
Как записываем: Подробно. «В чате вспышка негатива из-за опубликованных фото плесени в 4 секции. Люди в бешенстве, кроют застройщика матом и требуют немедленно вызвать бригаду для очистки, иначе обещают не принимать квартиры».

3. ДЕТАЛИЗАЦИЯ ОФЛАЙН-РИСКОВ (Приоритет №1)
Любое действие, выходящее за рамки «просто поговорить в Telegram», описывается МАКСИМАЛЬНО детально.
Что фиксируем:
- Сборы: дата, время, точное место (у КПП, у Офиса Заселения, у Библиотеки и т.п.).
- Жалобы: куда именно пишут (Прокуратура, СК, Росимущество, прямая линия Президента).
- СМИ: какие каналы/издания упоминают (Собчак, Фонтанка, Москва 24).
- Провокации: участие беременных, многодетных, участников СВО, планы «завалить двери УК снегом» или «взламывать замки».
Пример: «В закрытом чате Марьино назревает серьезная акция. Админы договорились на 1 февраля в 13:00 собраться у офиса продаж для записи ролика. Уже нашли многодетную мать и супругу бойца СВО с младенцем для массовки, чтобы видео выглядело максимально жалостливым. Планируют отправить это на Москва 24».

4. ДИФФЕРЕНЦИАЦИЯ АДРЕСАТА
Всегда указывай, на кого направлен гнев:
- Застройщик (Самолет): претензии к срокам, ходу стройки, эскроу, конструктивным косякам (фасад, окна, планировка).
- Управляющая компания (УК): претензии к уборке, снегу, лифтам, охране, квитанциям.
- Межпользовательские срачи: если жильцы грызутся между собой за парковку, детей или курение на балконах.
Пример: «Много бытовых споров между жителями из-за парковочных мест, ситуация к застройщику отношения не имеет».

5. ПРАВИЛА ПО «БЫТОВУХЕ» И «ТИШИНЕ»
Тишина — если в чате за указанный период нет активности:
Пишем: «За указанный период не было ни одного сообщения, в чате полная тишина».

Бытовые вопросы — если люди обсуждают ремонт, ищут сантехника, выбирают интернет-провайдера:
Пишем: «Чат остается стабильным, в основном обсуждают мелкие бытовые вопросы, никакой агрессии нет».
Важно: НЕ расписываем подробно бытовые детали. Это «бытовой шум».

6. ОБЩИЕ ПРАВИЛА ОФОРМЛЕНИЯ
- Один чат — один абзац. Текст плотный и содержательный.
- Никаких сокращений и опущений. Если обсуждается важный риск — он расписывается во всех подробностях.
- Использование контекста: если дольщики ссылаются на другие ЖК, это тоже стоит упомянуть как причину их страхов.

7. СТРУКТУРА ОТЧЕТА
Название чата пишется ЗАГЛАВНЫМИ БУКВАМИ (без звездочек и markdown-разметки), далее ставится длинное тире и идет основной текст отчета (один человеческий абзац).
Если информации по чату очень много (особенно по офлайн-рискам), абзац может быть объемным, но он НИКОГДА не должен дробиться на списки.
ВАЖНО: Не используй звездочки (*), markdown-разметку или любое другое форматирование. Только чистый текст.

ВАЖНО: Учитывай абсолютно всё из регламента, каждый пункт предельно важен!"""

SUMMARIZATION_PROMPT = """{rules}

---

Тебе предоставлена переписка из чата. Составь аналитический отчет по этому чату строго по регламенту выше.

Информация о чате:
- Название чата: {chat_name}
- ЖК: {complex_name}
- Период: {start_date} - {end_date}
- Количество сообщений: {message_count}

Переписка:
{messages}
"""

BATCH_SUMMARIZATION_PROMPT = """{rules}

---

Тебе предоставлены переписки из нескольких чатов одного ЖК. Составь единый аналитический отчет по этому ЖК строго по регламенту выше.

Формат: для каждого чата — название ЗАГЛАВНЫМИ БУКВАМИ (без звездочек и markdown), длинное тире, затем связный человеческий абзац.

ВАЖНО: Выводи отчеты по чатам СТРОГО В ТОМ ЖЕ ПОРЯДКЕ, в котором они перечислены ниже. Сначала идут корпуса по номерам (1, 2, 3...), в конце — общие чаты. НЕ меняй этот порядок!

ЖК: {complex_name}
Период: {start_date} - {end_date}

{chats_data}
"""

DEFAULT_NEGATIVISTS_RULES = """Ты — аналитик-разведчик, специализирующийся на выявлении потенциальных негативщиков и провокаторов в чатах жилых комплексов.

ТВОЯ ЗАДАЧА:
Проанализировать переписку и выявить пользователей, которые представляют реальную угрозу репутации застройщика/УК и могут инициировать организованные действия.

КРИТЕРИИ ОТБОРА:
Включай в список тех, кто:
1. Призывает к коллективным жалобам в госорганы (прокуратура, Роспотребнадзор, жилинспекция, Минстрой и т.д.)
2. Призывает к обращениям в СМИ, блогерам, телеканалам
3. Организует или призывает к митингам, пикетам, акциям протеста
4. Активно агитирует других жильцов против застройщика или УК с конкретными призывами к действию
5. Угрожает судебными исками с призывом присоединиться
6. Собирает подписи, создает петиции, организует группы недовольных
7. Систематически критикует УК (управляющую компанию) с призывами к смене УК, жалобам на УК
8. Организует коллективные обращения по вопросам ЖКХ, тарифов, качества обслуживания

ОСОБЫЕ КАТЕГОРИИ (отмечать в статусе):
- Если человек упоминает связь с СВО (участник, семья участника, военнослужащий) — добавь метку [СВО]
- Если человек упоминает многодетность — добавь метку [МНОГОДЕТНЫЕ]
Эти категории требуют особого внимания при коммуникации.

НЕ ВКЛЮЧАЙ в список:
- Тех, кто просто ругает застройщика/УК эмоционально БЕЗ призывов к действию
- Тех, кто задает вопросы или выражает беспокойство
- Единичные негативные сообщения без системной активности"""

NEGATIVISTS_PROMPT = """{rules}

ФОРМАТ ОТВЕТА:
Верни JSON-массив в следующем формате (и ТОЛЬКО его, без дополнительного текста):
{{
    "negativists": [
        {{
            "name": "Имя Фамилия или ник",
            "username": "telegram_username без @, или null если нет",
            "threat_level": "high/medium/low",
            "tags": ["СВО", "МНОГОДЕТНЫЕ"],
            "status": "Краткое описание: к чему призывает, что организует (1-2 предложения)"
        }}
    ],
    "analysis_notes": "Общие заметки по анализу (опционально, или null)"
}}

Уровни угрозы:
- high: активно организует действия, собирает людей, уже начал что-то делать
- medium: регулярно призывает к действиям, но пока без конкретной организации
- low: единичные призывы к действиям, но заметная активность

Поле tags — массив меток, может быть пустым [], или содержать "СВО" и/или "МНОГОДЕТНЫЕ"

Если негативщиков не выявлено, верни: {{"negativists": [], "analysis_notes": null}}

---

ЧАТЫ ДЛЯ АНАЛИЗА:
Период: {start_date} - {end_date}

{chats_data}
"""


class ChatSummarizer:
    """Summarizer using OpenRouter API with Gemini Flash (direct HTTP for proper UTF-8)"""

    OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
    DEFAULT_MODEL = "google/gemini-2.5-flash"  # Gemini 2.5 Flash

    def __init__(self, api_key: str, model: str = None):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL

    def _format_messages(self, messages: list[dict]) -> str:
        """Format messages for the prompt"""
        formatted = []
        for msg in messages:
            date_str = msg['date'][:16].replace('T', ' ')
            sender = msg.get('sender_name', 'Unknown')
            text = msg.get('text', '')
            formatted.append(f"[{date_str}] {sender}: {text}")
        return "\n".join(formatted)

    async def _call_api(self, prompt: str) -> str:
        """Make API call to OpenRouter with proper UTF-8 encoding"""
        prompt_len = len(prompt)
        print(f"[API] Calling {self.model}, prompt length: {prompt_len} chars")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "HTTP-Referer": "https://ohranka.app",
            "X-Title": "AI-agent-ohranka"
        }

        payload = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": [
                {"role": "user", "content": prompt}
            ]
        }

        # Explicitly encode as UTF-8 bytes to avoid ascii encoding issues
        json_bytes = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        print(f"[API] Request size: {len(json_bytes)} bytes, sending...")

        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                self.OPENROUTER_URL,
                headers=headers,
                content=json_bytes
            )

            print(f"[API] Response status: {response.status_code}")

            # Get response data for better error messages
            try:
                data = response.json()
            except:
                data = {"error": response.text}

            if response.status_code != 200:
                error_msg = data.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(data))
                print(f"[API] Error: {error_msg}")
                raise Exception(f"OpenRouter API error: {error_msg}")

            print(f"[API] Success, response length: {len(data['choices'][0]['message']['content'])} chars")
            return data["choices"][0]["message"]["content"]

    async def summarize_chat(
            self,
            messages: list[dict],
            chat_name: str,
            complex_name: str,
            start_date: datetime,
            end_date: datetime,
            rules: str = None
    ) -> dict:
        """Generate a summary for a single chat"""
        rules = rules or DEFAULT_REPORT_RULES

        if not messages:
            return {
                'summary_text': f'{chat_name.upper()} — за указанный период не было ни одного сообщения, в чате полная тишина.',
            }

        formatted_messages = self._format_messages(messages)

        # Handle large message volumes by chunking if needed
        max_chars = 500000  # Gemini has larger context
        if len(formatted_messages) > max_chars:
            formatted_messages = "...[сообщения сокращены]...\n" + formatted_messages[-max_chars:]

        prompt = SUMMARIZATION_PROMPT.format(
            rules=rules,
            chat_name=chat_name,
            complex_name=complex_name,
            start_date=start_date.strftime('%d.%m.%Y %H:%M'),
            end_date=end_date.strftime('%d.%m.%Y %H:%M'),
            message_count=len(messages),
            messages=formatted_messages
        )

        try:
            summary_text = await self._call_api(prompt)
            return {'summary_text': summary_text}
        except Exception as e:
            return {
                'summary_text': f'Ошибка при генерации сводки: {str(e)}',
            }

    async def summarize_complex(
            self,
            complex_name: str,
            chats_with_messages: list[dict],
            start_date: datetime,
            end_date: datetime,
            rules: str = None
    ) -> str:
        """Generate a combined summary for all chats in a complex"""
        rules = rules or DEFAULT_REPORT_RULES

        # Build combined chats data
        chats_data_parts = []
        all_empty = True

        for chat_info in chats_with_messages:
            chat_name = chat_info['chat_name']
            messages = chat_info['messages']
            content_filter = chat_info.get('content_filter', '')

            # Build filter instruction if specified
            filter_note = ""
            if content_filter:
                filter_note = f"\n[ФИЛЬТР: Анализируй только контент, связанный с: {content_filter}. Остальные сообщения игнорируй.]\n"

            if not messages:
                chats_data_parts.append(
                    f"--- Чат: {chat_name} ---{filter_note}Сообщений нет.\n"
                )
            else:
                all_empty = False
                formatted = self._format_messages(messages)
                chats_data_parts.append(
                    f"--- Чат: {chat_name} ({len(messages)} сообщений) ---{filter_note}{formatted}\n"
                )

        if all_empty:
            return f"По ЖК «{complex_name}» за указанный период не было ни одного сообщения во всех чатах, полная тишина."

        chats_data = "\n".join(chats_data_parts)

        # Truncate if too large (Gemini has ~1M tokens context)
        max_chars = 500000
        if len(chats_data) > max_chars:
            chats_data = "...[часть сообщений сокращена]...\n" + chats_data[-max_chars:]

        prompt = BATCH_SUMMARIZATION_PROMPT.format(
            rules=rules,
            complex_name=complex_name,
            start_date=start_date.strftime('%d.%m.%Y %H:%M'),
            end_date=end_date.strftime('%d.%m.%Y %H:%M'),
            chats_data=chats_data
        )

        try:
            return await self._call_api(prompt)
        except Exception as e:
            return f"Ошибка при генерации сводки по ЖК {complex_name}: {str(e)}"

    async def analyze_negativists(
            self,
            chats_with_messages: list[dict],
            start_date: datetime,
            end_date: datetime,
            rules: str = None
    ) -> dict:
        """Analyze chats to identify negativists and provocateurs"""
        # Build combined chats data
        chats_data_parts = []
        total_messages = 0

        for chat_info in chats_with_messages:
            chat_name = chat_info['chat_name']
            messages = chat_info['messages']
            content_filter = chat_info.get('content_filter', '')

            if not messages:
                continue

            # Build filter instruction if specified
            filter_note = ""
            if content_filter:
                filter_note = f"\n[ФИЛЬТР: Анализируй только контент, связанный с: {content_filter}. Остальные сообщения игнорируй.]\n"

            total_messages += len(messages)
            formatted = self._format_messages(messages)
            chats_data_parts.append(
                f"--- Чат: {chat_name} ({len(messages)} сообщений) ---{filter_note}{formatted}\n"
            )

        if total_messages == 0:
            return {
                "negativists": [],
                "analysis_notes": "За указанный период не было сообщений в выбранных чатах."
            }

        chats_data = "\n".join(chats_data_parts)

        # Truncate if too large
        max_chars = 500000
        if len(chats_data) > max_chars:
            chats_data = "...[часть сообщений сокращена]...\n" + chats_data[-max_chars:]

        prompt = NEGATIVISTS_PROMPT.format(
            rules=rules or DEFAULT_NEGATIVISTS_RULES,
            start_date=start_date.strftime('%d.%m.%Y %H:%M'),
            end_date=end_date.strftime('%d.%m.%Y %H:%M'),
            chats_data=chats_data
        )

        try:
            response = await self._call_api(prompt)
            # Parse JSON response
            # Clean up response - remove markdown code blocks if present
            response = response.strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.startswith("```"):
                response = response[3:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()

            result = json.loads(response)
            return result
        except json.JSONDecodeError as e:
            return {
                "negativists": [],
                "analysis_notes": f"Ошибка парсинга ответа AI: {str(e)}. Ответ: {response[:500]}"
            }
        except Exception as e:
            return {
                "negativists": [],
                "analysis_notes": f"Ошибка при анализе: {str(e)}"
            }


# Global instance
_summarizer: Optional[ChatSummarizer] = None


def init_summarizer(api_key: str, model: str = None):
    global _summarizer
    _summarizer = ChatSummarizer(api_key, model)
    return _summarizer


def get_summarizer() -> ChatSummarizer:
    if _summarizer is None:
        raise RuntimeError("Summarizer not initialized")
    return _summarizer


def get_default_report_rules() -> str:
    """Return the default report rules text"""
    return DEFAULT_REPORT_RULES


def get_default_negativists_rules() -> str:
    """Return the default negativists analysis rules text"""
    return DEFAULT_NEGATIVISTS_RULES
