from dataclasses import asdict
from datetime import date
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calendar_digest.email_html import render_email_html
from calendar_digest.preview import render_saved_preview
from calendar_digest.selection import DigestEvent


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def example(**overrides):
    values = dict(source_id="icloud", calendar_id="synthetic", calendar_name="Дом",
                  uid="one", recurrence_id=None, title="Встреча", start="2026-10-14T12:00:00+03:00",
                  end="2026-10-14T13:00:00+03:00", all_day=False, event_status="CONFIRMED",
                  participation="ACCEPTED", recurring=False, multi_day=False)
    values.update(overrides)
    return DigestEvent(**values)


class EmailHTMLTests(unittest.TestCase):
    def test_calendar_text_cannot_inject_markup_or_action_attributes(self):
        value = example(title='</script><img src=x onerror="alert(1)">',
                        calendar_name='<iframe src="https://example.test">',
                        location='<a href="javascript:alert(1)">place</a>')
        for interactive in (False, True):
            output = render_email_html([value], date(2026,10,14), interactive=interactive)
            tags = Tags(output).elements
            self.assertFalse(any(tag in ("img", "iframe") for tag, _ in tags))
            self.assertFalse(any(key.startswith("on") for _, attrs in tags for key in attrs))
            buttons = [attrs for _, attrs in tags if "data-action" in attrs]
            self.assertEqual(len(buttons), 2)
            self.assertTrue(all(attrs["data-title"] == value.title for attrs in buttons))
            self.assertTrue(all(attrs["href"] == "#actions-demo" for attrs in buttons))

    def test_email_is_script_free_and_demo_is_explicitly_separate(self):
        static = render_email_html([example()], date(2026,10,14))
        interactive = render_email_html([example()], date(2026,10,14), interactive=True)
        static_tags = Tags(static).elements
        self.assertFalse(any(tag in ("script", "form", "dialog") for tag, _ in static_tags))
        self.assertEqual(sum(tag == "script" for tag, _ in Tags(interactive).elements), 1)
        self.assertIn("Это демонстрация", interactive)
        self.assertNotIn("fetch(", interactive)
        self.assertNotIn("XMLHttpRequest", interactive)

    def test_offline_restyle_preserves_selection_and_baseline_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = json.dumps({"date":"2026-10-14", "timezone":"Europe/Moscow",
                                   "events":[asdict(example())], "first_snapshot":True})
            selection = root / "preview-2026-10-14.json"
            selection.write_text(original)
            baseline = root / "calendar-state.json"
            baseline.write_text("baseline must stay untouched")
            with patch("calendar_digest.preview.collect_all") as network:
                result = render_saved_preview(date(2026,10,14), root)
                network.assert_not_called()
            self.assertFalse(result["calendar_changes"])
            self.assertEqual(selection.read_text(), original)
            self.assertEqual(baseline.read_text(), "baseline must stay untouched")
            for key in ("email_html", "preview_html"):
                self.assertEqual(Path(result[key]).stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
