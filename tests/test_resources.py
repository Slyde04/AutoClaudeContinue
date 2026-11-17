from __future__ import annotations

import unittest

from autoclaude_continue.resources import license_path


class LicenseResourceTests(unittest.TestCase):
    def test_license_file_is_available(self):
        path = license_path()
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())

    def test_license_allows_noncommercial_redistribution_with_attribution(self):
        path = license_path()
        self.assertIsNotNone(path)
        text = path.read_text(encoding="utf-8").lower()
        self.assertIn("noncommercial source-available license", text)
        self.assertIn("kutaos", text)
        self.assertIn("noncommercial purpose", text)
        self.assertIn("redistribute", text)
        self.assertIn("attribution", text)
        self.assertIn("you may not sell", text)


if __name__ == "__main__":
    unittest.main()
