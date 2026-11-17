"""Optional system-tray icon.

pystray is imported lazily so the GUI still works if the dependency is missing.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

from .resources import logo_path

log = logging.getLogger(__name__)

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover - depends on optional packages
    pystray = None
    Image = None
    ImageDraw = None


def _square_logo(size: int = 64):
    path = logo_path()
    if Image is None:
        return None
    if path is not None:
        try:
            with Image.open(path) as source:
                image = source.convert("RGBA")
                side = min(image.size)
                left = (image.width - side) // 2
                top = (image.height - side) // 2
                image = image.crop((left, top, left + side, top + side))
                image = image.resize((size, size), Image.Resampling.LANCZOS)
                mask = Image.new("L", (size, size), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
                image.putalpha(mask)
                return image
        except Exception:
            log.debug("Could not load the tray logo", exc_info=True)

    # Fallback: a simple continue mark.
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, size - 3, size - 3), fill="#4F8CFF")
    draw.polygon(
        [(size * 0.36, size * 0.28), (size * 0.74, size * 0.5), (size * 0.36, size * 0.72)],
        fill="white",
    )
    return image


class TrayIcon:
    def __init__(
        self,
        ui_queue: queue.Queue,
        get_detection: Callable[[], bool],
        *,
        app_name: str = "AutoClaudeContinue",
    ) -> None:
        self.ui_queue = ui_queue
        self.get_detection = get_detection
        self.app_name = app_name
        self._icon = None
        self._thread: threading.Thread | None = None
        self._notified = False

    @property
    def available(self) -> bool:
        return pystray is not None and Image is not None

    def start(self) -> None:
        if not self.available or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="TrayIcon", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            icon_image = _square_logo()
            if icon_image is None:
                return
            menu = pystray.Menu(
                pystray.MenuItem(
                    "Open AutoClaudeContinue",
                    lambda item: self.ui_queue.put(("show", None)),
                    default=True,
                ),
                pystray.MenuItem(
                    "Send now",
                    lambda item: self.ui_queue.put(("check", None)),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Detection enabled",
                    lambda item: self.ui_queue.put(("toggle", None)),
                    checked=lambda item: self.get_detection(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Quit",
                    lambda item: self.ui_queue.put(("quit", None)),
                ),
            )
            self._icon = pystray.Icon(
                "AutoClaudeContinue",
                icon_image,
                self.app_name,
                menu,
            )
            self._icon.run()
        except Exception:
            log.exception("Could not start the tray icon")

    def notify_running_in_tray(self) -> None:
        if self._icon is None or self._notified:
            return
        self._notified = True
        try:
            self._icon.notify(
                "Still running in the tray. Use the tray menu to quit.",
                self.app_name,
            )
        except Exception:
            log.debug("Tray notification failed", exc_info=True)

    def stop(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                log.debug("Could not stop the tray icon", exc_info=True)
