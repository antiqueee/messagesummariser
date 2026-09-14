# -*- coding: utf-8 -*-
"""Deterministic helpers for the evidence-first daily report pipeline.

Models may suggest facts and classifications, but this module is the trust
boundary: only message IDs present in the source input become evidence, author
counts are derived from source messages, and action stages are capped by
observable language in those messages.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable


ACTION_STAGES = (
    "none",
    "suggestion",
    "supported",
    "collecting_contacts",
    "scheduled",
    "occurred",
)
ACTION_STAGE_RANK = {stage: index for index, stage in enumerate(ACTION_STAGES)}
EVENT_STATES = {"new", "ongoing", "escalated", "de_escalated", "resolved"}
SEVERITIES = {"low", "medium", "high", "critical"}
RISK_FLAGS = {
    "conflict",
    "threat",
    "complaint",
    "petition",
    "contact_collection",
    "organized_action",
    "offline_action",
    "media",
    "authorities",
    "legal",
    "safety",
}

RISK_TEXT_RE = re.compile(
    r"(?:угрож|расправ|подж|сломаем|взлом|перекро|митинг|пикет|собер[её]мся|"
    r"встреча|акци[яю]|коллективн|подпис[ьи]|петици|сбор\s+(?:контакт|телефон|"
    r"данн)|пришлите\s+(?:номер|телефон|контакт)|прокуратур|следственн(?:ый|ого)"
    r"|депутат|суд|юрист|жалоб|претензи|сми|телеканал|журналист|президент|"
    r"мчс|полици|опасн|авари|пожар|затоп|обруш|конфликт)",
    re.IGNORECASE,
)
SUGGESTION_RE = re.compile(
    r"(?:давайте|предлагаю|надо|нужно|может)\b.{0,100}(?:собра|встрет|напи|"
    r"пода|отправ|обрат|позвон|пойти|поехать|вызва)",
    re.IGNORECASE | re.DOTALL,
)
ACTION_CONTEXT_RE = re.compile(
    r"(?:собра|встрет|жалоб|петици|подпис|прокуратур|суд|сми|администрац|"
    r"застройщик|управляющ|ук\b|мчс|полици)",
    re.IGNORECASE,
)
SUPPORT_RE = re.compile(
    r"(?:^|\s)(?:я\s+за|поддерживаю|согласен|согласна|иду|приеду|буду|"
    r"участвую|присоединяюсь|\+1)(?:\s|$|[.!?,])",
    re.IGNORECASE,
)
CONTACT_RE = re.compile(
    r"(?:собира\w*\s+(?:контакт|телефон|номер|данн)|пришлите\s+(?:контакт|"
    r"телефон|номер|фио)|список\s+(?:желающих|участников)|записывайтесь|"
    r"подписываем|сбор\s+подпис|опрос\s+кто)",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"(?:\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|\b(?:сегодня|завтра|"
    r"послезавтра|понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|"
    r"воскресень[ея])\b)",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"(?:\b[012]?\d[:.]\d{2}\b|\bв\s+[012]?\d\s*(?:час|ч\b))", re.IGNORECASE)
PLACE_RE = re.compile(
    r"(?:у|возле|около|напротив|рядом\s+с|в)\s+(?:офис|кпп|штаб|дом|подъезд|"
    r"ворот|стройк|площадк|администрац|прокуратур|ук\b|мфц|парковк)",
    re.IGNORECASE,
)
OCCURRED_RE = re.compile(
    r"(?:уже\s+)?(?:собрались|встретились|провели|подали|отправили|передали|"
    r"обратились|сходили|съездили|вызвали)",
    re.IGNORECASE,
)


def parse_json_response(raw: str) -> Any:
    """Parse JSON even when a provider wraps it in a Markdown fence."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def author_id(message: dict, source: str) -> str:
    sender_id = message.get("sender_id")
    username = str(message.get("sender_username") or "").lstrip("@").strip()
    name = str(message.get("sender_name") or "Unknown").strip()
    if sender_id not in (None, "", 0, "0"):
        return f"{source}:{sender_id}"
    if username:
        return f"{source}:username:{username.casefold()}"
    return f"{source}:name:{name.casefold()}"


def message_id(message: dict) -> str:
    value = message.get("message_id")
    if value in (None, ""):
        value = message.get("id")
    return str(value or "").strip()


def format_messages_with_ids(messages: list[dict], source: str) -> str:
    lines = []
    for message in messages:
        mid = message_id(message)
        date = str(message.get("date") or "")[:19].replace("T", " ")
        sender = str(message.get("sender_name") or "Unknown")
        text = str(message.get("text") or "").replace("\x00", "").strip()
        lines.append(
            f"[date={date}] [message_id={mid}] "
            f"[author_id={author_id(message, source)}] {sender}: {text}"
        )
    return "\n".join(lines)


