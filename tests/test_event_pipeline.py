import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app import database
from app.event_pipeline import (
    chat_needs_verification,
    derive_action_stage,
    ground_memory_reference,
    merge_complex_events,
    merge_independent_review,
    sheet_rows_from_events,
    validate_event,
)
from app.summarizer import ChatSummarizer


def message(mid, sender_id, text, date="2026-09-14T10:00:00"):
    return {
        "message_id": mid,
        "sender_id": sender_id,
        "sender_name": f"Житель {sender_id}",
        "date": date,
        "text": text,
    }


def raw_event(ids, **overrides):
    result = {
        "memory_id": None,
        "event_key": "lift-breakdown",
        "event_type": "management_problem",
        "title": "Не работает лифт",
        "summary": "Жители сообщили о неработающем лифте.",
        "details": "Обращение адресовано УК.",
        "target": "УК",
        "location": "корпус 1",
        "severity": "medium",
        "confidence": 0.9,
        "action_stage": "none",
        "state": "new",
        "risk_flags": [],
        "related_message_ids": [str(mid) for mid in ids],
        "evidence": [{"message_id": str(mid), "quote": "model quote"} for mid in ids],
    }
    result.update(overrides)
    return result


class EvidenceValidationTests(unittest.TestCase):
    def test_invalid_message_ids_are_removed_and_cannot_create_event(self):
        messages = [message(10, 1, "Лифт снова не работает, написала в УК")]
        event = validate_event(raw_event([10, 999]), messages, "Корпус 1", "telegram")

        self.assertIsNotNone(event)
        self.assertEqual([item["message_id"] for item in event["evidence"]], ["10"])
        self.assertEqual(event["evidence"][0]["quote"], messages[0]["text"])
        self.assertEqual(event["participant_count"], 1)

        self.assertIsNone(
            validate_event(raw_event([999]), messages, "Корпус 1", "telegram")
        )

    def test_action_stage_is_derived_from_source_not_model_claim(self):
        suggestion = [message(1, 1, "Давайте напишем жалобу в УК")]
        self.assertEqual(derive_action_stage(suggestion, "telegram"), "suggestion")

        supported = suggestion + [message(2, 2, "Я за, поддерживаю")]
        self.assertEqual(derive_action_stage(supported, "telegram"), "supported")

        scheduled = supported + [
            message(3, 3, "Встречаемся завтра в 12:00 у офиса продаж")
        ]
        self.assertEqual(derive_action_stage(scheduled, "telegram"), "scheduled")

        event = validate_event(
            raw_event([1], action_stage="scheduled"),
            suggestion,
            "Общий чат",
            "telegram",
        )
        self.assertEqual(event["action_stage"], "suggestion")
        self.assertEqual(event["model_action_stage"], "scheduled")

    def test_support_requires_a_different_author_and_schedule_requires_action_context(self):
        same_author = [
            message(1, 1, "Давайте напишем жалобу в УК"),
            message(2, 1, "Я за, поддерживаю"),
            message(3, 2, "Лифт опять шумит"),
        ]
        self.assertEqual(derive_action_stage(same_author, "telegram"), "suggestion")

        unrelated_time_and_place = [
            message(4, 1, "Завтра в 12:00 буду у парковки"),
        ]
        self.assertEqual(derive_action_stage(unrelated_time_and_place, "telegram"), "none")

    def test_model_event_key_cannot_be_used_as_raw_database_key(self):
        event = validate_event(
            raw_event([1], event_key="generic"),
            [message(1, 1, "Лифт не работает")],
            "Чат",
            "telegram",
        )
        self.assertEqual(event["model_event_key"], "generic")
        self.assertNotEqual(event["event_key"], "generic")

    def test_risk_words_trigger_review_even_when_primary_found_nothing(self):
        messages = [message(1, 1, "Надо подавать коллективную жалобу в прокуратуру")]
        self.assertTrue(chat_needs_verification(messages, []))
        self.assertFalse(chat_needs_verification([message(2, 1, "Продам стол")], []))

    def test_independent_review_can_recover_omitted_event(self):
        messages = [message(1, 1, "Написали жалобу в УК")]
        reviewed = validate_event(raw_event([1]), messages, "Чат", "telegram")
        merged = merge_independent_review([], [reviewed])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["verification_status"], "recovered_by_verifier")

    def test_memory_link_requires_semantic_compatibility(self):
        memory = [{
            "id": 2,
            "event_type": "safety",
            "title": "Свободный доступ посторонних в корпус 14.2",
            "summary": "Открытые двери позволили посторонним войти в квартиры.",
            "target": "застройщик",
            "location": "корпус 14.2",
        }]
        meeting = validate_event(
            raw_event(
                [1],
                memory_id=2,
                event_type="organized_action",
                title="Собрание с застройщиком 17 сентября",
                summary="Жители готовят вопросы к очной встрече.",
                location="корпус 18",
            ),
            [message(1, 1, "Собрание с застройщиком 17 сентября")],
            "Корпус 18",
            "telegram",
        )
        access = validate_event(
            raw_event(
                [2],
                memory_id=2,
                event_type="safety",
                title="Посторонние снова получили свободный доступ в корпус 14.2",
                summary="Открытые двери позволяют войти в квартиры.",
                location="корпус 14.2",
            ),
            [message(2, 2, "Двери снова открыты, посторонние заходят в 14.2")],
            "Корпус 14.2",
            "telegram",
        )

        self.assertIsNone(ground_memory_reference(meeting, memory)["memory_id"])
        self.assertEqual(ground_memory_reference(access, memory)["memory_id"], 2)

    def test_same_issue_across_chats_is_merged_with_composite_evidence(self):
        first = validate_event(
            raw_event([1]),
            [message(1, 1, "Лифт не работает")],
            "Корпус 1",
            "telegram",
        )
        second = validate_event(
            raw_event([1]),
            [message(1, 2, "Лифт всё ещё не работает")],
            "Общий чат",
            "telegram",
        )
        merged = merge_complex_events([first, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["message_count"], 2)
        self.assertEqual(merged[0]["participant_count"], 2)
        self.assertEqual(merged[0]["chat_names"], ["Корпус 1", "Общий чат"])

    def test_sheet_rows_come_from_events_and_true_silence(self):
        messages = [message(1, 1, "Лифт не работает")]
        event = validate_event(raw_event([1]), messages, "Корпус 1", "telegram")
        rows = sheet_rows_from_events(
            [event],
            [
                {"chat_name": "Корпус 1", "message_count": 1},
                {"chat_name": "Корпус 2", "message_count": 0},
                {"chat_name": "Бытовой", "message_count": 12},
            ],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["chat"], "КОРПУС 1")
        self.assertEqual(rows[1]["background_topics"], "За этот день нет сообщений")


class SelectiveVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_calm_chat_does_not_call_luna(self):
        summarizer = ChatSummarizer("test", "google/gemini-3.8-flash")
        calls = []

        async def fake_call(
                prompt, model_override=None, model_fallbacks=None,
                response_schema=None, purpose="unknown",
        ):
            calls.append((model_override, purpose))
            return '{"analysis_confidence":0.98,"events":[]}'

        summarizer._call_api = fake_call
        result = await summarizer.analyze_chat_events(
            messages=[message(1, 1, "Продам детский стул")],
            chat_name="Соседи",
            complex_name="Тестовый ЖК",
            source="telegram",
            start_date=datetime(2026, 9, 14),
            end_date=datetime(2026, 9, 14, 23, 59),
            memory_events=[],
        )
        self.assertFalse(result["verification_used"])
        self.assertEqual(calls, [(None, "daily_event_extraction")])

    async def test_risky_source_calls_luna_independently(self):
        summarizer = ChatSummarizer("test", "google/gemini-3.8-flash")
        calls = []

        async def fake_call(
                prompt, model_override=None, model_fallbacks=None,
                response_schema=None, purpose="unknown",
        ):
            calls.append((model_override, model_fallbacks, purpose))
            return '{"analysis_confidence":0.95,"events":[]}'

        summarizer._call_api = fake_call
        result = await summarizer.analyze_chat_events(
            messages=[message(1, 1, "Будем подавать коллективную жалобу в прокуратуру")],
            chat_name="Соседи",
            complex_name="Тестовый ЖК",
            source="telegram",
            start_date=datetime(2026, 9, 14),
            end_date=datetime(2026, 9, 14, 23, 59),
            memory_events=[],
        )
        self.assertTrue(result["verification_used"])
        self.assertEqual(
            calls[1],
            (
                "openai/gpt-5.6-luna-pro",
                ["qwen/qwen3.8-flash"],
                "risk_verification",
            ),
        )

    async def test_report_rules_are_applied_during_extraction(self):
        summarizer = ChatSummarizer("test", "google/gemini-3.8-flash")
        prompts = []

        async def fake_call(
                prompt, model_override=None, model_fallbacks=None,
                response_schema=None, purpose="unknown",
        ):
            prompts.append(prompt)
            return '{"analysis_confidence":0.98,"events":[]}'

        summarizer._call_api = fake_call
        await summarizer.analyze_chat_events(
            messages=[message(1, 1, "Обычное сообщение")],
            chat_name="Соседи",
            complex_name="Тестовый ЖК",
            source="telegram",
            start_date=datetime(2026, 9, 14),
            end_date=datetime(2026, 9, 14, 23, 59),
            memory_events=[],
            rules="ОСОБОЕ ПРАВИЛО ЗАКАЗЧИКА",
        )
        self.assertIn("ОСОБОЕ ПРАВИЛО ЗАКАЗЧИКА", prompts[0])

    async def test_both_analyzers_failing_never_becomes_a_no_events_report(self):
        summarizer = ChatSummarizer("test", "google/gemini-3.8-flash")

        async def failed_call(
                prompt, model_override=None, model_fallbacks=None,
                response_schema=None, purpose="unknown",
        ):
            raise RuntimeError("provider unavailable")

        summarizer._call_api = failed_call
        with self.assertRaisesRegex(RuntimeError, "Не удалось надёжно проанализировать"):
            await summarizer.build_complex_report(
                complex_name="Тестовый ЖК",
                chats_with_messages=[{
                    "chat_name": "Соседи",
                    "source": "telegram",
                    "messages": [message(1, 1, "Сообщение")],
                }],
                start_date=datetime(2026, 9, 14),
                end_date=datetime(2026, 9, 14, 23, 59),
            )


class VerifierFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_luna_response_falls_back_to_qwen(self):
        summarizer = ChatSummarizer("test", "google/gemini-3.8-flash")
        requested_models = []

        class FakeResponse:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, headers, content):
                payload = json.loads(content)
                requested_models.append(payload["model"])
                if payload["model"] == "openai/gpt-5.6-luna-pro":
                    return FakeResponse({
                        "choices": [{
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": None},
                        }],
                    })
                return FakeResponse({
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "{\"events\":[]}"},
                    }],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                })

        with patch("app.summarizer.httpx.AsyncClient", FakeClient), patch(
            "app.summarizer.asyncio.sleep", return_value=None
        ):
            result = await summarizer._call_api(
                "test",
                model_override="openai/gpt-5.6-luna-pro",
                model_fallbacks=["qwen/qwen3.8-flash"],
                response_schema={"type": "object"},
                purpose="risk_verification",
            )

        self.assertEqual(result, '{"events":[]}')
        self.assertEqual(
            requested_models,
            [
                "openai/gpt-5.6-luna-pro",
                "openai/gpt-5.6-luna-pro",
                "qwen/qwen3.8-flash",
            ],
        )


class EventMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.temp_dir.name) / "test.db"
        await database.init_db()
        self.complex_id = await database.create_complex("Тестовый ЖК")

    async def asyncTearDown(self):
        database.DATABASE_PATH = self.original_path
        self.temp_dir.cleanup()

    async def test_event_memory_is_updated_not_resolved_by_silence(self):
        messages = [message(1, 1, "Давайте напишем жалобу в УК")]
        event = validate_event(raw_event([1]), messages, "Чат", "telegram")
        first_start = datetime(2026, 9, 13)
        first_end = datetime(2026, 9, 13, 23, 59)
        ids = await database.upsert_event_memory(
            complex_id=self.complex_id,
            events=[event],
            period_start=first_start.isoformat(),
            period_end=first_end.isoformat(),
        )
        self.assertEqual(len(ids), 1)

        memory = await database.get_recent_event_memory(
            self.complex_id, first_start - timedelta(days=1)
        )
        self.assertEqual(memory[0]["state"], "new")

        # A day with no observed event performs no update and therefore cannot
        # silently resolve the existing memory card.
        await database.upsert_event_memory(
            complex_id=self.complex_id,
            events=[],
            period_start=datetime(2026, 9, 14).isoformat(),
            period_end=datetime(2026, 9, 14, 23, 59).isoformat(),
        )
        memory_after_silence = await database.get_recent_event_memory(
            self.complex_id, first_start - timedelta(days=1)
        )
        self.assertEqual(memory_after_silence[0]["state"], "new")
        self.assertEqual(memory_after_silence[0]["last_seen"], first_end.isoformat())

    async def test_rerunning_same_period_is_idempotent(self):
        event = validate_event(
            raw_event([1]),
            [message(1, 1, "Лифт не работает")],
            "Чат",
            "telegram",
        )
        period_start = datetime(2026, 9, 14).isoformat()
        period_end = datetime(2026, 9, 14, 23, 59).isoformat()

        for _ in range(2):
            await database.upsert_event_memory(
                complex_id=self.complex_id,
                events=[event],
                period_start=period_start,
                period_end=period_end,
            )

        async with database.aiosqlite.connect(database.DATABASE_PATH) as db:
            memory_count = (await (await db.execute(
                "SELECT COUNT(*) FROM event_memory"
            )).fetchone())[0]
            observation_count = (await (await db.execute(
                "SELECT COUNT(*) FROM event_observations"
            )).fetchone())[0]
            state = (await (await db.execute(
                "SELECT state FROM event_memory"
            )).fetchone())[0]

        self.assertEqual(memory_count, 1)
        self.assertEqual(observation_count, 1)
        self.assertEqual(state, "new")


if __name__ == "__main__":
    unittest.main()
