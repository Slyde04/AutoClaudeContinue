from __future__ import annotations

import unittest

from autoclaude_continue.resources import license_path


class LicenseResourceTests(unittest.TestCase):
    def test_license_file_is_available(self):
        path = license_path()
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())

    def test_license_is_proprietary_and_forbids_redistribution(self):
        path = license_path()
        self.assertIsNotNone(path)
        text = path.read_text(encoding="utf-8").lower()
        self.assertIn("end user license agreement", text)
        self.assertIn("kutaos", text)
        self.assertIn("licensed, not sold", text)
        self.assertIn("distribute", text)
        self.assertIn("reverse engineer", text)


if __name__ == "__main__":
    unittest.main()