def _message_index(messages: Iterable[dict]) -> dict[str, dict]:
    return {message_id(message): message for message in messages if message_id(message)}


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _actual_quote(message: dict, limit: int = 360) -> str:
    text = _clean_text(message.get("text"), limit + 1)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def derive_action_stage(messages: list[dict], source: str) -> str:
    """Derive the highest defensible collective-action stage from source text."""
    if not messages:
        return "none"
    texts = [str(message.get("text") or "") for message in messages]
    joined = "\n".join(texts)
    suggestion_authors = {
        author_id(message, source)
        for message in messages
        if SUGGESTION_RE.search(str(message.get("text") or ""))
    }
    support_authors = {
        author_id(message, source)
        for message in messages
        if SUPPORT_RE.search(str(message.get("text") or ""))
    }

    if OCCURRED_RE.search(joined) and ACTION_CONTEXT_RE.search(joined):
        return "occurred"
    if (
        ACTION_CONTEXT_RE.search(joined)
        and DATE_RE.search(joined)
        and TIME_RE.search(joined)
        and PLACE_RE.search(joined)
    ):
        return "scheduled"
    if CONTACT_RE.search(joined):
        return "collecting_contacts"
    if suggestion_authors and any(author not in suggestion_authors for author in support_authors):
        return "supported"
    if SUGGESTION_RE.search(joined):
        return "suggestion"
    return "none"


