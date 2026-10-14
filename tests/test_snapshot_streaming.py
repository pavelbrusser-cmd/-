from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calendar_digest.local_state import (
    Snapshot, StateError, advance_snapshot, read_snapshot, write_snapshot,
)
from calendar_digest.selection import select_day
from test_preview import resource


def encoded(value):
    return {name: getattr(value, name) for name in
            ("source_id", "calendar_id", "calendar_name", "data")}


class StreamingSnapshotTests(unittest.TestCase):
    def test_legacy_snapshot_with_duplicate_history_and_large_unicode_roundtrips(self):
        value = resource(title="Синтетическая заметка 🎵" * 5000)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"previous": [encoded(value)],
                                       "current": [encoded(value)], "schema_version": 1},
                                      ensure_ascii=False), encoding="utf-8")
            restored = read_snapshot(path)
            self.assertEqual(restored.current[0].data, value.data)
            self.assertEqual(restored.previous[0].data, value.data)
            self.assertEqual(restored.current[0].account_email, "")
            write_snapshot(path, restored)
            compact = read_snapshot(path)
            self.assertEqual(compact.current[0].data, value.data)
            self.assertEqual(compact.previous, [])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(value.account_email, path.read_text())

    def test_compaction_preserves_known_move_and_detects_later_move_after_restart(self):
        first = resource()
        moved = resource("20261015T110000Z")
        unchanged = replace(resource(), calendar_id="other-calendar")
        snapshot = Snapshot([moved, unchanged], [first, unchanged])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_snapshot(path, snapshot)
            restored = read_snapshot(path)
            self.assertEqual(len(restored.previous), 1)
            self.assertEqual(select_day(snapshot.current, date(2026, 10, 14), snapshot.previous),
                             select_day(restored.current, date(2026, 10, 14), restored.previous))
            later = replace(resource("20261016T120000Z"), calendar_id="other-calendar")
            advanced = advance_snapshot([moved, later], restored)
            events = select_day(advanced.current, date(2026, 10, 14), advanced.previous)
            self.assertEqual(len(events), 2)
            self.assertEqual({event.original_start for event in events}, {"2026-10-14T12:00:00+03:00"})

    def test_failed_streamed_write_preserves_old_baseline_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_bytes(b"original baseline")
            def fail(payload, stream, **kwargs):
                stream.write('{"incomplete":')
                raise OSError("synthetic disk failure")
            with patch("calendar_digest.local_state.json.dump", side_effect=fail):
                with self.assertRaises(OSError):
                    write_snapshot(path, Snapshot([resource()], []))
            self.assertEqual(path.read_bytes(), b"original baseline")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_malformed_structure_never_replaces_baseline_or_exposes_values(self):
        private = "synthetic-private-content"
        cases = (
            '{"schema_version":1,"current":[],"previous":[]',
            '{"schema_version":1,"current":[],"previous":[]} trailing',
            '{"schema_version":true,"current":[],"previous":[]}',
            '{"schema_version":1,"current":[],"current":[],"previous":[]}',
            '{"schema_version":1,"current":{},"previous":[]}',
            json.dumps({"schema_version":1, "current":[{"data":private}], "previous":[]}),
            json.dumps({"schema_version":1, "current":[dict(encoded(resource()), data=None)], "previous":[]}),
            json.dumps({"schema_version":1, "current":[dict(encoded(resource()), password=private)], "previous":[]}),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for text in cases:
                with self.subTest(case=cases.index(text)):
                    path.write_text(text)
                    before = path.read_bytes()
                    with self.assertRaises(StateError) as error:
                        read_snapshot(path)
                    self.assertNotIn(private, str(error.exception))
                    self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
