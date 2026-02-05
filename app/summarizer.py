import json
from datetime import datetime
from typing import Optional

try:
    import anthropic
    HAS_ANTHROPIC = True
except Exception:
    HAS_ANTHROPIC = False


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
Название чата выделяется жирным, далее ставится длинное тире и идет основной текст отчета (один человеческий абзац).
Если информации по чату очень много (особенно по офлайн-рискам), абзац может быть объемным, но он НИКОГДА не должен дробиться на списки.

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

Формат: для каждого чата — жирное название, длинное тире, затем связный человеческий абзац.

ЖК: {complex_name}
Период: {start_date} - {end_date}

{chats_data}
"""


class ChatSummarizer:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not HAS_ANTHROPIC:
                raise RuntimeError("Библиотека anthropic не установлена или несовместима. Выполните: pip install --upgrade anthropic")
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def _format_messages(self, messages: list[dict]) -> str:
        """Format messages for the prompt"""
        formatted = []
        for msg in messages:
            date_str = msg['date'][:16].replace('T', ' ')
            formatted.append(f"[{date_str}] {msg['sender_name']}: {msg['text']}")
        return "\n".join(formatted)

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
                'summary_text': f'**{chat_name}** — за указанный период не было ни одного сообщения, в чате полная тишина.',
            }

        formatted_messages = self._format_messages(messages)

        # Handle large message volumes by chunking if needed
        max_chars = 150000  # Leave room for prompt and response
        if len(formatted_messages) > max_chars:
            # Truncate but keep recent messages
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
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8192,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )

            summary_text = response.content[0].text
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

            if not messages:
                chats_data_parts.append(
                    f"--- Чат: {chat_name} ---\nСообщений нет.\n"
                )
            else:
                all_empty = False
                formatted = self._format_messages(messages)
                chats_data_parts.append(
                    f"--- Чат: {chat_name} ({len(messages)} сообщений) ---\n{formatted}\n"
                )

        if all_empty:
            return f"По ЖК «{complex_name}» за указанный период не было ни одного сообщения во всех чатах, полная тишина."

        chats_data = "\n".join(chats_data_parts)

        # Truncate if too large
        max_chars = 150000
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
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8192,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            return response.content[0].text
        except Exception as e:
            return f"Ошибка при генерации сводки по ЖК {complex_name}: {str(e)}"

# Global instance
_summarizer: Optional[ChatSummarizer] = None


def init_summarizer(api_key: str):
    global _summarizer
    _summarizer = ChatSummarizer(api_key)
    return _summarizer


def get_summarizer() -> ChatSummarizer:
    if _summarizer is None:
        raise RuntimeError("Summarizer not initialized")
    return _summarizer


def get_default_report_rules() -> str:
    """Return the default report rules text"""
    return DEFAULT_REPORT_RULES
