import json
from datetime import datetime
from typing import Optional

try:
    import anthropic
    HAS_ANTHROPIC = True
except Exception:
    HAS_ANTHROPIC = False


SUMMARIZATION_PROMPT = """Ты - аналитик, специализирующийся на мониторинге настроений в чатах строительных объектов (жилых комплексов).

Проанализируй переписку из чата и создай ПОДРОБНУЮ сводку со следующей структурой:

## Общее настроение чата
Оцени общий тон обсуждений: позитивный, нейтральный, негативный или смешанный.

## Негативные события и проблемы
Перечисли ВСЕ негативные события, жалобы и проблемы, которые обсуждались. Для каждого события укажи:
- Суть проблемы
- Кто поднял вопрос (имя или никнейм)
- Реакция других участников
- Была ли проблема решена или нет

## Персонажи, распространяющие негатив
Определи людей, которые:
- Систематически жалуются
- Разжигают конфликты
- Распространяют негативные настроения
- Провоцируют других участников

Для каждого такого человека укажи:
- Имя/никнейм
- Характер его негативных высказываний
- Примеры (цитаты)
- Частота негативных сообщений

## Основные темы обсуждений
Перечисли все темы, которые обсуждались в чате, с краткой характеристикой.

## Конфликты и споры
Опиши любые конфликтные ситуации, споры между участниками.

## Важные объявления и новости
Если были официальные объявления или важная информация - укажи её.

## Рекомендации
Кратко укажи, на что стоит обратить внимание руководству/администрации.

---

ВАЖНО:
- Будь максимально подробным и конкретным
- Указывай реальные имена участников из переписки
- Приводи цитаты для подтверждения выводов
- Если негатива нет - так и напиши
- Пиши на русском языке

Информация о чате:
- Название чата: {chat_name}
- ЖК: {complex_name}
- Период: {start_date} - {end_date}
- Количество сообщений: {message_count}

Переписка:
{messages}
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
            end_date: datetime
    ) -> dict:
        """Generate a summary for a single chat"""

        if not messages:
            return {
                'summary_text': 'Нет сообщений за указанный период.',
                'negative_events': [],
                'negative_actors': [],
                'topics': [],
                'overall_sentiment': 'нет данных'
            }

        formatted_messages = self._format_messages(messages)

        # Handle large message volumes by chunking if needed
        max_chars = 150000  # Leave room for prompt and response
        if len(formatted_messages) > max_chars:
            # Truncate but keep recent messages
            formatted_messages = "...[сообщения сокращены]...\n" + formatted_messages[-max_chars:]

        prompt = SUMMARIZATION_PROMPT.format(
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
                max_tokens=4096,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )

            summary_text = response.content[0].text

            # Parse the summary to extract structured data
            parsed = self._parse_summary(summary_text)

            return {
                'summary_text': summary_text,
                'negative_events': parsed.get('negative_events', []),
                'negative_actors': parsed.get('negative_actors', []),
                'topics': parsed.get('topics', []),
                'overall_sentiment': parsed.get('overall_sentiment', 'не определено')
            }

        except Exception as e:
            return {
                'summary_text': f'Ошибка при генерации сводки: {str(e)}',
                'negative_events': [],
                'negative_actors': [],
                'topics': [],
                'overall_sentiment': 'ошибка'
            }

    def _parse_summary(self, summary_text: str) -> dict:
        """Extract structured data from the summary text"""
        result = {
            'negative_events': [],
            'negative_actors': [],
            'topics': [],
            'overall_sentiment': 'не определено'
        }

        lines = summary_text.split('\n')
        current_section = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Detect sections
            if '## Общее настроение' in line or '## общее настроение' in line.lower():
                current_section = 'sentiment'
            elif '## Негативные события' in line or '## негативные события' in line.lower():
                current_section = 'negative_events'
            elif '## Персонажи' in line or '## персонажи' in line.lower():
                current_section = 'negative_actors'
            elif '## Основные темы' in line or '## основные темы' in line.lower():
                current_section = 'topics'
            elif line.startswith('## '):
                current_section = 'other'
            elif current_section == 'sentiment' and not line.startswith('#'):
                if 'негативн' in line.lower():
                    result['overall_sentiment'] = 'негативный'
                elif 'позитивн' in line.lower():
                    result['overall_sentiment'] = 'позитивный'
                elif 'нейтральн' in line.lower():
                    result['overall_sentiment'] = 'нейтральный'
                elif 'смешан' in line.lower():
                    result['overall_sentiment'] = 'смешанный'
            elif current_section == 'negative_events' and line.startswith('- '):
                result['negative_events'].append(line[2:])
            elif current_section == 'topics' and line.startswith('- '):
                result['topics'].append(line[2:])
            elif current_section == 'negative_actors' and line.startswith('- '):
                result['negative_actors'].append({'description': line[2:]})

        return result


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
