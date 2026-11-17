"""Command-line entry point, including a UI diagnostic mode."""

from __future__ import annotations

import sys

import uiautomation as auto

from .app import main as run_app
from .apps import ALL_ADAPTERS, CHATGPT, CLAUDE
from .rate_limit import format_reset_time


def _diagnose_adapter(adapter) -> int:
    print(f"=== {adapter.display_name} ===")
    window = adapter.find_window()
    if window is None:
        print(f"{adapter.display_name} window: NOT FOUND")
        return 1
    print(f"{adapter.display_name} window: found (HWND {window.NativeWindowHandle})")

    ready = adapter.has_tree(window)
    print(f"UIA tree ready: {ready}")
    if not ready:
        print("Warming up UIA by focusing the app briefly (diagnostic only)...")
        adapter.warm_up(window)
        ready = adapter.has_tree(window)
        print(f"UIA tree ready after warm-up: {ready}")
    if not ready:
        print(f"{adapter.display_name}'s accessibility tree is still blank.")
        return 2

    snapshot = adapter.scan_window(window)
    print(f"Composer present: {snapshot.composer_present}")
    print(f"Modal open: {snapshot.modal_open}")
    print(f"Chat title: {snapshot.chat_title!r}")
    if snapshot.limit:
        print(f"Rate limit detected: {snapshot.limit.raw!r}")
        if snapshot.limit.reset_at:
            print(
                "Reset time:",
                snapshot.limit.reset_at.strftime("%Y-%m-%d %H:%M:%S"),
                f"({format_reset_time(snapshot.limit.reset_at)})",
            )
        else:
            print("Reset time: could not parse")
    else:
        print("Rate limit: none detected")

    chats = adapter.list_sidebar_chats(window)
    print(f"Visible sidebar chats ({len(chats)}):")
    for chat in chats:
        print("  -", chat)
    return 0


def diagnose(target: str = "all") -> int:
    print("AutoClaudeContinue diagnostic")
    print("=" * 40)
    target = (target or "all").strip().lower()
    if target == "claude":
        adapters = (CLAUDE,)
    elif target == "chatgpt":
        adapters = (CHATGPT,)
    else:
        adapters = ALL_ADAPTERS

    results: list[int] = []
    with auto.UIAutomationInitializerInThread():
        for adapter in adapters:
            results.append(_diagnose_adapter(adapter))
            print()
    return 0 if any(result == 0 for result in results) else 2


def cli() -> int:
    args = sys.argv[1:]
    if "--diagnose" in args:
        index = args.index("--diagnose")
        target = args[index + 1] if len(args) > index + 1 else "all"
        return diagnose(target)
    if "--version" in args:
        from . import __version__

        print(__version__)
        return 0
    return run_app()


if __name__ == "__main__":
    raise SystemExit(cli())
