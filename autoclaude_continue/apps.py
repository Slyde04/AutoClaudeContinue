"""Application adapters for Claude Desktop and the ChatGPT desktop app."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from types import ModuleType

import uiautomation as auto

from . import chatgpt_uia, claude_uia
from .config import Config

log = logging.getLogger(__name__)

APP_TARGETS = ("claude", "chatgpt", "auto")
_TARGET_LABELS = {
    "claude": "Claude",
    "chatgpt": "ChatGPT",
    "auto": "Automatic",
}


@dataclass(frozen=True)
class AppAdapter:
    key: str
    display_name: str
    module: ModuleType
    aumid_field: str

    def find_window(self) -> auto.Control | None:
        return self.module.find_window()

    def has_tree(self, window: auto.Control) -> bool:
        return self.module.has_tree(window)

    def warm_up(self, window: auto.Control, timeout: float = 3.0) -> bool:
        return self.module.warm_up(window, timeout)

    def scan_window(self, window: auto.Control):
        return self.module.scan_window(window)

    def list_sidebar_chats(self, window: auto.Control) -> list[str]:
        return self.module.list_sidebar_chats(window)

    def select_chat(self, window: auto.Control, chat_name: str) -> tuple[bool, str]:
        return self.module.select_chat(window, chat_name)

    def send_message(
        self,
        window: auto.Control,
        message: str,
        attachments: tuple[str, ...] = (),
        preserve_draft: bool = True,
    ):
        return self.module.send_message(
            window,
            message,
            attachments=attachments,
            preserve_draft=preserve_draft,
        )

    def dismiss_modal(self, window: auto.Control, timeout: float = 2.0) -> bool:
        return self.module.dismiss_modal(window, timeout=timeout)

    def launch(self, config: Config) -> bool:
        aumid = getattr(config, self.aumid_field, "") or ""
        if not aumid:
            return False
        try:
            subprocess.Popen(
                ["explorer.exe", rf"shell:AppsFolder\{aumid}"],
                close_fds=True,
            )
            log.info("Launching %s (%s)", self.display_name, aumid)
            return True
        except Exception:
            log.exception("Could not launch %s", self.display_name)
            return False


CLAUDE = AppAdapter("claude", "Claude", claude_uia, "claude_aumid")
CHATGPT = AppAdapter("chatgpt", "ChatGPT", chatgpt_uia, "chatgpt_aumid")
ALL_ADAPTERS = (CLAUDE, CHATGPT)


def adapter_for(key: str) -> AppAdapter:
    key = (key or "claude").strip().lower()
    if key == "chatgpt":
        return CHATGPT
    return CLAUDE


def candidates_for(config: Config) -> tuple[AppAdapter, ...]:
    """Return the adapters to inspect, in priority order."""

    target = (config.app_target or "claude").strip().lower()
    if target == "chatgpt":
        return (CHATGPT,)
    if target == "auto":
        return ALL_ADAPTERS
    return (CLAUDE,)


def target_label(key: str) -> str:
    return _TARGET_LABELS.get((key or "").strip().lower(), "Claude")
