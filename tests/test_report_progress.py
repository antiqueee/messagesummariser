import unittest

from app.main import _build_partial_report


class ReportProgressTests(unittest.TestCase):
    def test_partial_report_contains_display_data_without_heavy_event_payload(self):
        report = {
            "report_run_id": "run-1",
            "generated_at": "2026-09-15T10:00:00",
            "period_start": "2026-09-14T00:00:00",
            "period_end": "2026-09-14T23:59:59",
            "mode": "ai",
            "usage": {"calls": 12},
            "complexes": [{
                "complex_id": 2,
                "complex_name": "ЖК Пригород Лесное",
                "chats": [{"chat_name": "Чат", "message_count": 5}],
                "summary": "Готовая сводка.",
                "verification": {"chats_checked": 1, "errors": []},
                "analysis_warnings": [{"kind": "verification_incomplete", "message": "Предварительно"}],
                "events": [{"evidence": [{"quote": "private source text"}]}],
                "structured_rows": [{"chat": "ЧАТ"}],
            }],
        }

        partial = _build_partial_report(report)

        self.assertEqual(partial["report_run_id"], "run-1")
        self.assertEqual(partial["complexes"][0]["summary"], "Готовая сводка.")
        self.assertEqual(
            partial["complexes"][0]["analysis_warnings"][0]["kind"],
            "verification_incomplete",
        )
        self.assertNotIn("events", partial["complexes"][0])
        self.assertNotIn("structured_rows", partial["complexes"][0])
        self.assertNotIn("usage", partial)


if __name__ == "__main__":
    unittest.main()
