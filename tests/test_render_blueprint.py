"""Blueprint updates must not turn off schedules already enabled by the owner."""

from pathlib import Path
import re
import unittest


class RenderBlueprintTests(unittest.TestCase):
    def test_operational_switches_have_no_blueprint_managed_values(self):
        # This guard covers the operational contract with Render: sync:false
        # fields are prompted on first creation and ignored on later updates.
        text = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
        blocks = re.findall(r"(?m)^      - key: ([A-Z_0-9]+)\n((?:        [^\n]*\n)*)", text)
        for name in ("DIGEST_SCHEDULE_ENABLED", "PUSH_SCHEDULE_ENABLED",
                     "CONTROL_EMAIL_ENABLED", "ICLOUD_CONTACT_BIRTHDAYS_ENABLED"):
            with self.subTest(name=name):
                matches = [body for key, body in blocks if key == name]
                self.assertEqual(len(matches), 1)
                properties = {}
                for line in matches[0].splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        key, value = line.split(":", 1)
                        properties[key] = value.strip()
                self.assertEqual(properties, {"sync": "false"})


if __name__ == "__main__":
    unittest.main()
