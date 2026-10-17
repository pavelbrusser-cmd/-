"""Coherent series history and compatible private snapshot persistence."""

from dataclasses import asdict, replace
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from icalendar import Calendar

from calendar_digest.calendar_collection import CalendarResource
from calendar_digest import local_state
from calendar_digest.local_state import Snapshot, StateError, advance_snapshot, read_snapshot, snapshot_json, write_snapshot
from calendar_digest.selection import select_day


def event(uid="series", start="20261015T120000Z", *, recurring=True, rid=None, extra=""):
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTART:{start}", "SUMMARY:Synthetic"]
    if recurring:
        lines.append("RRULE:FREQ=DAILY;COUNT=5")
    if rid is not None:
        lines.append(f"RECURRENCE-ID:{rid}")
    if extra:
        lines.append(extra)
    return "\r\n".join([*lines, "END:VEVENT"])


def resource(*events, source="icloud", calendar="test"):
    return CalendarResource(source, calendar, "Synthetic", "self@example.invalid", "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//History Tests//EN", *events, "END:VCALENDAR", "",
    ]))


def encoded(resource):
    return {key: value for key, value in asdict(resource).items() if key != "account_email"}


def identities(resources):
    return [{str(item["UID"]) for item in Calendar.from_ical(resource.data).walk("VEVENT")} for resource in resources]


