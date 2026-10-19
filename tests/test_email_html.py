from dataclasses import asdict
from datetime import date
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
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


class Layout(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.div_depth = 0
        self.section_depth = None
        self.section = None
        self.title = None
        self.section_titles = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "div":
            self.div_depth += 1
            if attributes.get("data-section"):
                self.section = attributes["data-section"]
                self.section_depth = self.div_depth
                self.section_titles[self.section] = []
        if tag == "h3" and self.section:
            self.title = []

    def handle_data(self, text):
        if self.title is not None:
            self.title.append(text)

    def handle_endtag(self, tag):
        if tag == "h3" and self.title is not None:
            self.section_titles[self.section].append("".join(self.title))
            self.title = None
        if tag == "div":
            if self.div_depth == self.section_depth:
                self.section = self.section_depth = None
            self.div_depth -= 1


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

    def test_semantic_sections_keep_each_event_once_and_sort_without_changing_action_targets(self):
        events = [
            example(uid="late", title="Поздняя встреча", start="2026-10-14T18:00:00+03:00", end="2026-10-14T19:00:00+03:00"),
            example(uid="early", title="Ранняя встреча", start="2026-10-14T09:00:00+03:00", end="2026-10-14T10:00:00+03:00"),
            example(uid="birthday", title="День рождения коллеги", start="2026-10-14", end="2026-10-15", all_day=True),
            example(uid="control", source_id="mailru_2", title="Отчёт по поручению"),
            example(uid="cancelled", title="Отменённая встреча", event_status="CANCELLED"),
            example(uid="moved", title="Перенесённая встреча", original_start="2026-10-14T11:00:00+03:00", original_end="2026-10-14T12:00:00+03:00"),
            example(uid="holiday", title="Праздник: день команды", start="2026-10-14", end="2026-10-15", all_day=True),
            example(uid="all-day", title="День в офисе", start="2026-10-14", end="2026-10-15", all_day=True),
        ]
        links = {(index, action): f"https://digest.example/a/synthetic-{index}-{action}"
                 for index in range(len(events)) for action in ("reschedule", "cancel")}
        output = render_email_html(events, date(2026, 10, 14), action_links=links)
        sections = Layout(output).section_titles
        self.assertEqual(sections["timeline"], ["Ранняя встреча", "Поздняя встреча"])
        self.assertEqual(sections["birthdays"], ["День рождения коллеги"])
        self.assertEqual(sections["control"], ["Отчёт по поручению"])
        self.assertEqual(sections["changes"], ["Отменённая встреча", "Перенесённая встреча"])
        self.assertEqual(sections["holidays"], ["Праздник: день команды"])
        self.assertEqual(sections["all_day"], ["День в офисе"])
        self.assertCountEqual([title for group in sections.values() for title in group], [event.title for event in events])
        tags = Tags(output).elements
        self.assertEqual(sum(attributes.get("class") == "event-card" for _, attributes in tags), len(events))
        buttons = [attributes for _, attributes in tags if attributes.get("class") == "action"]
        for index, event in enumerate(events):
            actual = [button["href"] for button in buttons if button["aria-label"].endswith(": " + event.title)]
            self.assertEqual(set(actual), set() if event.event_status == "CANCELLED" else {
                links[index, "reschedule"], links[index, "cancel"],
            })
        self.assertIn("Было: 14 октября 2026, 11:00–12:00", output)
        self.assertIn("Стало: <strong>14 октября 2026, 12:00–13:00", output)

    def test_section_accounts_come_from_event_metadata_and_mixed_cards_identify_their_source(self):
        records = [
            SimpleNamespace(**{**asdict(example(title="Первый источник")), "account_email": "first@example.invalid"}),
            SimpleNamespace(**{**asdict(example(title="Второй источник", source_id="google")), "account_email": "second@example.invalid"}),
        ]
        output = render_email_html(records, date(2026, 10, 14), action_links={})
        self.assertIn("Адреса источников: first@example.invalid · second@example.invalid", output)
        self.assertEqual(output.count('class="card-source-email"'), 2)
        self.assertIn("Google Календарь", output)
        self.assertNotIn("три аккаунта Mail.ru", output)
        only_one = render_email_html([records[0]], date(2026, 10, 14), action_links={})
        self.assertIn("Адрес источника: first@example.invalid", only_one)
        self.assertNotIn('class="card-source-email"', only_one)
        old_record = asdict(example(source_id="mailru_2", calendar_name="Контроль поручений"))
        old_record.pop("account_email", None)
        legacy = render_email_html([SimpleNamespace(**old_record)], date(2026, 10, 14), action_links={})
        self.assertIn("Контроль поручений", legacy)
        self.assertNotIn('class="section-source-emails"', legacy)
        self.assertNotIn("gpb.control@mail.ru", legacy)

    def test_account_labels_are_escaped_and_static_email_has_no_external_assets(self):
        event = SimpleNamespace(**{**asdict(example()), "account_email": '\"><img src="https://tracker.invalid">'})
        output = render_email_html([event], date(2026, 10, 14), action_links={})
        tags = Tags(output).elements
        self.assertIn("&lt;img", output)
        self.assertFalse(any(tag in {"img", "iframe", "svg", "script", "link"} for tag, _ in tags))
        self.assertFalse(any("src" in attributes for _, attributes in tags))
        self.assertNotIn("url(", output)

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
