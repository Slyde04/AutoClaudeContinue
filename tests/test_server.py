from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from autoclaude_continue import __version__, server
from autoclaude_continue.config import Config, ConfigStore
from autoclaude_continue.server import ApiHandler


class StubHub:
    def __init__(self) -> None:
        self.config = Config()
        self.logs: list[str] = []

    def snapshot(self) -> dict:
        return {
            "version": __version__,
            "config": asdict(self.config),
            "state": {"state": "idle"},
            "activity": [],
            "chats": [],
            "chat_app": "",
            "chat_app_label": "",
        }

    def log(self, message: str, level: str = "info") -> None:
        self.logs.append(message)

    def config_changed(self, config=None) -> None:
        if config is not None:
            self.config = config


class StubAutomator:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def submit(self, command: str, **kwargs) -> None:
        self.commands.append(command)


class StubBackend:
    def __init__(self) -> None:
        self.hub = StubHub()
        self.automator = StubAutomator()
        self.stopped = False

    def update_config(self, changes: dict) -> Config:
        current = asdict(self.hub.config)
        current.update({key: value for key, value in changes.items() if key in current})
        self.hub.config = Config(**current).validated()
        return self.hub.config

    def send_now(self, message: str | None = None) -> Config:
        if message:
            self.hub.config = self.update_config({"message": message})
        self.automator.submit("check_now")
        return self.hub.config

    def stop(self) -> None:
        self.stopped = True


class ServerTests(unittest.TestCase):
    def start(self, token: str = "") -> None:
        from http.server import ThreadingHTTPServer

        backend = StubBackend()
        handler = type(
            "TestApiHandler",
            (ApiHandler,),
            {"backend": backend, "token": token, "ui_dir": None},
        )
        self.backend = backend
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        if getattr(self, "server", None) is not None:
            self.server.shutdown()
            self.server.server_close()

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        token: str | None = None,
    ) -> tuple[int, dict | str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if token is not None:
            headers["X-ACC-Token"] = token
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        connection.close()
        try:
            return response.status, json.loads(raw)
        except json.JSONDecodeError:
            return response.status, raw

    def test_health_and_bootstrap_are_available_without_a_token_in_preview_mode(self):
        self.start()
        status, payload = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], __version__)

        status, snapshot = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["config"]["poll_seconds"], 900)
        self.assertEqual(snapshot["state"]["state"], "idle")

    def test_a_token_is_required_when_configured(self):
        self.start(token="secret")
        status, payload = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "unauthorized")

        status, _ = self.request("GET", "/api/bootstrap", token="wrong")
        self.assertEqual(status, 401)

        status, snapshot = self.request("GET", "/api/bootstrap", token="secret")
        self.assertEqual(status, 200)
        self.assertIn("config", snapshot)

        status, _ = self.request("GET", "/api/health?token=secret")
        self.assertEqual(status, 200)

    def test_config_updates_are_saved_and_returned(self):
        self.start()
        status, payload = self.request(
            "POST",
            "/api/config",
            {"theme": "light", "poll_seconds": 1800, "claude_dev_mode": False},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["config"]["theme"], "light")
        self.assertEqual(payload["config"]["poll_seconds"], 1800)
        self.assertFalse(payload["config"]["claude_dev_mode"])
        self.assertEqual(self.backend.hub.config.theme, "light")

    def test_detection_and_send_commands_reach_the_backend(self):
        self.start()
        status, payload = self.request("POST", "/api/detection", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(payload["config"]["detection_enabled"])
        self.assertIn("Detection paused.", self.backend.hub.logs)

        status, _ = self.request("POST", "/api/chats/refresh", {})
        self.assertEqual(status, 200)
        self.assertEqual(self.backend.automator.commands, ["refresh_chats"])

        status, payload = self.request(
            "POST",
            "/api/send",
            {"message": "still there?"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["config"]["message"], "still there?")
        self.assertIn("check_now", self.backend.automator.commands)

    def test_shutdown_asks_the_backend_to_stop(self):
        self.start()
        status, payload = self.request("POST", "/api/shutdown", {})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        deadline = time.monotonic() + 2
        while not self.backend.stopped and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.backend.stopped)

    def test_open_url_rejects_non_http_targets(self):
        self.start()
        status, payload = self.request(
            "POST",
            "/api/open-url",
            {"url": "file:///C:/Windows/System32/calc.exe"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid url")


class AttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.backend = server.Backend(ConfigStore(root / "config.json"))
        self.image = root / "one.png"
        from PIL import Image

        Image.new("RGB", (8, 8), "red").save(self.image)

    def test_adding_and_removing_images_updates_the_config(self):
        result = self.backend.add_attachments([str(self.image)])
        self.assertEqual(result["added"], 1)
        self.assertEqual(self.backend.store.get().attachments, (str(self.image),))

        result = self.backend.remove_attachment(str(self.image))
        self.assertEqual(result["attachments"], [])
        self.assertEqual(self.backend.store.get().attachments, ())

    def test_pasting_uses_the_saved_clipboard_image(self):
        with mock.patch.object(
            server, "save_clipboard_images", return_value=[str(self.image)]
        ):
            result = self.backend.paste_attachments()
        self.assertEqual(result["added"], 1)
        self.assertEqual(self.backend.store.get().attachments, (str(self.image),))


if __name__ == "__main__":
    unittest.main()
