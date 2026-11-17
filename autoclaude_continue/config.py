"""JSON configuration for Auto Continue."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path


APP_NAME = "AutoClaudeContinue"
DEFAULT_AUMID = "Claude_pzs8sxrjxfjjc!Claude"
DEFAULT_CHATGPT_AUMID = "OpenAI.Codex_2p2nqsd0c76g0!App"

APP_TARGETS = ("claude", "chatgpt", "auto")
THEMES = ("dark", "light")
UI_SCALES = (90, 100, 110, 125)


def default_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / "AppData" / "Roaming" / APP_NAME


@dataclass(frozen=True)
class Config:
    detection_enabled: bool = True
    app_target: str = "claude"
    claude_dev_mode: bool = True
    theme: str = "dark"
    ui_scale: int = 100
    chat_name: str = ""
    poll_seconds: int = 900
    wake_early_seconds: int = 10
    start_with_windows: bool = False
    minimize_to_tray: bool = True
    launch_if_missing: bool = False
    message: str = "continue"
    saved_messages: tuple[str, ...] = ("continue",)
    attachments: tuple[str, ...] = ()
    preserve_drafts: bool = True
    defer_when_active: bool = True
    active_grace_seconds: int = 20
    pause_when_locked: bool = True
    claude_aumid: str = DEFAULT_AUMID
    chatgpt_aumid: str = DEFAULT_CHATGPT_AUMID

    def validated(self) -> "Config":
        poll = _clamp(int(self.poll_seconds), 60, 86400)
        wake = _clamp(int(self.wake_early_seconds), 0, 300)
        grace = _clamp(int(self.active_grace_seconds), 0, 3600)
        message = (self.message or "").strip() or "continue"
        chat = (self.chat_name or "").strip()
        target = (self.app_target or "claude").strip().lower()
        if target not in APP_TARGETS:
            target = "claude"
        theme = (self.theme or "dark").strip().lower()
        if theme not in THEMES:
            theme = "dark"
        try:
            ui_scale = int(self.ui_scale)
        except (TypeError, ValueError):
            ui_scale = 100
        if ui_scale not in UI_SCALES:
            ui_scale = 100
        saved = tuple(
            dict.fromkeys(
                item.strip() for item in (self.saved_messages or ()) if item and item.strip()
            )
        )
        if not saved:
            saved = ("continue",)
        attachments = tuple(
            dict.fromkeys(str(p) for p in (self.attachments or ()) if str(p).strip())
        )
        return replace(
            self,
            detection_enabled=bool(self.detection_enabled),
            app_target=target,
            claude_dev_mode=bool(self.claude_dev_mode),
            theme=theme,
            ui_scale=ui_scale,
            chat_name=chat,
            poll_seconds=poll,
            wake_early_seconds=wake,
            start_with_windows=bool(self.start_with_windows),
            minimize_to_tray=bool(self.minimize_to_tray),
            launch_if_missing=bool(self.launch_if_missing),
            message=message,
            saved_messages=saved,
            attachments=attachments,
            preserve_drafts=bool(self.preserve_drafts),
            defer_when_active=bool(self.defer_when_active),
            active_grace_seconds=grace,
            pause_when_locked=bool(self.pause_when_locked),
            claude_aumid=(self.claude_aumid or "").strip() or DEFAULT_AUMID,
            chatgpt_aumid=(self.chatgpt_aumid or "").strip() or DEFAULT_CHATGPT_AUMID,
        )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _prune_autosaved_presets(
    saved: tuple[str, ...],
    message: str,
) -> tuple[str, ...]:
    """Drop the partial messages left by the old autosave behaviour.

    The previous build appended the composer contents to ``saved_messages`` on
    every debounced keystroke, producing a chain of ever-longer variants that
    all share the beginning of the same draft.  Collapse each such chain to
    its longest entry (or the exact current message) and keep short presets
    such as ``continue`` untouched.
    """

    unique = list(
        dict.fromkeys(
            str(item).strip() for item in saved if str(item).strip()
        )
    )
    if len(unique) < 3:
        return tuple(unique)

    groups: dict[str, list[str]] = {}
    for text in unique:
        if len(text) <= 30:
            continue
        groups.setdefault(text.lower()[:40], []).append(text)

    drop: set[str] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        keep = message if message in group else max(group, key=len)
        drop.update(text for text in group if text != keep)

    kept = [text for text in unique if text not in drop]
    return tuple(kept) or ((message,) if message else ("continue",))


class ConfigStore:
    """Thread-safe config store backed by a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (default_data_dir() / "config.json")
        self._lock = threading.RLock()
        self._config = self._load()

    def get(self) -> Config:
        with self._lock:
            return self._config

    def update(self, **changes) -> Config:
        with self._lock:
            current = asdict(self._config)
            current.update({k: v for k, v in changes.items() if k in current})
            new_config = Config(**current).validated()
            self._config = new_config
            self._save(new_config)
            return new_config

    def reload(self) -> Config:
        with self._lock:
            self._config = self._load()
            return self._config

    def _load(self) -> Config:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            return Config().validated()

        if not isinstance(data, dict):
            return Config().validated()

        known = {f.name for f in fields(Config)}
        values = {k: v for k, v in data.items() if k in known}
        changed = False
        for key in ("saved_messages", "attachments"):
            value = values.get(key)
            if isinstance(value, list):
                values[key] = tuple(str(item) for item in value)
        # Migrate the old single-message config.
        if "saved_messages" not in values and "message" in values:
            values["saved_messages"] = (str(values["message"]),)
            changed = True
        # Remove the partial-message chain created by the old autosave bug.
        saved_values = values.get("saved_messages")
        if isinstance(saved_values, tuple):
            prefix_counts: dict[str, int] = {}
            for item in saved_values:
                text = str(item).strip()
                if len(text) > 30:
                    key = text.lower()[:40]
                    prefix_counts[key] = prefix_counts.get(key, 0) + 1
            if len(saved_values) >= 5 and any(
                count >= 3 for count in prefix_counts.values()
            ):
                pruned = _prune_autosaved_presets(
                    saved_values, str(values.get("message", "")).strip()
                )
                if pruned != saved_values:
                    values["saved_messages"] = pruned
                    changed = True
        # The old default was a 20-second check.  Values below the new
        # one-minute minimum were never intentional, so restore the current
        # fifteen-minute default instead of clamping them to 60 seconds.
        try:
            if int(values.get("poll_seconds", 900)) < 60:
                values["poll_seconds"] = 900
                changed = True
        except (TypeError, ValueError):
            pass
        try:
            config = Config(**values).validated()
        except (TypeError, ValueError):
            return Config().validated()
        if changed:
            self._save(config)
        return config

    def _save(self, config: Config) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError:
            # A read-only profile or full disk should not crash the app.
            pass
