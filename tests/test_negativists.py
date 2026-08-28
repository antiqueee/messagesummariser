import unittest
from datetime import datetime

from app.summarizer import ChatSummarizer


class NegativistsAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.summarizer = ChatSummarizer("test-key", "test-model")

    def test_batches_keep_every_message_and_identity(self):
        chats = [{
            "chat_name": "ЖК Тест",
            "source": "telegram",
            "content_filter": "",
            "messages": [
                {
                    "date": f"2026-08-27T10:{index:02d}:00Z",
                    "sender_id": 42,
                    "sender_name": "Иван",
                    "sender_username": "ivan42",
                    "text": "Претензия " + ("x" * 30),
                }
                for index in range(20)
            ],
        }]

        batches, diagnostics = self.summarizer._build_negativist_batches(chats, max_chars=500)

        combined = "\n".join(batches)
        self.assertGreater(len(batches), 1)
        self.assertEqual(diagnostics["input_messages"], 20)
        self.assertFalse(diagnostics["truncated"])
        self.assertEqual(combined.count("Претензия"), 20)
        self.assertIn("author_id=telegram:42; username=@ivan42", combined)

    def test_merge_uses_author_id_and_keeps_strongest_assessment(self):
        results = [
            {"negativists": [{
                "author_id": "telegram:42",
                "name": "Иван",
                "username": "ivan42",
                "threat_level": "low",
                "category": "critic",
                "status": "Критикует тарифы.",
                "description": "Несколько раз предметно критиковал начисления.",
                "phone": "+7 900 000-00-00",
                "building": "2",
                "section": None,
                "floor": None,
                "apartment": None,
                "tags": [],
                "evidence": [{"chat_name": "Чат 1", "date": "27.08", "quote": "Тариф снова неверный"}],
            }]},
            {"negativists": [{
                "author_id": "telegram:42",
                "name": "Иван Иванов",
                "username": "ivan42",
                "threat_level": "high",
                "category": "organizer",
                "status": "Собирает подписи.",
                "description": "Назначил место и время сбора подписей.",
                "phone": None,
                "building": None,
                "section": "3",
                "floor": "8",
                "apartment": "125",
                "tags": [],
                "evidence": [{"chat_name": "Чат 2", "date": "27.08", "quote": "Приходите подписать в 19:00"}],
            }]},
        ]

        people = self.summarizer._merge_negativists(results)

        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["threat_level"], "high")
        self.assertEqual(people[0]["category"], "organizer")
        self.assertEqual(len(people[0]["evidence"]), 2)
        self.assertIn("Назначил место", people[0]["description"])
        self.assertEqual(people[0]["phone"], "+7 900 000-00-00")
        self.assertEqual(people[0]["building"], "2")
        self.assertEqual(people[0]["section"], "3")
        self.assertEqual(people[0]["floor"], "8")
        self.assertEqual(people[0]["apartment"], "125")

    def test_same_display_name_with_different_ids_is_not_merged(self):
        result = {"negativists": [
            {"author_id": "telegram:1", "name": "Алексей", "status": "A"},
            {"author_id": "telegram:2", "name": "Алексей", "status": "B"},
        ]}

        people = self.summarizer._merge_negativists([result])

        self.assertEqual(len(people), 2)

    def test_messenger_profile_overrides_model_identity(self):
        chats = [{
            "source": "telegram",
            "messages": [{
                "sender_id": 12345,
                "sender_name": "Анна Смирнова",
                "sender_username": "anna_home",
            }],
        }]
        people = [{
            "author_id": "telegram:12345",
            "name": "Ошибочное имя AI",
            "username": None,
        }]

        profiles = self.summarizer._build_author_profiles(chats)
        self.summarizer._apply_author_profiles(people, profiles)

        self.assertEqual(people[0]["name"], "Анна Смирнова")
        self.assertEqual(people[0]["username"], "anna_home")


class NegativistsAsyncAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_processes_every_batch_and_reports_diagnostics(self):
        summarizer = ChatSummarizer("test-key", "test-model")
        calls = []

        async def fake_call(prompt, model_override=None):
            calls.append(prompt)
            return '{"negativists": [], "analysis_notes": null}'

        summarizer._call_api = fake_call
        messages = [{
            "message_id": index,
            "date": "2026-08-27T10:00:00Z",
            "sender_id": index,
            "sender_name": f"Автор {index}",
            "text": "Недовольство работой УК " + ("x" * 5000),
        } for index in range(25)]

        result = await summarizer.analyze_negativists(
            [{"chat_name": "Большой чат", "source": "telegram", "messages": messages}],
            datetime(2026, 8, 20),
            datetime(2026, 8, 27),
        )

        self.assertGreater(len(calls), 1)
        self.assertEqual(result["diagnostics"]["input_messages"], 25)
        self.assertEqual(result["diagnostics"]["successful_batches"], len(calls))
        self.assertEqual(result["diagnostics"]["failed_batches"], 0)
        self.assertFalse(result["diagnostics"]["truncated"])


if __name__ == "__main__":
    unittest.main()