class SeriesHistoryTests(unittest.TestCase):
    def test_new_field_has_independent_defaults(self):
        first, second = Snapshot([], []), Snapshot([], [])
        first.series_previous.append(resource(event()))
        self.assertEqual(second.series_previous, [])

    def test_deleted_and_unchanged_detached_overrides_are_preserved(self):
        master = resource(event())
        deleted = resource(event(start="20261016T140000Z", recurring=False, rid="20261016T120000Z"))
        cancelled = resource(event(start="20261017T120000Z", recurring=False, rid="20261017T120000Z", extra="STATUS:CANCELLED"))
        moved = resource(event(start="20261018T120000Z"))
        result = advance_snapshot([moved, cancelled], Snapshot([master, deleted, cancelled], []))
        self.assertEqual(result.series_previous, [master, deleted, cancelled])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_snapshot(path, result)
            restored = read_snapshot(path)
            self.assertEqual([item.data for item in restored.series_previous], [master.data, deleted.data, cancelled.data])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("self@example.invalid", path.read_text())
            self.assertEqual(advance_snapshot([moved, cancelled], restored).series_previous, restored.series_previous)

    def test_next_move_uses_latest_complete_current_not_old_remembered_history(self):
        first = resource(event(start="20261015T120000Z"))
        second = resource(event(start="20261016T120000Z"))
        third = resource(event(start="20261017T120000Z"))
        old_override = resource(event(start="20261016T140000Z", recurring=False, rid="20261016T120000Z"))
        new_override = resource(event(start="20261016T150000Z", recurring=False, rid="20261016T120000Z"))
        once = advance_snapshot([second, new_override], Snapshot([first, old_override], []))
        twice = advance_snapshot([third], once)
        self.assertEqual(twice.series_previous, [second, new_override])
        self.assertNotIn(first, twice.series_previous)
        edited = replace(third, data=third.data.replace("Synthetic", "Edited"))
        self.assertEqual(advance_snapshot([edited], twice).series_previous, twice.series_previous)

    def test_history_lives_while_uid_exists_even_if_only_override_remains(self):
        first, second = resource(event()), resource(event(start="20261018T120000Z"))
        changed = advance_snapshot([second], Snapshot([first], []))
        override = resource(event(start="20261020T120000Z", recurring=False, rid="20261016T120000Z"))
        detached_only = advance_snapshot([override], changed)
        self.assertEqual(detached_only.series_previous, [first])
        self.assertEqual(advance_snapshot([], detached_only).series_previous, [])

    def test_source_and_calendar_are_part_of_history_identity(self):
        old = resource(event())
        changed = advance_snapshot([resource(event(start="20261018T120000Z"))], Snapshot([old], []))
        for unrelated in (replace(changed.current[0], source_id="mailru_1"), replace(changed.current[0], calendar_id="other")):
            self.assertEqual(advance_snapshot([unrelated], changed).series_previous, [])

    def test_recurrence_transitions_with_changed_anchor_capture_old_version(self):
        for before_recurring, after_recurring in ((True, False), (False, True), (True, True)):
            with self.subTest(before=before_recurring, after=after_recurring):
                old = resource(event(recurring=before_recurring))
                current = resource(event(start="20261018T120000Z", recurring=after_recurring))
                self.assertEqual(advance_snapshot([current], Snapshot([old], [])).series_previous, [old])
        old = resource(event(recurring=False))
        current = resource(event(start="20261018T120000Z", recurring=False))
        result = advance_snapshot([current], Snapshot([old], []))
        self.assertEqual(result.series_previous, [])
        self.assertEqual(result.previous, [old])

    def test_all_day_to_timed_is_change_but_equivalent_timezone_is_not(self):
        all_day = resource(event(start="20261015").replace("DTSTART:", "DTSTART;VALUE=DATE:"))
        timed = resource(event())
        self.assertEqual(advance_snapshot([timed], Snapshot([all_day], [])).series_previous, [all_day])
        equivalent = resource(event(start="20261015T150000").replace("DTSTART:", "DTSTART;TZID=Europe/Moscow:"))
        self.assertEqual(advance_snapshot([equivalent], Snapshot([timed], [])).series_previous, [])

    def test_aggregate_resource_is_scoped_for_independent_series_histories(self):
        old = resource(event("a"), event("b"))
        moved_a = resource(event("a", "20261016T120000Z"), event("b"))
        moved_b = resource(event("a", "20261016T120000Z"), event("b", "20261017T120000Z"))
        once = advance_snapshot([moved_a], Snapshot([old], []))
        self.assertEqual(identities(once.series_previous), [{"a"}])
        twice = advance_snapshot([moved_b], once)
        self.assertCountEqual(identities(twice.series_previous), [{"a"}, {"b"}])
        for historical in twice.series_previous:
            component, = Calendar.from_ical(historical.data).walk("VEVENT")
            self.assertEqual(component.decoded("DTSTART").day, 15)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_snapshot(path, twice)
            self.assertCountEqual(identities(read_snapshot(path).series_previous), [{"a"}, {"b"}])

    def test_aggregate_projection_preserves_timezone_alarms_and_duration_semantics(self):
        zone = "\r\n".join([
            "BEGIN:VTIMEZONE", "TZID:Custom/HistoryTest", "BEGIN:STANDARD",
            "DTSTART:19700101T000000", "TZOFFSETFROM:+0300", "TZOFFSETTO:+0300",
            "END:STANDARD", "END:VTIMEZONE",
        ])
        exact = event("exact", "20261024T120000", extra="DURATION:PT24H\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Synthetic alarm\r\nTRIGGER:-PT15M\r\nEND:VALARM")
        nominal = event("nominal", "20261024T120000", extra="DURATION:P1D")
        old = resource(zone, exact.replace("DTSTART:", "DTSTART;TZID=Europe/Berlin:"),
                       nominal.replace("DTSTART:", "DTSTART;TZID=Europe/Berlin:"))
        current = replace(old, data=old.data.replace("20261024T120000", "20261026T120000"))
        result = advance_snapshot([current], Snapshot([old], []))
        by_uid = {next(iter(identities([item])[0])): item for item in result.series_previous}
        self.assertIn("DURATION:PT24H", by_uid["exact"].data)
        self.assertIn("DURATION:P1D", by_uid["nominal"].data)
        self.assertIn("TZID:Custom/HistoryTest", by_uid["exact"].data)
        self.assertEqual(len(Calendar.from_ical(by_uid["exact"].data).walk("VALARM")), 1)
        # Actual old intervals still distinguish 24 elapsed hours from a local day.
        exact_event = next(item for item in select_day([by_uid["exact"]], date(2026, 10, 25)) if item.start.startswith("2026-10-24"))
        nominal_event = next(item for item in select_day([by_uid["nominal"]], date(2026, 10, 25)) if item.start.startswith("2026-10-24"))
        self.assertEqual(exact_event.end, "2026-10-25T13:00:00+03:00")
        self.assertEqual(nominal_event.end, "2026-10-25T14:00:00+03:00")

    def test_phase_cache_parses_each_unique_ordinary_resource_once(self):
        old = resource(event())
        current = resource(event(start="20261018T120000Z"))
        override = resource(event(start="20261016T140000Z", recurring=False, rid="20261016T120000Z"))
        with patch.object(local_state.Calendar, "from_ical", wraps=Calendar.from_ical) as parser:
            advance_snapshot([current, override], Snapshot([old, override], [old]))
        self.assertEqual(parser.call_count, 3)

    def test_v1_history_is_not_fabricated_from_mixed_previous_resources(self):
        old = resource(event())
        current = resource(event(start="20261018T120000Z"))
        payload = {"schema_version": 1, "current": [encoded(current)], "previous": [encoded(old)]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(payload))
            legacy = read_snapshot(path)
            self.assertEqual(legacy.series_previous, [])
            unchanged = advance_snapshot([current], legacy)
            self.assertEqual(unchanged.series_previous, [])
            self.assertEqual(unchanged.previous[0].data, old.data)
            later = advance_snapshot([resource(event(start="20261020T120000Z"))], unchanged)
            self.assertEqual([item.data for item in later.series_previous], [current.data])
            write_snapshot(path, later)
            self.assertEqual(json.loads(path.read_text())["schema_version"], 2)

    def test_schema2_rejects_missing_unknown_duplicate_or_ambiguous_fields(self):
        valid = json.loads(snapshot_json(Snapshot([], [])))
        invalid = [
            {key: value for key, value in valid.items() if key != "series_previous"},
            {**valid, "schema_version": 1},
            {**valid, "schema_version": 2.0},
            {**valid, "extra": []},
            {**valid, "series_previous": [encoded(resource(event("a"), event("b")))]},
            {**valid, "series_previous": [encoded(resource(event(recurring=False, rid="20261015T120000Z")))]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for payload in invalid:
                with self.subTest(payload_keys=payload.keys()):
                    original = json.dumps(payload)
                    path.write_text(original)
                    with self.assertRaises(StateError):
                        read_snapshot(path)
                    self.assertEqual(path.read_text(), original)
            path.write_text('{"schema_version":2,"current":[],"previous":[],"series_previous":[],"series_previous":[]}')
            with self.assertRaises(StateError):
                read_snapshot(path)

    def test_conflicting_resources_and_history_limits_fail_without_truncation(self):
        old = resource(event())
        different = replace(old, data=old.data.replace("Synthetic", "Different"))
        with self.assertRaises(StateError):
            advance_snapshot([old, different], Snapshot([], []))
        with patch.object(local_state, "MAX_SERIES_HISTORY_BYTES", 1), self.assertRaises(StateError):
            advance_snapshot([resource(event(start="20261018T120000Z"))], Snapshot([old], []))

    def test_oversized_write_preserves_previous_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_snapshot(path, Snapshot([], []))
            original = path.read_bytes()
            with patch.object(local_state, "MAX_SNAPSHOT_BYTES", 1), self.assertRaises(StateError):
                write_snapshot(path, Snapshot([resource(event())], []))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob(".digest-*")), [])


if __name__ == "__main__":
    unittest.main()
