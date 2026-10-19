"""Notice-only records survive every presentation path without action tokens."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from calendar_digest.action_links import issue_email
from calendar_digest.action_store import ActionStore, StoreError
from calendar_digest.calendar_collection import Collection
from calendar_digest.delivery import DeliveryError, prepare_delivery
from calendar_digest.email_html import render_email_html
from calendar_digest.local_state import Snapshot, snapshot_json, write_private
from calendar_digest.preview import PreviewError, create_preview, render_markdown, render_saved_preview
from calendar_digest.selection import DigestEvent, SeriesChangeNotice, SeriesInterval
from calendar_digest.series_changes import parse_series_changes
from test_preview import resource


DAY = date(2026, 10, 14)


def notice(**overrides):
    values = dict(
        source_id="icloud", calendar_id="synthetic", calendar_name="Дом", uid="series-only-uid",
        title="Изменившаяся серия", old_anchor_start="2020-10-01", old_anchor_end="2020-10-02",
        new_anchor_start="2026-10-15T15:00:00+03:00", new_anchor_end="2026-10-15T16:00:00+03:00",
        old_recurring=True, new_recurring=True,
        old_intervals=[SeriesInterval("2026-10-14", "2026-10-15", True)], new_intervals=[],
    )
    values.update(overrides)
    return SeriesChangeNotice(**values)


def event():
    return DigestEvent(
        source_id="icloud", calendar_id="synthetic", calendar_name="Дом", uid="current-event-uid",
        recurrence_id=None, title="Текущее событие", start="2026-10-14T12:00:00+03:00",
        end="2026-10-14T13:00:00+03:00", all_day=False, event_status="CONFIRMED",
        participation="ACCEPTED", recurring=False, multi_day=False,
    )


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class SeriesPresentationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {
            "DIGEST_STATE_DIR": temporary.name,
            "DIGEST_PUBLIC_URL": "https://digest.example.test",
            "DIGEST_ACTION_SECRET": "synthetic-signing-key-" * 3,
            "SMTP_USERNAME": "sender@example.invalid", "SMTP_PASSWORD": "synthetic-password",
            "DIGEST_RECIPIENT": "recipient@example.invalid",
        }, clear=True))

    def save(self, *, events=(), changes=()):
        path = self.directory / f"preview-{DAY.isoformat()}.json"
        path.write_text(json.dumps({
            "date": DAY.isoformat(), "timezone": "Europe/Moscow", "first_snapshot": False,
            "events": [asdict(item) for item in events], "series_changes": [asdict(item) for item in changes],
        }), encoding="utf-8")
        return path

    def test_notices_are_separate_from_current_events_and_have_no_buttons_in_any_layout(self):
        changed = notice()
        for options in ({}, {"interactive": True}, {"action_links": {}}):
            with self.subTest(options=options):
                html = render_email_html([], DAY, series_changes=[changed], **options)
                tags = Tags(html).elements
                self.assertEqual(sum(attrs.get("class") == "series-change-card" for _, attrs in tags), 1)
                self.assertFalse(any(attrs.get("class") == "event-card" for _, attrs in tags))
                self.assertFalse(any("data-action" in attrs or attrs.get("class") == "action" for _, attrs in tags))
                self.assertIn("Изменения повторяющихся событий", html)
                self.assertIn("В сохранённом расписании", html)
                self.assertIn("Сейчас", html)
                self.assertIn("Начало серии: 1 октября 2020", html)
                self.assertIn("14 октября 2026 · весь день", html)
                self.assertNotIn("Перенесено", html)
                self.assertNotIn("изменилось сегодня", html.lower())

    def test_notice_text_is_escaped_and_each_interval_keeps_its_own_date_type(self):
        changed = notice(title='<img src=x onerror="alert(1)"> [secret](https://example.test)',
                         calendar_name='<iframe src="https://example.test">', new_recurring=False,
                         new_anchor_start="2026-10-14T15:00:00+03:00", new_anchor_end="2026-10-14T16:00:00+03:00",
                         new_intervals=[SeriesInterval("2026-10-14T15:00:00+03:00", "2026-10-14T16:00:00+03:00", False)])
        html = render_email_html([], DAY, series_changes=[changed])
        tags = Tags(html).elements
        self.assertFalse(any(tag in {"img", "iframe"} for tag, _ in tags))
        self.assertFalse(any(key.startswith("on") for _, attrs in tags for key in attrs))
        self.assertIn("&lt;img", html)
        self.assertIn("15:00–16:00", html)
        self.assertIn("Начало события:", html)
        markdown = render_markdown([], DAY, first_snapshot=False, series_changes=[changed])
        self.assertIn("Изменения повторяющихся событий", markdown)
        self.assertIn("14.10.2026 · весь день", markdown)
        self.assertIn("14.10.2026 15:00", markdown)
        self.assertIn("&lt;img", markdown)
        self.assertIn(r"\[secret\]", markdown)
        self.assertNotIn("**Перенесено", markdown)

    def test_saved_notice_round_trip_restyles_and_issues_no_action_tokens(self):
        selection = self.save(changes=[notice()])
        original = selection.read_bytes()
        with patch("calendar_digest.preview.collect_all") as collect, patch.object(ActionStore, "issue") as issue:
            result = render_saved_preview(DAY, self.directory)
            live = issue_email(DAY, self.directory)
            collect.assert_not_called()
            issue.assert_not_called()
        self.assertEqual(result["series_changes"], 1)
        self.assertEqual(live["events"], 0)
        self.assertEqual(live["series_changes"], 1)
        self.assertEqual(live["action_links"], 0)
        self.assertEqual(selection.read_bytes(), original)
        for path in (result["email_html"], result["preview_html"], live["email_html"]):
            self.assertIn("Изменения повторяющихся событий", Path(path).read_text())
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)

    def test_notices_do_not_change_current_event_action_count(self):
        self.save(events=[event()], changes=[notice()])
        result = issue_email(DAY, self.directory)
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["action_links"], 2)
        self.assertEqual(result["series_changes"], 1)
        tags = Tags(Path(result["email_html"]).read_text()).elements
        links = [attrs for _, attrs in tags if attrs.get("class") == "action"]
        self.assertEqual(len(links), 2)
        self.assertTrue(all(attrs["aria-label"].endswith(event().title) for attrs in links))

    def test_series_source_email_is_grouped_with_changes_without_turning_notice_into_action(self):
        changed = replace(notice(), account_email="series@example.invalid")
        moved = replace(event(), original_start="2026-10-14T11:00:00+03:00",
                        original_end="2026-10-14T12:00:00+03:00", account_email="calendar@example.invalid")
        html = render_email_html([moved], DAY, series_changes=[changed], action_links={})
        self.assertIn("Адреса источников: calendar@example.invalid · series@example.invalid", html)
        self.assertEqual(html.count('class="card-source-email"'), 2)
        self.assertIn("Изменения в расписании · 2", html)
        tags = Tags(html).elements
        self.assertEqual(sum(attrs.get("class") == "series-change-card" for _, attrs in tags), 1)
        self.assertEqual(sum(attrs.get("class") == "event-card" for _, attrs in tags), 1)
        self.assertFalse(any(attrs.get("class") == "action" for _, attrs in tags))

    def test_preview_writes_notice_beside_current_events_and_keeps_it_after_reload(self):
        old = resource("20261013T090000Z")
        old = replace(old, data=old.data.replace("DURATION:PT1H", "DURATION:PT1H\r\nRRULE:FREQ=DAILY;COUNT=3"))
        current = replace(old, data=old.data.replace("DTSTART:20261013T090000Z", "DTSTART:20261014T100000Z"))
        write_private(self.directory / "calendar-state.json", snapshot_json(Snapshot([old], [])))
        with patch("calendar_digest.preview.collect_all", return_value=Collection([current], 1)):
            first = create_preview(DAY, self.directory)
            second = create_preview(DAY, self.directory)
        for summary in (first, second):
            self.assertEqual(summary["events"], 1)
            self.assertEqual(summary["series_changes"], 1)
            self.assertEqual(summary["moved"], 0)
        payload = json.loads(Path(second["preview_json"]).read_text())
        self.assertEqual(len(parse_series_changes(payload["series_changes"])), 1)
        self.assertIsNone(payload["events"][0]["original_start"])
        for key in ("preview_markdown", "email_html", "preview_html"):
            self.assertIn("Изменения повторяющихся событий", Path(second[key]).read_text())

    def test_outbox_preparation_preserves_notices_in_both_mime_parts_without_sending(self):
        def preview(*_, **__):
            self.save(events=[event()], changes=[notice()])

        with patch("calendar_digest.preview.create_preview", side_effect=preview), \
                patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL") as smtp:
            summary = prepare_delivery(DAY, self.directory, kind="test")
            smtp.assert_not_called()
        self.assertEqual(summary["state"], "ready")
        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["action_links"], 2)
        with sqlite3.connect(self.directory / "deliveries.sqlite3") as database:
            name = database.execute("SELECT eml_name FROM deliveries WHERE kind='test'").fetchone()[0]
        message = BytesParser(policy=policy.default).parsebytes((self.directory / "deliveries" / name).read_bytes())
        for subtype in ("plain", "html"):
            text = message.get_body(preferencelist=(subtype,)).get_content()
            self.assertIn("Изменения повторяющихся событий", text)
            self.assertIn("Изменившаяся серия", text)
            self.assertIn("В сохранённом расписании", text)

    def test_malformed_notice_fails_before_actions_or_smtp_and_preserves_saved_files(self):
        path = self.save(changes=[notice()])
        payload = json.loads(path.read_text())
        payload["series_changes"][0]["old_intervals"][0]["start"] = "private-invalid-date"
        path.write_text(json.dumps(payload))
        marker = self.directory / f"email-{DAY.isoformat()}.html"
        marker.write_text("existing preview")
        with patch.object(ActionStore, "issue") as issue:
            with self.assertRaises(PreviewError):
                render_saved_preview(DAY, self.directory)
            with self.assertRaises(StoreError):
                issue_email(DAY, self.directory)
            issue.assert_not_called()
        self.assertEqual(marker.read_text(), "existing preview")
        with patch("calendar_digest.preview.create_preview"), \
                patch("calendar_digest.action_links.compose_action_email") as compose, \
                patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL") as smtp:
            with self.assertRaises(DeliveryError) as failure:
                prepare_delivery(DAY, self.directory, kind="test")
            self.assertNotIn("private-invalid-date", str(failure.exception))
            compose.assert_not_called()
            smtp.assert_not_called()

    def test_parser_rejects_bad_nested_types_duplicate_notices_and_private_date_errors(self):
        valid = asdict(notice())
        self.assertEqual(parse_series_changes([valid]), [notice()])
        malformed = []
        for field, value in (("old_recurring", "true"), ("title", {}), ("old_intervals", {}),
                             ("new_anchor_start", "private-date")):
            item = deepcopy(valid)
            item[field] = value
            malformed.append([item])
        item = deepcopy(valid)
        item["old_intervals"][0]["all_day"] = "true"
        malformed.extend([[item], [valid, valid], {}, None])
        for values in malformed:
            with self.subTest(values=values), self.assertRaises(ValueError) as failure:
                parse_series_changes(values)
            self.assertNotIn("private-date", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
