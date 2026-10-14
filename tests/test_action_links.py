from dataclasses import asdict
from datetime import date
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from calendar_digest.action_links import issue_email, public_url
from calendar_digest.action_store import ActionStore, StoreError
from calendar_digest.email_html import render_email_html
from calendar_digest.selection import DigestEvent


DAY = date(2026, 10, 14)
BASE = "https://digest.example.test"


def event(**overrides):
    values = {
        "source_id": "mailru_1", "calendar_id": "private-calendar-id",
        "calendar_name": "Рабочий календарь", "uid": "private-event-id",
        "recurrence_id": None, "title": "Встреча", "start": "2026-10-14T12:00:00+03:00",
        "end": "2026-10-14T13:00:00+03:00", "all_day": False,
        "event_status": "CONFIRMED", "participation": "ACCEPTED",
        "recurring": False, "multi_day": False,
    }
    values.update(overrides)
    return DigestEvent(**values)


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.visible = []
        self.hidden_depth = 0
        self.feed(text)

    def handle_starttag(self, tag, attributes):
        self.elements.append((tag, dict(attributes)))
        if tag in {"script", "style"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            self.visible.append(data)

    @property
    def action_links(self):
        return [attrs for tag, attrs in self.elements
                if tag == "a" and "action" in attrs.get("class", "").split()]


class ActionLinksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "samples"
        self.directory.mkdir()
        self.state = self.root / "private-state"
        self.key = secrets.token_urlsafe(48)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def save(self, records, **overrides):
        payload = {"date": DAY.isoformat(), "timezone": "Europe/Moscow",
                   "events": [asdict(value) for value in records], "first_snapshot": False}
        payload.update(overrides)
        path = self.directory / f"preview-{DAY.isoformat()}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def issue(self, **overrides):
        options = {"state_dir": self.state, "base_url": BASE, "signing_key": self.key}
        options.update(overrides)
        return issue_email(DAY, self.directory, **options)

    def test_opaque_links_resolve_to_exact_instance_in_shared_state_database(self):
        timed = event(recurrence_id="2026-10-14T09:00:00+03:00", recurring=True)
        all_day = event(uid="private-all-day-id", title="Задача на день", all_day=True,
                        start="2026-10-14", end="2026-10-15",
                        event_status="TENTATIVE", participation="TENTATIVE")
        cancelled = event(uid="cancelled-event", event_status="CANCELLED", participation="DECLINED")
        unknown = event(uid="unknown-event", event_status="UNSPECIFIED", participation="UNKNOWN")
        source = self.save([timed, all_day, cancelled, unknown])
        initial_source = source.read_bytes()
        baseline = self.directory / "calendar-state.json"
        baseline.write_text("existing baseline")
        with patch("niquests.Session.send") as network, patch(
            "calendar_digest.writable_caldav.CalDAVActionBackend.write"
        ) as calendar_write:
            result = self.issue()
            network.assert_not_called()
            calendar_write.assert_not_called()
        self.assertEqual(result["events"], 4)
        self.assertEqual(result["action_links"], 3)
        self.assertFalse(result["sent"])
        output = Path(result["email_html"])
        self.assertEqual(output, self.directory / "email-actions-2026-10-14.html")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(source.read_bytes(), initial_source)
        self.assertEqual(baseline.read_text(), "existing baseline")
        self.assertFalse((self.directory / "actions.sqlite3").exists())
        store = ActionStore(self.state / "actions.sqlite3", self.key)
        markup = Markup(output.read_text())
        self.assertEqual(sum("event-card" in attrs.get("class", "").split()
                             for _, attrs in markup.elements), 4)
        resolved = []
        for link in markup.action_links:
            parsed = urlsplit(link["href"])
            self.assertEqual(parsed.scheme, "https")
            self.assertEqual(parsed.netloc, "digest.example.test")
            self.assertEqual(parsed.query, "")
            self.assertEqual(parsed.fragment, "")
            self.assertTrue(parsed.path.startswith("/a/"))
            token = parsed.path.removeprefix("/a/")
            self.assertEqual(len(token), 87)
            self.assertNotIn("private", token)
            self.assertNotIn(self.key, token)
            record = store.read(token)
            self.assertEqual(record["state"], "ready")
            self.assertIsNone(record["result"])
            target = record["target"]
            chosen = timed if target["uid"] == timed.uid else all_day
            self.assertEqual(target, {
                "source_id": chosen.source_id, "calendar_id": chosen.calendar_id,
                "uid": chosen.uid, "expected_start": chosen.start, "expected_end": chosen.end,
                "recurrence_id": chosen.recurrence_id, "expected_event_status": chosen.event_status,
                "expected_participation": chosen.participation,
            })
            self.assertTrue(link["aria-label"].endswith(chosen.title))
            resolved.append((chosen.uid, record["action"]))
        self.assertCountEqual(resolved, [(timed.uid, "reschedule"), (timed.uid, "cancel"),
                                         (all_day.uid, "confirm")])

    def test_issued_mail_is_script_free_and_has_no_demo_controls(self):
        self.save([event()])
        markup = Markup(Path(self.issue()["email_html"]).read_text())
        self.assertEqual(len(markup.action_links), 2)
        self.assertFalse(any(tag in {"script", "form", "dialog"} for tag, _ in markup.elements))
        self.assertFalse(any("data-action" in attrs for _, attrs in markup.elements))
        self.assertTrue(all(link["href"].startswith(BASE + "/a/") for link in markup.action_links))
        visible = " ".join(markup.visible)
        self.assertNotIn("Это демонстрация", visible)
        self.assertNotIn("Кнопки показаны для согласования", visible)
        self.assertIn("После входа подтвердите действие", visible)
        self.assertIn("Ссылки действуют 48 часов", visible)

    def test_environment_configuration_and_render_address_fallback(self):
        self.save([event()])
        environment = {"RENDER_EXTERNAL_URL": BASE + "/", "DIGEST_ACTION_SECRET": self.key,
                       "DIGEST_STATE_DIR": str(self.state)}
        with patch.dict(os.environ, environment):
            result = issue_email(DAY, self.directory)
        markup = Markup(Path(result["email_html"]).read_text())
        self.assertTrue(all(link["href"].startswith(BASE + "/a/") for link in markup.action_links))
        self.assertTrue((self.state / "actions.sqlite3").is_file())

    def test_explicit_public_address_takes_precedence_over_render_address(self):
        self.save([event()])
        environment = {"RENDER_EXTERNAL_URL": "https://fallback.example.test",
                       "DIGEST_PUBLIC_URL": BASE, "DIGEST_ACTION_SECRET": self.key,
                       "DIGEST_STATE_DIR": str(self.state)}
        with patch.dict(os.environ, environment):
            result = issue_email(DAY, self.directory)
        self.assertTrue(all(link["href"].startswith(BASE + "/a/") for link in
                            Markup(Path(result["email_html"]).read_text()).action_links))

    def test_missing_settings_and_invalid_saved_digest_fail_safely(self):
        self.save([event()])
        for environment in ({}, {"DIGEST_PUBLIC_URL": BASE}, {"DIGEST_ACTION_SECRET": self.key}):
            with self.subTest(keys=list(environment)), patch.dict(os.environ, environment):
                with self.assertRaises(StoreError) as error:
                    issue_email(DAY, self.directory)
                self.assertNotIn(self.key, str(error.exception))
        for changes in ({"date": "2026-10-15"}, {"timezone": "Etc/UTC"}, {"events": [{}]}):
            self.save([event()], **changes)
            with self.assertRaises(StoreError):
                self.issue()
        self.assertFalse((self.directory / "email-actions-2026-10-14.html").exists())

    def test_public_url_rejects_http_private_hosts_and_embedded_credentials(self):
        invalid = (
            "http://digest.example.test", "https://localhost", "https://localhost.",
            "https://calendar.local", "https://host.localhost", "https://127.0.0.1",
            "https://127.1", "https://10.0.0.1", "https://[::1]",
            "https://user:secret@digest.example.test", "https://digest.example.test:8443",
            "https://digest.example.test/path", "https://digest.example.test?key=secret",
            "https://digest.example.test#fragment", "", None,
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(StoreError) as error:
                    public_url(value)
                self.assertNotIn("secret", str(error.exception))
        self.assertEqual(public_url(BASE + "/"), BASE)

    def test_calendar_text_is_escaped_in_live_mail(self):
        malicious = event(title='</a><script>alert(1)</script><img src=x onerror="alert(2)">',
                          calendar_name='<iframe src="https://attacker.example.test">',
                          location='" onmouseover="alert(3) <b>place</b>')
        self.save([malicious])
        output = Path(self.issue()["email_html"]).read_text()
        markup = Markup(output)
        self.assertFalse(any(tag in {"script", "img", "iframe"} for tag, _ in markup.elements))
        self.assertFalse(any(key.startswith("on") for _, attrs in markup.elements for key in attrs))
        self.assertEqual(len(markup.action_links), 2)
        self.assertTrue(all(link["aria-label"].endswith(malicious.title) for link in markup.action_links))
        self.assertIn("&lt;script&gt;", output)

    def test_renderer_keeps_live_links_and_demo_modes_separate(self):
        chosen = event()
        demo = Markup(render_email_html([chosen], DAY, interactive=True))
        self.assertTrue(any(tag == "script" for tag, _ in demo.elements))
        self.assertTrue(all(link["href"] == "#actions-demo" for link in demo.action_links))
        links = {(0, "cancel"): BASE + "/a/signed-opaque-token"}
        live = Markup(render_email_html([chosen], DAY, interactive=True, action_links=links))
        self.assertFalse(any(tag in {"script", "form", "dialog"} for tag, _ in live.elements))
        self.assertEqual(len(live.action_links), 1)
        self.assertEqual(live.action_links[0]["href"], links[0, "cancel"])
        self.assertNotIn("data-action", live.action_links[0])
        empty = Markup(render_email_html([chosen], DAY, interactive=True, action_links={}))
        self.assertEqual(empty.action_links, [])
        self.assertFalse(any(tag == "script" for tag, _ in empty.elements))


if __name__ == "__main__":
    unittest.main()
