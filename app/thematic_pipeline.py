# -*- coding: utf-8 -*-
"""Deterministic support for prompt-driven thematic reports.

The model decides what is relevant to the user's one-off research question, but
it may only reference source records created here.  This keeps arbitrary themes
possible without giving up auditable message IDs, authors, dates and context.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .event_pipeline import author_id, message_id


def thematic_ref(source: str, chat_id: Any, message: dict) -> str:
    """Return an unambiguous reference even when messengers reuse message IDs."""
    return f"{source}:{chat_id}:{message_id(message)}"


def telegram_message_link(chat_info: dict, message: dict) -> str | None:
    """Build a private-supergroup Telegram link when the stored ID permits it."""
    if str(chat_info.get("source") or "telegram") != "telegram":
        return None
    raw_chat_id = str(
        chat_info.get("source_chat_id")
        or chat_info.get("telegram_id")
        or ""
    ).strip()
    mid = message_id(message)
    if raw_chat_id.startswith("-100") and mid:
        return f"https://t.me/c/{raw_chat_id[4:]}/{mid}"
    return None


def build_thematic_batches(
        chats_with_messages: list[dict],
        max_chars: int = 55_000,
) -> tuple[list[dict], dict[str, dict], dict]:
    """Pack every source message into bounded batches without silent truncation."""
    batches: list[dict] = []
    records: dict[str, dict] = {}
    chat_refs: dict[str, list[str]] = defaultdict(list)
    current_lines: list[str] = []
    current_refs: list[str] = []
    current_chars = 0
    active_header: str | None = None
    input_messages = 0

    def flush() -> None:
        nonlocal current_lines, current_refs, current_chars, active_header
        if current_lines:
            batches.append({"text": "\n".join(current_lines), "refs": list(current_refs)})
            current_lines = []
            current_refs = []
            current_chars = 0
            active_header = None

    for chat_position, chat_info in enumerate(chats_with_messages):
        source = str(chat_info.get("source") or "telegram")
        chat_id = chat_info.get("chat_id") or f"position-{chat_position}"
        chat_name = str(
            chat_info.get("report_chat_name")
            or chat_info.get("chat_name")
            or "Неизвестный чат"
        )
        messages = chat_info.get("messages") or []
        for position, message in enumerate(messages):
            mid = message_id(message)
            if not mid:
                continue
            ref = thematic_ref(source, chat_id, message)
            # A duplicate ref can only come from duplicate data for the same DB
            # chat. Keep one authoritative copy and do not double-count it.
            if ref in records:
                continue
            text = str(message.get("text") or "").replace("\x00", "").strip()
            date = str(message.get("date") or "")
            sender = str(message.get("sender_name") or "Unknown")
            username = str(message.get("sender_username") or "").lstrip("@").strip()
            username_part = f" [username=@{username}]" if username else ""
            reply_to = message.get("reply_to")
            reply_part = f" [reply_to={reply_to}]" if reply_to not in (None, "") else ""
            line = (
                f"[ref={ref}] [date={date}] [author_id={author_id(message, source)}]"
                f"{username_part}{reply_part} {sender}: {text}"
            )
            header = f"--- ЧАТ: {chat_name} | ИСТОЧНИК: {source} ---"
            required = len(line) + len(header) + 2
            if current_lines and current_chars + required > max_chars:
                flush()
            if active_header != header:
                current_lines.append(header)
                current_chars += len(header) + 1
                active_header = header
            current_lines.append(line)
            current_refs.append(ref)
            current_chars += len(line) + 1

            record = {
                "ref": ref,
                "chat_id": chat_id,
                "chat_name": chat_name,
                "source": source,
                "message_id": mid,
                "date": date,
                "author_id": author_id(message, source),
                "author_name": sender,
                "username": username or None,
                "text": text,
                "reply_to": str(reply_to) if reply_to not in (None, "") else None,
                "link": telegram_message_link(chat_info, message),
                "position": position,
                "selected": False,
            }
            records[ref] = record
            chat_refs[str(chat_id)].append(ref)
            input_messages += 1

    flush()
    return batches, records, {
        "input_messages": input_messages,
        "nonempty_chats": sum(1 for chat in chats_with_messages if chat.get("messages")),
        "batches": len(batches),
        "input_chars": sum(len(batch["text"]) for batch in batches),
        "chat_refs": dict(chat_refs),
    }


def collect_candidate_refs(
        payload: dict,
        allowed_refs: set[str],
) -> tuple[list[str], int]:
    """Accept only exact refs present in the corresponding source batch."""
    valid: list[str] = []
    invalid = 0
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        for raw_ref in candidate.get("refs") or []:
            ref = str(raw_ref or "").strip()
            if ref in allowed_refs:
                if ref not in valid:
                    valid.append(ref)
            elif ref:
                invalid += 1
    return valid, invalid


def expand_thematic_evidence(
        selected_refs: list[str],
        records: dict[str, dict],
        chat_refs: dict[str, list[str]],
        neighbour_radius: int = 2,
) -> list[dict]:
    """Add neighbouring messages, reply parents and direct replies as context."""
    selected = {ref for ref in selected_refs if ref in records}
    expanded = set(selected)

    refs_by_chat_and_mid: dict[tuple[str, str], str] = {}
    for ref, record in records.items():
        refs_by_chat_and_mid[(str(record["chat_id"]), str(record["message_id"]))] = ref

    for ref in list(selected):
        record = records[ref]
        chat_key = str(record["chat_id"])
        ordered = chat_refs.get(chat_key) or []
        try:
            index = ordered.index(ref)
        except ValueError:
            continue
        start = max(0, index - neighbour_radius)
        end = min(len(ordered), index + neighbour_radius + 1)
        expanded.update(ordered[start:end])
        if record.get("reply_to"):
            parent = refs_by_chat_and_mid.get((chat_key, str(record["reply_to"])))
            if parent:
                expanded.add(parent)

    selected_message_keys = {
        (str(records[ref]["chat_id"]), str(records[ref]["message_id"]))
        for ref in selected
    }
    for ref, record in records.items():
        reply_key = (str(record["chat_id"]), str(record.get("reply_to") or ""))
        if reply_key in selected_message_keys:
            expanded.add(ref)

    evidence = []
    for ref in expanded:
        item = dict(records[ref])
        item["selected"] = ref in selected
        evidence.append(item)
    evidence.sort(key=lambda item: (item.get("date") or "", str(item.get("chat_id")), item["position"]))
    return evidence


def format_thematic_evidence(evidence: list[dict]) -> str:
    lines = []
    for item in evidence:
        fields = [
            f"ref={item['ref']}",
            f"role={'match' if item.get('selected') else 'context'}",
            f"chat={item['chat_name']}",
            f"source={item['source']}",
            f"date={item['date']}",
            f"message_id={item['message_id']}",
            f"author_id={item['author_id']}",
            f"author={item['author_name']}",
        ]
        if item.get("username"):
            fields.append(f"username=@{item['username']}")
        if item.get("reply_to"):
            fields.append(f"reply_to={item['reply_to']}")
        if item.get("link"):
            fields.append(f"link={item['link']}")
        lines.append("[" + "] [".join(fields) + "] " + item["text"])
    return "\n".join(lines)