def _event_fingerprint(event: dict) -> str:
    basis = "|".join(
        (
            str(event.get("event_type") or "other").casefold(),
            str(event.get("target") or "").casefold(),
            str(event.get("location") or "").casefold(),
            re.sub(r"[^a-zа-яё0-9]+", " ", str(event.get("title") or "").casefold()).strip(),
        )
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def validate_event(raw_event: dict, messages: list[dict], chat_name: str, source: str) -> dict | None:
    """Return a source-grounded event or None when it has no valid evidence."""
    if not isinstance(raw_event, dict):
        return None
    index = _message_index(messages)
    requested_ids: list[str] = []
    for item in raw_event.get("evidence") or []:
        if isinstance(item, dict):
            mid = str(item.get("message_id") or "").strip()
        else:
            mid = str(item or "").strip()
        if mid and mid not in requested_ids:
            requested_ids.append(mid)
    for mid in raw_event.get("related_message_ids") or []:
        mid = str(mid or "").strip()
        if mid and mid not in requested_ids:
            requested_ids.append(mid)

    valid_messages = [index[mid] for mid in requested_ids if mid in index]
    if not valid_messages:
        return None

    evidence = []
    for message in valid_messages:
        evidence.append(
            {
                "chat_name": chat_name,
                "message_id": message_id(message),
                "author_id": author_id(message, source),
                "date": str(message.get("date") or ""),
                "quote": _actual_quote(message),
            }
        )

    participant_ids = sorted({item["author_id"] for item in evidence})
    model_stage = str(raw_event.get("action_stage") or "none")
    if model_stage not in ACTION_STAGE_RANK:
        model_stage = "none"
    observed_stage = derive_action_stage(valid_messages, source)
    # The code-observed stage is authoritative. It prevents a model from turning
    # one suggestion into an allegedly scheduled collective action.
    action_stage = observed_stage

    severity = str(raw_event.get("severity") or "low")
    if severity not in SEVERITIES:
        severity = "low"
    state = str(raw_event.get("state") or "new")
    if state not in EVENT_STATES:
        state = "new"
    try:
        confidence = min(1.0, max(0.0, float(raw_event.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5

    flags = {
        str(flag).strip()
        for flag in (raw_event.get("risk_flags") or [])
        if str(flag).strip() in RISK_FLAGS
    }
    source_text = "\n".join(str(message.get("text") or "") for message in valid_messages)
    if RISK_TEXT_RE.search(source_text):
        if re.search(r"угрож|расправ|подж|сломаем|взлом", source_text, re.IGNORECASE):
            flags.add("threat")
        if re.search(r"жалоб|прокуратур|суд|претензи|обрати", source_text, re.IGNORECASE):
            flags.add("complaint")
    if action_stage in {"collecting_contacts", "scheduled", "occurred"}:
        flags.add("organized_action")
    if action_stage == "collecting_contacts":
        flags.add("contact_collection")
    if action_stage in {"scheduled", "occurred"}:
        flags.add("offline_action")

    event = {
        "memory_id": raw_event.get("memory_id"),
        "event_type": _clean_text(raw_event.get("event_type") or "other", 80),
        "title": _clean_text(raw_event.get("title") or "Событие", 240),
        "summary": _clean_text(raw_event.get("summary"), 1400),
        "details": _clean_text(raw_event.get("details"), 1800),
        "target": _clean_text(raw_event.get("target"), 120),
        "location": _clean_text(raw_event.get("location"), 180),
        "severity": severity,
        "confidence": confidence,
        "action_stage": action_stage,
        "model_action_stage": model_stage,
        "state": state,
        "risk_flags": sorted(flags),
        "chat_names": [chat_name],
        "participant_ids": participant_ids,
        "participant_count": len(participant_ids),
        "message_count": len(evidence),
        "evidence": evidence,
        "verification_status": "not_required",
    }
    # Treat the model key as a semantic hint, never as a database identifier.
    # Hashing it together with grounded fields prevents a generic or malicious
    # key from overwriting a different event in the same complex.
    model_event_key = _clean_text(raw_event.get("event_key"), 160)
    event["model_event_key"] = model_event_key
    event["event_key"] = _event_fingerprint({
        **event,
        "title": model_event_key or event["title"],
    })
    return event


def ground_memory_reference(event: dict, memory_events: list[dict]) -> dict:
    """Keep a model-selected memory link only when the event is semantically compatible."""
    grounded = dict(event)
    model_memory_id = grounded.get("memory_id")
    grounded["model_memory_id"] = model_memory_id
    try:
        normalized_id = int(model_memory_id)
    except (TypeError, ValueError):
        grounded["memory_id"] = None
        return grounded

    candidate = next(
        (
            item for item in memory_events
            if int(item.get("id") or item.get("memory_id") or 0) == normalized_id
        ),
        None,
    )
    if candidate is None:
        grounded["memory_id"] = None
        return grounded

    def semantic_terms(item: dict) -> set[str]:
        text = " ".join(
            str(item.get(field) or "")
            for field in ("title", "summary", "target", "location")
        ).casefold()
        words = re.findall(r"[a-zа-яё0-9]+(?:[.,][0-9]+)?", text)
        generic = {
            "жилой", "комплекс", "жители", "дольщики", "участники",
            "обсуждение", "сообщение", "событие", "пригород", "лесное",
            "корпус", "корпуса", "проблема", "вопрос",
        }
        return {
            word if word[0].isdigit() else word[:7]
            for word in words
            if (len(word) >= 4 or word[0].isdigit()) and word not in generic
        }

    shared_terms = semantic_terms(grounded) & semantic_terms(candidate)
    same_type = (
        str(grounded.get("event_type") or "").casefold()
        == str(candidate.get("event_type") or "").casefold()
    )
    if len(shared_terms) < 3 and not (same_type and len(shared_terms) >= 2):
        grounded["memory_id"] = None
        return grounded

    grounded["memory_id"] = normalized_id
    return grounded


def chat_needs_verification(messages: list[dict], events: list[dict]) -> bool:
    """Select Luna only for risky source text/events or low-confidence extraction."""
    if any(float(event.get("confidence") or 0) < 0.75 for event in events):
        return True
    if any(event.get("risk_flags") for event in events):
        return True
    if any(event.get("severity") in {"high", "critical"} for event in events):
        return True
    return bool(RISK_TEXT_RE.search("\n".join(str(message.get("text") or "") for message in messages)))


def _event_matches(left: dict, right: dict) -> bool:
    left_ids = {
        (item.get("chat_name"), item.get("message_id"))
        for item in left.get("evidence") or []
    }
    right_ids = {
        (item.get("chat_name"), item.get("message_id"))
        for item in right.get("evidence") or []
    }
    if left_ids & right_ids:
        return True
    if left.get("event_key") and left.get("event_key") == right.get("event_key"):
        return True
    return _event_fingerprint(left) == _event_fingerprint(right)


def _combine_events(base: dict, candidate: dict) -> dict:
    combined = dict(base)
    combined_evidence = {
        (item.get("chat_name"), item.get("message_id")): item
        for item in (base.get("evidence") or []) + (candidate.get("evidence") or [])
    }
    combined["evidence"] = list(combined_evidence.values())
    combined["participant_ids"] = sorted({item["author_id"] for item in combined["evidence"]})
    combined["participant_count"] = len(combined["participant_ids"])
    combined["message_count"] = len(combined["evidence"])
    combined["chat_names"] = list(dict.fromkeys(
        (base.get("chat_names") or []) + (candidate.get("chat_names") or [])
    ))
    combined["confidence"] = max(float(base.get("confidence") or 0), float(candidate.get("confidence") or 0))
    combined["risk_flags"] = sorted(set(base.get("risk_flags") or []) | set(candidate.get("risk_flags") or []))
    if ACTION_STAGE_RANK.get(candidate.get("action_stage"), 0) > ACTION_STAGE_RANK.get(combined.get("action_stage"), 0):
        combined["action_stage"] = candidate["action_stage"]
    severity_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    if severity_rank.get(candidate.get("severity"), 0) > severity_rank.get(combined.get("severity"), 0):
        combined["severity"] = candidate["severity"]
    return combined


def merge_independent_review(primary: list[dict], reviewed: list[dict]) -> list[dict]:
    """Merge two independently extracted, already validated event lists."""
    result: list[dict] = []
    used_reviewed: set[int] = set()
    for event in primary:
        match_index = next(
            (index for index, candidate in enumerate(reviewed) if index not in used_reviewed and _event_matches(event, candidate)),
            None,
        )
        if match_index is None:
            merged = dict(event)
            merged["verification_status"] = "primary_only"
            result.append(merged)
            continue
        candidate = reviewed[match_index]
        used_reviewed.add(match_index)
        combined = _combine_events(event, candidate)
        combined["verification_status"] = "confirmed"
        result.append(combined)

    for index, candidate in enumerate(reviewed):
        if index in used_reviewed:
            continue
        recovered = dict(candidate)
        recovered["verification_status"] = "recovered_by_verifier"
        result.append(recovered)
    return result


def merge_complex_events(events: list[dict]) -> list[dict]:
    """Merge the same issue observed in several chats without merging lookalikes."""
    merged: list[dict] = []
    for event in events:
        match_index = None
        for index, existing in enumerate(merged):
            same_memory = (
                event.get("memory_id") is not None
                and event.get("memory_id") == existing.get("memory_id")
            )
            same_event_key = (
                bool(event.get("event_key"))
                and event.get("event_key") == existing.get("event_key")
            )
            same_fingerprint = _event_fingerprint(event) == _event_fingerprint(existing)
            if same_memory or same_event_key or same_fingerprint:
                match_index = index
                break
        if match_index is None:
            merged.append(dict(event))
        else:
            merged[match_index] = _combine_events(merged[match_index], event)
    return merged


def sheet_rows_from_events(events: list[dict], chat_statuses: list[dict]) -> list[dict]:
    """Build Google Sheets rows directly from event cards, without another AI call."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        for chat_name in event.get("chat_names") or ["Неизвестный чат"]:
            grouped[chat_name].append(event)

    rows = []
    for chat_name, chat_events in grouped.items():
        alarming = []
        details = []
        risks = []
        for event in chat_events:
            alarming.append(event.get("summary") or event.get("title") or "")
            if event.get("details"):
                details.append(event["details"])
            if event.get("risk_flags") or event.get("action_stage") != "none":
                risk_bits = []
                if event.get("action_stage") != "none":
                    risk_bits.append(f"стадия действий: {event['action_stage']}")
                if event.get("risk_flags"):
                    risk_bits.append("риски: " + ", ".join(event["risk_flags"]))
                risks.append((event.get("title") or "Событие") + " — " + "; ".join(risk_bits))
        rows.append(
            {
                "chat": chat_name.upper(),
                "alarming_topics": " ".join(filter(None, alarming)),
                "additional_info": " ".join(filter(None, details)),
                "risks_reaction": " ".join(filter(None, risks)),
                "background_topics": "",
            }
        )

    # Preserve explicit silence only when a chat truly contained no messages.
    for status in chat_statuses:
        if status.get("message_count", 0) == 0 and status.get("chat_name") not in grouped:
            rows.append(
                {
                    "chat": str(status.get("chat_name") or "").upper(),
                    "alarming_topics": "",
                    "additional_info": "",
                    "risks_reaction": "",
                    "background_topics": "За этот день нет сообщений",
                }
            )
    return rows


def fallback_report(complex_name: str, events: list[dict]) -> str:
    """Produce a usable report if the final prose model is temporarily unavailable."""
    if not events:
        return f"По ЖК «{complex_name}» существенных событий за день не зафиксировано."
    lines = []
    for event in events:
        sentence = event.get("summary") or event.get("title") or "Зафиксировано событие."
        if event.get("details") and event.get("severity") in {"high", "critical"}:
            sentence = f"{sentence} {event['details']}"
        lines.append(sentence.strip())
    return "\n".join(lines)


def memory_payload(events: list[dict], observed_at: datetime) -> list[dict]:
    """Attach the observation timestamp before database persistence."""
    timestamp = observed_at.isoformat()
    return [{**event, "observed_at": timestamp} for event in events]
