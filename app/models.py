from pydantic import BaseModel, field_validator
from typing import Optional
from datetime import datetime
import re


def parse_flexible_datetime(value) -> datetime:
    """Parse datetime from various string formats"""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Expected string or datetime, got {type(value)}")

    value = value.strip()

    # ISO format: 2026-04-11T12:30:00, 2026-04-11T12:30, 2026-04-11 12:30:00
    for fmt in [
        '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S.%f',
    ]:
        try:
            return datetime.strptime(value.rstrip('Z'), fmt)
        except ValueError:
            continue

    # DD.MM.YYYY HH:MM or DD/MM/YYYY HH:MM
    m = re.match(r'(\d{1,2})[./\-](\d{1,2})[./\-](\d{4})[,\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?', value)
    if m:
        day, month, year, hour, minute = int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5])
        second = int(m[6]) if m[6] else 0
        return datetime(year, month, day, hour, minute, second)

    raise ValueError(f"Cannot parse datetime: {value}")


class TelegramAccount(BaseModel):
    id: int
    phone: str
    name: str
    is_authorized: bool = False
    created_at: datetime


class ResidentialComplex(BaseModel):
    """Жилой комплекс (ЖК)"""
    id: int
    name: str
    created_at: datetime


class Chat(BaseModel):
    id: int
    telegram_id: int
    account_id: int
    complex_id: Optional[int] = None
    original_title: str
    custom_name: Optional[str] = None
    is_monitored: bool = False
    created_at: datetime

    @property
    def display_name(self) -> str:
        return self.custom_name or self.original_title


class ChatMessage(BaseModel):
    id: int
    chat_id: int
    telegram_message_id: int
    sender_name: str
    sender_id: int
    text: str
    date: datetime


class SummaryRequest(BaseModel):
    complex_ids: list[int]
    start_date: datetime
    end_date: datetime


class ChatSummary(BaseModel):
    chat_id: int
    chat_name: str
    period_start: datetime
    period_end: datetime
    message_count: int
    summary_text: str
    negative_events: list[str]
    negative_actors: list[dict]
    topics: list[str]
    overall_sentiment: str


class ComplexSummary(BaseModel):
    complex_id: int
    complex_name: str
    chats: list[ChatSummary]


class FullReport(BaseModel):
    generated_at: datetime
    period_start: datetime
    period_end: datetime
    complexes: list[ComplexSummary]


# Request/Response models for API
class AccountCreateRequest(BaseModel):
    phone: str
    name: str


class AccountVerifyRequest(BaseModel):
    account_id: int
    code: str
    password: Optional[str] = None


class ComplexCreateRequest(BaseModel):
    name: str


class ChatUpdateRequest(BaseModel):
    custom_name: Optional[str] = None
    complex_id: Optional[int] = None
    is_monitored: Optional[bool] = None
    content_filter: Optional[str] = None
    selected_topics: Optional[str] = None  # JSON string of topic IDs


class GenerateReportRequest(BaseModel):
    complex_ids: list[int]
    start_date: str
    end_date: str

    @field_validator('start_date', 'end_date')
    @classmethod
    def validate_dates(cls, v):
        parse_flexible_datetime(v)  # Validate but keep as string
        return v

    def get_start_date(self) -> datetime:
        return parse_flexible_datetime(self.start_date)

    def get_end_date(self) -> datetime:
        return parse_flexible_datetime(self.end_date)


class AnalyzeNegativistsRequest(BaseModel):
    chat_ids: list[int]
    start_date: str
    end_date: str

    @field_validator('start_date', 'end_date')
    @classmethod
    def validate_dates(cls, v):
        parse_flexible_datetime(v)
        return v

    def get_start_date(self) -> datetime:
        return parse_flexible_datetime(self.start_date)

    def get_end_date(self) -> datetime:
        return parse_flexible_datetime(self.end_date)


# Max messenger models
class MaxAccountCreateRequest(BaseModel):
    phone: str
    name: str


class MaxChatAddRequest(BaseModel):
    max_chat_id: int
    title: str
