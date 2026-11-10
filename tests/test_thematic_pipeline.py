import json
import os
import re
import unittest
from datetime import datetime
from unittest.mock import patch

from app.summarizer import ChatSummarizer
from app.thematic_pipeline import (
    build_thematic_batches,
    collect_candidate_refs,
    expand_thematic_evidence,
    pack_thematic_text_blocks,
)


def msg(mid, sender, text, *, reply_to=None, date="2026-10-08T10:00:00Z"):
    return {
        "message_id": mid,
        "sender_id": sender,
        "sender_name": f"Автор {sender}",
        "text": text,
        "date": date,
        "reply_to": reply_to,
    }


class ThematicEvidenceTests(unittest.TestCase):
    def test_text_blocks_are_bounded_without_losing_content(self):
        blocks = ["a" * 7, "b" * 15, "c" * 4]
        groups = pack_thematic_text_blocks(blocks, max_chars=10)

        self.assertTrue(all(len(group) <= 10 for group in groups))
        self.assertEqual("".join(group.replace("\n", "") for group in groups), "".join(blocks))

    def test_refs_do_not_collide_between_chats(self):
        chats = [
            {
                "chat_id": 10,
                "chat_name": "Первый",
                "source": "telegram",
                "telegram_id": -100111,
                "messages": [msg(7, 1, "Первая версия")],
            },
            {
                "chat_id": 20,
                "chat_name": "Второй",
                "source": "telegram",
                "telegram_id": -100222,
                "messages": [msg(7, 2, "Вторая версия")],
            },
        ]
        batches, records, diagnostics = build_thematic_batches(chats)

        self.assertEqual(diagnostics["input_messages"], 2)
        self.assertEqual(set(records), {"telegram:10:7", "telegram:20:7"})
        self.assertIn("https://t.me/c/111/7", records["telegram:10:7"]["link"])
        self.assertEqual(len(batches), 1)

    def test_invalid_model_refs_are_rejected(self):
        payload = {
            "candidates": [{
                "refs": ["telegram:10:7", "invented:999"],
                "relevance": "primary",
                "reason": "test",
            }]
        }
        valid, invalid = collect_candidate_refs(payload, {"telegram:10:7"})
        self.assertEqual(valid, ["telegram:10:7"])
        self.assertEqual(invalid, 1)

    def test_context_expands_to_neighbours_parent_and_reply(self):
        chats = [{
            "chat_id": 10,
            "chat_name": "Чат",
            "source": "telegram",
            "telegram_id": -100111,
            "messages": [
                msg(1, 1, "До", date="2026-10-08T09:59:00Z"),
                msg(2, 2, "Главное", reply_to=1),
                msg(3, 3, "Ответ", reply_to=2, date="2026-10-08T10:01:00Z"),
                msg(4, 4, "После", date="2026-10-08T10:02:00Z"),
            ],
        }]
        _, records, diagnostics = build_thematic_batches(chats)
        evidence = expand_thematic_evidence(
            ["telegram:10:2"], records, diagnostics["chat_refs"], neighbour_radius=1
        )
        refs = {item["ref"] for item in evidence}
        self.assertEqual(refs, {"telegram:10:1", "telegram:10:2", "telegram:10:3"})
        self.assertTrue(next(item for item in evidence if item["ref"] == "telegram:10:2")["selected"])


class ThematicReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_prompt_controls_final_report(self):
        summarizer = ChatSummarizer("test", "google/gemini-3.8-flash")
        prompts = []

        async def fake_call(prompt, model_override=None, model_fallbacks=None,
                            response_schema=None, purpose="unknown"):
            prompts.append((purpose, prompt))
            if purpose == "thematic_scan":
                return '{"candidates":[{"refs":["telegram:10:2"],"relevance":"primary","reason":"в тему"}],"batch_summary":""}'
            return "ГОТОВЫЙ ТЕМАТИЧЕСКИЙ ОТЧЁТ"

        summarizer._call_api = fake_call
        result = await summarizer.build_thematic_report(
            complex_name="ЖК Тест",
            chats_with_messages=[{
                "chat_id": 10,
                "chat_name": "Общий чат",
                "source": "telegram",
                "telegram_id": -100111,
                "messages": [msg(1, 1, "Дождь"), msg(2, 2, "Затопило паркинг", reply_to=1)],
            }],
            start_date=datetime(2026, 10, 1),
            end_date=datetime(2026, 10, 8),
            instructions="Выясни последствия затопления после дождя и дай хронологию.",
            coverage=[{"chat_name": "Общий чат", "status": "read", "message_count": 2}],
        )

        self.assertEqual(result["summary_text"], "ГОТОВЫЙ ТЕМАТИЧЕСКИЙ ОТЧЁТ")
        self.assertEqual(result["matched_message_count"], 1)
        self.assertEqual(result["evidence_count"], 2)
        final_prompt = next(prompt for purpose, prompt in prompts if purpose == "thematic_render")
        self.assertIn("Выясни последствия затопления", final_prompt)
        self.assertIn("Затопило паркинг", final_prompt)
        self.assertIn("author=Автор 2", final_prompt)

    async def test_large_evidence_is_reduced_before_final_render(self):
        summarizer = ChatSummarizer("test", "google/gemini-3.8-flash")
        prompts = []

        async def fake_call(prompt, model_override=None, model_fallbacks=None,
                            response_schema=None, purpose="unknown"):
            prompts.append((purpose, prompt))
            if purpose == "thematic_scan":
                refs = re.findall(r"\[ref=([^\]]+)\]", prompt)
                return json.dumps({
                    "candidates": [{
                        "refs": refs,
                        "relevance": "primary",
                        "reason": "в тему",
                    }],
                    "batch_summary": "",
                })
            if purpose == "thematic_reduce":
                refs = re.findall(r"(?:\[ref=|ref=)([^\]\s]+)", prompt)
                digest = "Краткий конспект со ссылками: " + ", ".join(refs[:4])
                if "Уровень сжатия: 1" in prompt:
                    return digest + (" данные" * 1200)
                return digest
            return "ГОТОВЫЙ БОЛЬШОЙ ОТЧЁТ"

        summarizer._call_api = fake_call
        messages = [
            msg(
                index,
                index,
                f"Релевантное сообщение {index}: " + ("подробности " * 100),
                date=f"2026-10-08T10:{index:02d}:00Z",
            )
            for index in range(1, 21)
        ]
        with patch.dict(os.environ, {
            "AI_THEMATIC_DIRECT_EVIDENCE_CHARS": "20000",
            "AI_THEMATIC_REDUCE_BATCH_CHARS": "10000",
        }):
            result = await summarizer.build_thematic_report(
                complex_name="ЖК Большой",
                chats_with_messages=[{
                    "chat_id": 10,
                    "chat_name": "Большой чат",
                    "source": "telegram",
                    "telegram_id": -100111,
                    "messages": messages,
                }],
                start_date=datetime(2026, 10, 1),
                end_date=datetime(2026, 10, 9),
                instructions="Собери все события и сохрани ссылки на сообщения.",
                coverage=[{"chat_name": "Большой чат", "status": "read", "message_count": 20}],
            )

        self.assertEqual(result["summary_text"], "ГОТОВЫЙ БОЛЬШОЙ ОТЧЁТ")
        self.assertGreaterEqual(result["diagnostics"]["reduction_rounds"], 2)
        self.assertGreaterEqual(result["diagnostics"]["reduction_calls"], 1)
        self.assertTrue(any(purpose == "thematic_reduce" for purpose, _ in prompts))
        final_prompt = next(prompt for purpose, prompt in prompts if purpose == "thematic_render")
        self.assertLess(len(final_prompt), 25_000)


if __name__ == "__main__":
    unittest.main()
