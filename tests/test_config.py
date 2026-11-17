from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoclaude_continue.config import ConfigStore


class ConfigStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_old_poll_interval_migrates_to_one_hour(self):
        self.write({"poll_seconds": 20, "message": "continue"})
        store = ConfigStore(self.path)
        self.assertEqual(store.get().poll_seconds, 900)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["poll_seconds"], 900)

    def test_valid_poll_interval_is_kept(self):
        self.write({"poll_seconds": 1800})
        store = ConfigStore(self.path)
        self.assertEqual(store.get().poll_seconds, 1800)

    def test_unknown_old_keys_are_ignored(self):
        self.write({"auto_continue": False, "poll_seconds": 3600})
        store = ConfigStore(self.path)
        self.assertFalse(hasattr(store.get(), "auto_continue"))
        self.assertTrue(store.get().detection_enabled)

    def test_invalid_target_falls_back_to_claude(self):
        self.write({"app_target": "gemini"})
        store = ConfigStore(self.path)
        self.assertEqual(store.get().app_target, "claude")

    def test_theme_defaults_to_dark(self):
        store = ConfigStore(self.path)
        self.assertEqual(store.get().theme, "dark")

    def test_theme_is_persisted(self):
        store = ConfigStore(self.path)
        store.update(theme="light")
        again = ConfigStore(self.path).get()
        self.assertEqual(again.theme, "light")

    def test_invalid_theme_falls_back_to_dark(self):
        self.write({"theme": "solarized"})
        store = ConfigStore(self.path)
        self.assertEqual(store.get().theme, "dark")

    def test_ui_scale_defaults_to_100(self):
        store = ConfigStore(self.path)
        self.assertEqual(store.get().ui_scale, 100)

    def test_ui_scale_is_persisted(self):
        store = ConfigStore(self.path)
        store.update(ui_scale=110)
        again = ConfigStore(self.path).get()
        self.assertEqual(again.ui_scale, 110)

    def test_invalid_ui_scale_falls_back_to_100(self):
        self.write({"ui_scale": 137})
        store = ConfigStore(self.path)
        self.assertEqual(store.get().ui_scale, 100)

    def test_list_fields_are_tuples_and_legacy_message_becomes_preset(self):
        self.write({"message": "try again", "attachments": ["C:/tmp/a.png"]})
        store = ConfigStore(self.path)
        config = store.get()
        self.assertEqual(config.message, "try again")
        self.assertIn("try again", config.saved_messages)
        self.assertEqual(config.attachments, ("C:/tmp/a.png",))
        self.assertIsInstance(config.saved_messages, tuple)

    def test_update_is_persisted(self):
        store = ConfigStore(self.path)
        store.update(app_target="chatgpt", poll_seconds=120)
        again = ConfigStore(self.path).get()
        self.assertEqual(again.app_target, "chatgpt")
        self.assertEqual(again.poll_seconds, 120)

    def test_editing_the_draft_does_not_add_presets(self):
        self.write({"message": "continue", "saved_messages": ["continue"]})
        store = ConfigStore(self.path)
        store.update(message="please continue with the UI work")
        store.update(message="please continue with the UI work and test it")
        self.assertEqual(store.get().saved_messages, ("continue",))

    def test_autosaved_prefix_chain_is_pruned_on_load(self):
        full = "continue also please remember to make use of the new UI and revamp it."
        self.write(
            {
                "message": full,
                "saved_messages": [
                    "continue",
                    "continue also please remember to make use",
                    "continue also please remember to make use of the galaxy repo",
                    "continue also please remember to make use of the new UI",
                    "continue also please remember to make use of the new UI and revamp",
                    full,
                ],
            }
        )
        saved = ConfigStore(self.path).get().saved_messages
        self.assertIn("continue", saved)
        self.assertIn(full, saved)
        self.assertNotIn("continue also please remember to make use", saved)
        self.assertNotIn("continue also please remember to make use of the galaxy repo", saved)
        self.assertNotIn(
            "continue also please remember to make use of the new UI", saved
        )
        self.assertLessEqual(len(saved), 2)


if __name__ == "__main__":
    unittest.main()
