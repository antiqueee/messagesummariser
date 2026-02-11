from pydantic import BaseModel
from typing import Optional
from datetime import datetime


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
    start_date: datetime
    end_date: datetime


class AnalyzeNegativistsRequest(BaseModel):
    chat_ids: list[int]
    start_date: datetime
    end_date: datetime
