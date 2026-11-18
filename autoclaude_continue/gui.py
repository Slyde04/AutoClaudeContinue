"""CustomTkinter interface for AutoClaudeContinue."""

from __future__ import annotations

import logging
import os
import queue
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageDraw

from . import __version__
from .attachments import (
    IMAGE_EXTENSIONS,
    MAX_ATTACHMENTS,
    format_size,
    is_image,
    save_clipboard_images,
)
from .automator import Automator
from .autostart import is_enabled as startup_is_enabled
from .autostart import set_enabled as set_startup_enabled
from .config import Config, ConfigStore, UI_SCALES, default_data_dir
from .resources import (
    FONT_FAMILY,
    discord_logo_path,
    icon_path,
    license_path,
    logo_path,
    register_fonts,
    youtube_logo_path,
)
from .tray import TrayIcon

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

Color = str | tuple[str, str]

BG = ("#F5F7FB", "#0E1014")
SIDEBAR = ("#E9EEF6", "#12151B")
CARD = ("#FFFFFF", "#171B23")
CARD_ALT = ("#F2F5FA", "#1C212B")
BORDER = ("#D7DEEA", "#252B37")
BORDER_SOFT = ("#E3E8F0", "#1F2530")
TEXT = ("#1A2230", "#ECEEF3")
MUTED = ("#5A6476", "#8B93A7")
FAINT = ("#7A8496", "#5E6678")
ACCENT = ("#2F6FE4", "#4F8CFF")
ACCENT_HOVER = ("#245CC2", "#3D78E8")
ACCENT_SOFT = ("#E3ECFC", "#1B2942")
GREEN = ("#138A5E", "#39D98A")
ORANGE = ("#B26A00", "#F5A524")
RED = ("#D14343", "#FF6B6B")
BLUE = ("#2468C9", "#5EA8FF")
GRAY = ("#6E7889", "#8B93A7")

NAV_HOVER = ("#DEE5F0", "#1A1F29")
SCROLLBAR_HOVER = ("#BCC6D6", "#333A48")
BUTTON_HOVER = ("#E5EAF3", "#242A36")
GHOST_HOVER = ("#ECEFF5", "#20252F")
ACCENT_SOFT_HOVER = ("#D4E2FB", "#233657")
LINK_HOVER = ("#E7ECF4", "#222834")
ROW_HOVER = ("#E7ECF4", "#2B303C")
SWITCH_OFF = ("#AEB9CA", "#3A404D")
SWITCH_KNOB = ("#DCE3EE", "#ECEEF3")
SWITCH_KNOB_HOVER = ("#E8EEF7", "#FFFFFF")

COLORS: dict[str, Color] = {
    "green": GREEN,
    "orange": ORANGE,
    "red": RED,
    "blue": BLUE,
    "gray": GRAY,
}

STATE_LABELS = {
    "disabled": "Detection paused",
    "waiting": "Waiting for reset",
    "sending": "Sending now",
    "sent": "Message sent",
    "idle": "Monitoring",
    "not_found": "App not found",
    "not_ready": "Not readable",
    "error": "Needs attention",
    "deferred": "Waiting for you",
    "stopped": "Stopped",
}

APP_LABELS = {"claude": "Claude", "chatgpt": "ChatGPT", "auto": "Automatic"}
APP_KEYS = {label: key for key, label in APP_LABELS.items()}

PAGE_META = {
    "home": (
        "Home",
        "Watch for a rate limit, wait it out, then send your message.",
    ),
    "settings": (
        "Settings",
        "Everything the app does, and when it does it.",
    ),
    "misc": (
        "Misc",
        "About the app, its creator, and the license.",
    ),
}

LICENSE_FALLBACK = (
    "AutoClaudeContinue — End User License Agreement\n\n"
    "The license text could not be loaded from this installation.\n\n"
    "AutoClaudeContinue is proprietary software owned by KutaOS. You may use "
    "it yourself, but you may not copy, share, publish, resell, or bundle it, "
    "or reuse its code, design, or assets in another product. The Software is "
    'provided "as is", without warranty.\n\n'
    "Contact KutaOS on Discord for a copy of the full agreement:\n"
    "https://discord.gg/VgqfppxR7G\n"
)


# ---------------------------------------------------------------------------
# Small image helpers
# ---------------------------------------------------------------------------


def _circular_photo(path: Path | None, size: int) -> Image.Image | None:
    if path is None:
        return None
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
        log.debug("Could not prepare image %s", path, exc_info=True)
        return None


def _square_photo(path: Path | None, size: int) -> Image.Image | None:
    if path is None:
        return None
    try:
        with Image.open(path) as source:
            image = source.convert("RGBA")
            side = min(image.size)
            left = (image.width - side) // 2
            top = (image.height - side) // 2
            image = image.crop((left, top, left + side, top + side))
            return image.resize((size, size), Image.Resampling.LANCZOS)
    except Exception:
        log.debug("Could not prepare image %s", path, exc_info=True)
        return None


def _thumbnail(path: str, size: int = 34) -> Image.Image | None:
    try:
        with Image.open(path) as source:
            image = source.convert("RGBA")
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.alpha_composite(
                image,
                ((size - image.width) // 2, (size - image.height) // 2),
            )
            return canvas
    except Exception:
        return None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root: ctk.CTk, store: ConfigStore) -> None:
        self.root = root
        self.store = store
        self.events: queue.Queue = queue.Queue()
        self.ui_queue: queue.Queue = queue.Queue()
        self.automator = Automator(store, self.events)
        self.tray = TrayIcon(
            self.ui_queue,
            get_detection=lambda: self.store.get().detection_enabled,
        )

        self._images: list = []
        self._thumb_refs: list = []
        self._attachment_refs: list = []
        self._loading = True
        self._quitting = False
        self._page = "home"
        self._state = "idle"
        self._status_color = GRAY
        self._wait_target: datetime | None = None
        self._retry_target: datetime | None = None
        self._check_reset_after: str | None = None
        self._message_after: str | None = None
        self._settings_after: str | None = None
        self._chats: list[str] = []
        self._attachments: list[str] = list(store.get().attachments)
        self._license_dialog: ctk.CTkToplevel | None = None
        self._theme = "dark"
        self._ui_scale = 100

        register_fonts()
        self._configure_window()
        self._build_fonts()
        self._build_shell()
        self._build_home()
        self._build_settings()
        self._build_misc()

        self._loading = False
        self._show_page("home")
        self._load_from_config(store.get())
        self._sync_startup()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.report_callback_exception = self._report_callback_exception
        self.root.bind("<Control-v>", self._on_paste)

        self.automator.start()
        self.tray.start()

        self.root.after(150, self._poll_events)
        self.root.after(250, self._poll_tray)
        self.root.after(500, self._tick_clock)

    # ------------------------------------------------------------------
    # Window and fonts
    # ------------------------------------------------------------------

    def _configure_window(self) -> None:
        config = self.store.get()
        self._theme = "light" if config.theme == "light" else "dark"
        self._ui_scale = config.ui_scale
        ctk.set_appearance_mode(self._theme)
        ctk.set_widget_scaling(self._ui_scale / 100)
        ctk.set_window_scaling(1.0)
        self.root.title("AutoClaudeContinue")
        width, height = self._fit_window_size(1080, 780)
        self.root.geometry(self._centered_geometry(width, height))
        min_width, min_height = self._fit_window_size(940, 600)
        self.root.minsize(min_width, min_height)
        self.root.configure(fg_color=BG)

        ico = icon_path()
        if ico is not None:
            try:
                self.root.iconbitmap(default=str(ico))
            except Exception:
                log.debug("Could not set the window icon", exc_info=True)

        logo = logo_path()
        if logo is not None:
            try:
                photo = tk.PhotoImage(file=str(logo))
                self.root.iconphoto(True, photo)
                self._images.append(photo)
            except Exception:
                pass

    def _centered_geometry(self, width: int, height: int) -> str:
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - 20)
        return f"{width}x{height}+{x}+{y}"

    def _fit_window_size(self, base_width: int, base_height: int) -> tuple[int, int]:
        """Keep the window inside the screen, leaving room for the taskbar."""

        scale = self._ui_scale / 100
        screen_w = max(800, self.root.winfo_screenwidth())
        screen_h = max(600, self.root.winfo_screenheight())
        width = min(int(base_width * scale), screen_w - 40)
        height = min(int(base_height * scale), screen_h - 90)
        return max(720, width), max(500, height)

    def _build_fonts(self) -> None:
        try:
            families = set(tkfont.families(self.root))
        except Exception:
            families = set()
        family = FONT_FAMILY if FONT_FAMILY in families else "Segoe UI"
        self.font_family = family
        self.font_body = ctk.CTkFont(family=family, size=13)
        self.font_small = ctk.CTkFont(family=family, size=11)
        self.font_tiny = ctk.CTkFont(family=family, size=10, weight="bold")
        self.font_label = ctk.CTkFont(family=family, size=12)
        self.font_heading = ctk.CTkFont(family=family, size=22, weight="bold")
        self.font_section = ctk.CTkFont(family=family, size=15, weight="bold")
        self.font_status = ctk.CTkFont(family=family, size=20, weight="bold")
        self.font_brand = ctk.CTkFont(family=family, size=17, weight="bold")
        self.font_countdown = ctk.CTkFont(family=family, size=36, weight="bold")
        self.font_nav = ctk.CTkFont(family=family, size=13, weight="bold")
        self.font_button = ctk.CTkFont(family=family, size=12, weight="bold")

    # ------------------------------------------------------------------
    # Shell
    # ------------------------------------------------------------------

    def _build_shell(self) -> None:
        self.root.grid_columnconfigure(0, weight=0, minsize=226)
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(
            self.root,
            width=226,
            corner_radius=0,
            fg_color=SIDEBAR,
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(2, weight=1)

        brand = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=18, pady=(22, 24))
        brand.grid_columnconfigure(1, weight=1)

        logo = _circular_photo(logo_path(), 44)
        if logo is not None:
            image = ctk.CTkImage(light_image=logo, dark_image=logo, size=(44, 44))
            self._images.append(image)
            ctk.CTkLabel(brand, image=image, text="", width=44, height=44).grid(
                row=0, column=0, rowspan=2, sticky="w"
            )
        ctk.CTkLabel(
            brand,
            text="AutoClaude",
            font=self.font_brand,
            text_color=TEXT,
            anchor="w",
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ctk.CTkLabel(
            brand,
            text="Continue",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        ).grid(row=1, column=1, sticky="w", padx=(12, 0))

        nav = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        nav.grid(row=1, column=0, sticky="ew", padx=12)
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        for key, label in (("home", "Home"), ("settings", "Settings"), ("misc", "Misc")):
            button = ctk.CTkButton(
                nav,
                text=label,
                height=40,
                corner_radius=10,
                fg_color="transparent",
                hover_color=NAV_HOVER,
                text_color=MUTED,
                font=self.font_nav,
                anchor="w",
                command=lambda page=key: self._show_page(page),
            )
            button.pack(fill="x", pady=2)
            self.nav_buttons[key] = button

        footer = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 18))
        footer.grid_columnconfigure(1, weight=1)

        self.sidebar_dot = ctk.CTkFrame(
            footer,
            width=9,
            height=9,
            corner_radius=5,
            fg_color=GREEN,
        )
        self.sidebar_dot.grid(row=0, column=0, sticky="w", padx=(1, 8))
        self.sidebar_status = ctk.CTkLabel(
            footer,
            text="Monitoring",
            font=self.font_small,
            text_color=MUTED,
            anchor="w",
        )
        self.sidebar_status.grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(
            footer,
            text=f"v{__version__}  ·  by KutaOS",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        main = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(main, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=30, pady=(24, 10))
        header.grid_columnconfigure(0, weight=1)
        self.page_title = ctk.CTkLabel(
            header,
            text="Home",
            font=self.font_heading,
            text_color=TEXT,
            anchor="w",
        )
        self.page_title.grid(row=0, column=0, sticky="w")
        self.page_subtitle = ctk.CTkLabel(
            header,
            text=PAGE_META["home"][1],
            font=self.font_label,
            text_color=MUTED,
            anchor="w",
        )
        self.page_subtitle.grid(row=1, column=0, sticky="w", pady=(2, 0))

        self.page_host = ctk.CTkFrame(main, fg_color=BG, corner_radius=0)
        self.page_host.grid(row=1, column=0, sticky="nsew", padx=30, pady=(0, 26))
        self.page_host.grid_rowconfigure(0, weight=1)
        self.page_host.grid_columnconfigure(0, weight=1)

    # ------------------------------------------------------------------
    # Home
    # ------------------------------------------------------------------

    def _build_home(self) -> None:
        page = ctk.CTkFrame(self.page_host, fg_color=BG, corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)
        self.pages = {"home": page}
        self.home_page = page

        scroll = ctk.CTkScrollableFrame(
            page,
            fg_color="transparent",
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=SCROLLBAR_HOVER,
        )
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        self.home_scroll = scroll

        controls = ctk.CTkFrame(scroll, fg_color="transparent")
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        controls.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            controls,
            text="WATCHING",
            font=self.font_tiny,
            text_color=FAINT,
        ).grid(row=0, column=0, sticky="w", padx=(2, 10))

        self.app_segment_home = ctk.CTkSegmentedButton(
            controls,
            values=["Claude", "ChatGPT", "Automatic"],
            width=300,
            height=32,
            corner_radius=8,
            font=self.font_button,
            selected_color=ACCENT,
            selected_hover_color=ACCENT_HOVER,
            unselected_color=CARD_ALT,
            unselected_hover_color=BUTTON_HOVER,
            text_color=TEXT,
            command=self._on_app_change,
        )
        self.app_segment_home.grid(row=0, column=1, sticky="w")

        right = ctk.CTkFrame(controls, fg_color="transparent")
        right.grid(row=0, column=3, sticky="e")

        self.detection_switch = ctk.CTkSwitch(
            right,
            text="Detection on",
            font=self.font_button,
            text_color=TEXT,
            switch_width=40,
            switch_height=22,
            fg_color=SWITCH_OFF,
            progress_color=ACCENT,
            button_color=SWITCH_KNOB,
            button_hover_color=SWITCH_KNOB_HOVER,
            command=self._on_detection_toggle,
        )
        self.detection_switch.pack(side="left", padx=(0, 12))

        self.check_button = ctk.CTkButton(
            right,
            text="Send now",
            width=104,
            height=32,
            corner_radius=8,
            fg_color=CARD_ALT,
            hover_color=BUTTON_HOVER,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=self.font_button,
            command=self._check_now,
        )
        self.check_button.pack(side="left")

        self._build_status_card(scroll)
        self._build_work_area(scroll)
        self._build_activity(scroll)

    def _fit_home_content(self, _event=None) -> None:
        """Kept as a no-op for older call sites.

        CustomTkinter resizes the scrollable content window itself.  Forcing its
        height on every canvas <Configure> made the scrollbar oscillate while
        the wheel was used, which flashed the whole page.
        """

        return

    def _build_status_card(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(
            parent,
            fg_color=CARD,
            corner_radius=16,
            border_width=1,
            border_color=BORDER,
        )
        card.grid(row=1, column=0, sticky="ew")
        card.grid_columnconfigure(0, weight=1)
        card.grid_columnconfigure(1, weight=0, minsize=280)
        self.status_card = card

        left = ctk.CTkFrame(card, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(22, 12), pady=20)
        left.grid_columnconfigure(0, weight=1)

        pill = ctk.CTkFrame(left, fg_color="transparent")
        pill.grid(row=0, column=0, sticky="w")
        self.status_dot = ctk.CTkFrame(
            pill,
            width=9,
            height=9,
            corner_radius=5,
            fg_color=GREEN,
        )
        self.status_dot.grid(row=0, column=0, padx=(0, 8))
        self.status_state = ctk.CTkLabel(
            pill,
            text="Monitoring",
            font=self.font_small,
            text_color=GREEN,
            anchor="w",
        )
        self.status_state.grid(row=0, column=1, sticky="w")

        self.status_title = ctk.CTkLabel(
            left,
            text="Monitoring Claude",
            font=self.font_status,
            text_color=TEXT,
            anchor="w",
            justify="left",
        )
        self.status_title.grid(row=1, column=0, sticky="w", pady=(10, 4))

        self.status_detail = ctk.CTkLabel(
            left,
            text="No active rate limit. Waiting for one to appear.",
            font=self.font_body,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=560,
        )
        self.status_detail.grid(row=2, column=0, sticky="w")
        card.bind(
            "<Configure>",
            lambda event: self.status_detail.configure(
                wraplength=max(
                    220,
                    int(
                        (event.width - 360)
                        / self.status_detail._get_widget_scaling()
                    ),
                )
            ),
        )

        meta = ctk.CTkFrame(left, fg_color="transparent")
        meta.grid(row=3, column=0, sticky="w", pady=(14, 0))
        self.status_app = ctk.CTkLabel(
            meta,
            text="Claude",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        )
        self.status_app.pack(side="left")
        ctk.CTkLabel(
            meta,
            text="·",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        ).pack(side="left", padx=8)
        self.status_reset = ctk.CTkLabel(
            meta,
            text="No reset scheduled",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        )
        self.status_reset.pack(side="left")

        right = ctk.CTkFrame(card, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 22), pady=20)
        right.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            right,
            text="RATE-LIMIT TIMER",
            font=self.font_tiny,
            text_color=FAINT,
            anchor="e",
        ).grid(row=0, column=0, sticky="e")
        self.countdown_label = ctk.CTkLabel(
            right,
            text="—",
            font=self.font_countdown,
            text_color=TEXT,
            anchor="e",
        )
        self.countdown_label.grid(row=1, column=0, sticky="e", pady=(2, 0))
        self.countdown_caption = ctk.CTkLabel(
            right,
            text="no active limit",
            font=self.font_small,
            text_color=MUTED,
            anchor="e",
        )
        self.countdown_caption.grid(row=2, column=0, sticky="e")

    def _build_work_area(self, parent: ctk.CTkFrame) -> None:
        area = ctk.CTkFrame(parent, fg_color="transparent")
        area.grid(row=2, column=0, sticky="nsew", pady=(14, 0))
        area.grid_columnconfigure(0, weight=1, uniform="work")
        area.grid_columnconfigure(1, weight=1, uniform="work")
        area.grid_rowconfigure(0, weight=1)

        # Message card -----------------------------------------------------
        message_card = ctk.CTkFrame(
            area,
            fg_color=CARD,
            corner_radius=16,
            border_width=1,
            border_color=BORDER,
        )
        message_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        message_card.grid_columnconfigure(0, weight=1)
        message_card.grid_rowconfigure(1, weight=1, minsize=190)

        head = ctk.CTkFrame(message_card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            head,
            text="Message",
            font=self.font_section,
            text_color=TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            head,
            text="Sent when the limit clears",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(1, 0))
        self.preset_menu = ctk.CTkOptionMenu(
            head,
            values=["Presets"],
            width=170,
            height=26,
            corner_radius=8,
            fg_color=CARD_ALT,
            button_color=BORDER,
            button_hover_color=BUTTON_HOVER,
            dropdown_fg_color=CARD_ALT,
            dropdown_hover_color=ACCENT_SOFT,
            dropdown_text_color=TEXT,
            text_color=MUTED,
            font=self.font_small,
            dropdown_font=self.font_small,
            command=self._on_preset_selected,
        )
        self.preset_menu.grid(row=0, column=1, sticky="e")

        self.message_box = ctk.CTkTextbox(
            message_card,
            height=190,
            corner_radius=10,
            border_width=1,
            border_color=BORDER_SOFT,
            fg_color=CARD_ALT,
            text_color=TEXT,
            font=self.font_body,
            wrap="word",
            activate_scrollbars=True,
        )
        self.message_box.grid(row=1, column=0, sticky="nsew", padx=18)
        self.message_box.bind("<KeyRelease>", self._on_message_key)
        self.message_box.bind("<FocusOut>", self._save_message)

        bottom = ctk.CTkFrame(message_card, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=18, pady=(10, 16))
        bottom.grid_columnconfigure(0, weight=1)
        self.message_saved = ctk.CTkLabel(
            bottom,
            text="Draft saves automatically",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        )
        self.message_saved.grid(row=0, column=0, sticky="w")
        self.save_preset_button = ctk.CTkButton(
            bottom,
            text="Save as preset",
            width=112,
            height=28,
            corner_radius=8,
            fg_color="transparent",
            hover_color=GHOST_HOVER,
            border_width=1,
            border_color=BORDER,
            text_color=MUTED,
            font=self.font_small,
            command=self._save_preset,
        )
        self.save_preset_button.grid(row=0, column=1, sticky="e")

        def flow_message_footer(event) -> None:
            needed = (
                self.message_saved.winfo_reqwidth()
                + self.save_preset_button.winfo_reqwidth()
                + 20
            )
            if event.width < needed:
                self.message_saved.grid_configure(
                    row=0, column=0, columnspan=2, sticky="w"
                )
                self.save_preset_button.grid_configure(
                    row=1, column=0, columnspan=2, sticky="e", pady=(6, 0)
                )
            else:
                self.message_saved.grid_configure(
                    row=0, column=0, columnspan=1, sticky="w"
                )
                self.save_preset_button.grid_configure(
                    row=0, column=1, columnspan=1, sticky="e", pady=0
                )

        bottom.bind("<Configure>", flow_message_footer, add="+")

        # Images card ------------------------------------------------------
        images_card = ctk.CTkFrame(
            area,
            fg_color=CARD,
            corner_radius=16,
            border_width=1,
            border_color=BORDER,
        )
        images_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        images_card.grid_columnconfigure(0, weight=1)
        images_card.grid_rowconfigure(1, weight=1)

        image_head = ctk.CTkFrame(images_card, fg_color="transparent")
        image_head.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        image_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            image_head,
            text="Images",
            font=self.font_section,
            text_color=TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            image_head,
            text=f"Optional · up to {MAX_ATTACHMENTS}",
            font=self.font_small,
            text_color=FAINT,
            anchor="e",
        ).grid(row=0, column=1, sticky="e")

        self.empty_state = ctk.CTkFrame(
            images_card,
            fg_color=CARD_ALT,
            corner_radius=10,
            border_width=1,
            border_color=BORDER_SOFT,
        )
        self.empty_state.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=18,
            pady=(0, 8),
        )
        self.empty_state.grid_rowconfigure(0, weight=1)
        self.empty_state.grid_rowconfigure(3, weight=1)
        self.empty_state.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self.empty_state,
            text="+",
            font=ctk.CTkFont(family=self.font_family, size=26),
            text_color=FAINT,
            anchor="center",
        ).grid(row=1, column=0, sticky="s")
        ctk.CTkLabel(
            self.empty_state,
            text="No images attached",
            font=self.font_body,
            text_color=MUTED,
            anchor="center",
        ).grid(row=2, column=0, pady=(4, 0))
        ctk.CTkLabel(
            self.empty_state,
            text="Paste with Ctrl+V, or add files below",
            font=self.font_small,
            text_color=FAINT,
            anchor="center",
        ).grid(row=3, column=0, sticky="n", pady=(2, 0))

        self.attachment_list = ctk.CTkScrollableFrame(
            images_card,
            fg_color="transparent",
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=SCROLLBAR_HOVER,
            height=82,
        )
        self.attachment_list.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=14,
            pady=(0, 6),
        )
        self.attachment_list.grid_remove()

        image_bottom = ctk.CTkFrame(images_card, fg_color="transparent")
        image_bottom.grid(row=2, column=0, sticky="ew", padx=18, pady=(4, 16))
        image_bottom.grid_columnconfigure(0, weight=1)
        self.attachment_hint = ctk.CTkLabel(
            image_bottom,
            text="Images are optional.",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        )
        self.attachment_hint.grid(row=0, column=0, sticky="w")
        self.paste_images_button = ctk.CTkButton(
            image_bottom,
            text="Paste",
            width=72,
            height=28,
            corner_radius=8,
            fg_color=ACCENT_SOFT,
            hover_color=ACCENT_SOFT_HOVER,
            text_color=BLUE,
            font=self.font_small,
            command=self._paste_images,
        )
        self.paste_images_button.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self.add_images_button = ctk.CTkButton(
            image_bottom,
            text="Add images…",
            width=108,
            height=28,
            corner_radius=8,
            fg_color="transparent",
            hover_color=GHOST_HOVER,
            border_width=1,
            border_color=BORDER,
            text_color=MUTED,
            font=self.font_small,
            command=self._add_images,
        )
        self.add_images_button.grid(row=0, column=2, sticky="e")

        def flow_image_footer(event) -> None:
            needed = (
                self.attachment_hint.winfo_reqwidth()
                + self.paste_images_button.winfo_reqwidth()
                + self.add_images_button.winfo_reqwidth()
                + 28
            )
            if event.width < needed:
                self.attachment_hint.grid_configure(
                    row=0, column=0, columnspan=3, sticky="w"
                )
                self.paste_images_button.grid_configure(
                    row=1, column=1, sticky="e", pady=(6, 0)
                )
                self.add_images_button.grid_configure(
                    row=1, column=2, sticky="e", pady=(6, 0)
                )
            else:
                self.attachment_hint.grid_configure(
                    row=0, column=0, columnspan=1, sticky="w"
                )
                self.paste_images_button.grid_configure(
                    row=0, column=1, sticky="e", pady=0
                )
                self.add_images_button.grid_configure(
                    row=0, column=2, sticky="e", pady=0
                )

        image_bottom.bind("<Configure>", flow_image_footer, add="+")

    def _build_activity(self, parent: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(
            parent,
            fg_color=CARD,
            corner_radius=16,
            border_width=1,
            border_color=BORDER,
        )
        card.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        card.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 2))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            head,
            text="Activity",
            font=self.font_label,
            text_color=TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            head,
            text="Clear",
            width=54,
            height=24,
            corner_radius=6,
            fg_color="transparent",
            hover_color=GHOST_HOVER,
            text_color=FAINT,
            font=self.font_small,
            command=self._clear_activity,
        ).grid(row=0, column=1, sticky="e")

        self.activity_box = ctk.CTkTextbox(
            card,
            height=92,
            corner_radius=10,
            fg_color=CARD_ALT,
            border_width=0,
            text_color=MUTED,
            font=self.font_small,
            wrap="word",
            activate_scrollbars=True,
        )
        self.activity_box.grid(row=1, column=0, sticky="ew", padx=18, pady=(2, 14))
        self.activity_box.configure(state="disabled")

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _build_settings(self) -> None:
        page = ctk.CTkFrame(self.page_host, fg_color=BG, corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(0, weight=1)
        page.grid_columnconfigure(0, weight=1)
        self.pages["settings"] = page

        scroll = ctk.CTkScrollableFrame(
            page,
            fg_color="transparent",
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=SCROLLBAR_HOVER,
        )
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(scroll, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 10), padx=2)
        head.grid_columnconfigure(0, weight=1)
        self.settings_status = ctk.CTkLabel(
            head,
            text="Changes save automatically",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        )
        self.settings_status.grid(row=0, column=0, sticky="w")

        automation_card, automation = self._section(
            scroll,
            "Automation",
            "Choose what to watch and how often.",
        )
        automation_card.grid(row=1, column=0, sticky="ew", pady=(0, 14), padx=2)

        self.app_segment_settings = self._setting_row_widget(
            automation,
            "Target app",
            "Claude, ChatGPT, or whichever one shows a limit.",
            lambda parent: ctk.CTkSegmentedButton(
                parent,
                values=["Claude", "ChatGPT", "Automatic"],
                width=280,
                height=30,
                corner_radius=8,
                font=self.font_button,
                selected_color=ACCENT,
                selected_hover_color=ACCENT_HOVER,
                unselected_color=CARD_ALT,
                unselected_hover_color=BUTTON_HOVER,
                text_color=TEXT,
                command=self._on_app_change,
            ),
        )

        chat_row = ctk.CTkFrame(automation, fg_color="transparent")
        chat_row.pack(fill="x", pady=7)
        chat_left = ctk.CTkFrame(chat_row, fg_color="transparent")
        chat_left.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            chat_left,
            text="Chat",
            font=self.font_body,
            text_color=TEXT,
            anchor="w",
        ).pack(anchor="w")
        self.chat_hint = ctk.CTkLabel(
            chat_left,
            text="Use the currently open chat.",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        )
        self.chat_hint.pack(anchor="w")
        chat_right = ctk.CTkFrame(chat_row, fg_color="transparent")
        chat_right.pack(side="right")
        self.chat_menu = ctk.CTkOptionMenu(
            chat_right,
            values=["Current chat"],
            width=250,
            height=30,
            corner_radius=8,
            fg_color=CARD_ALT,
            button_color=BORDER,
            button_hover_color=BUTTON_HOVER,
            dropdown_fg_color=CARD_ALT,
            dropdown_hover_color=ACCENT_SOFT,
            dropdown_text_color=TEXT,
            text_color=TEXT,
            font=self.font_small,
            dropdown_font=self.font_small,
            command=self._on_chat_change,
        )
        self.chat_menu.pack(side="left")
        self.refresh_chats_button = ctk.CTkButton(
            chat_right,
            text="Refresh",
            width=76,
            height=30,
            corner_radius=8,
            fg_color="transparent",
            hover_color=GHOST_HOVER,
            border_width=1,
            border_color=BORDER,
            text_color=MUTED,
            font=self.font_small,
            command=self._refresh_chats,
        )
        self.refresh_chats_button.pack(side="left", padx=(8, 0))

        self.interval_entry = self._setting_row_entry(
            automation,
            "Check interval",
            "How often to read the app when no limit is active. Default 15 minutes.",
            84,
        )

        self.dev_mode_var = tk.BooleanVar(value=False)
        self._setting_row_switch(
            automation,
            "Claude developer mode",
            "Probe Claude after launch, because developer-mode limits appear only after sending.",
            self.dev_mode_var,
        )

        windows_card, windows = self._section(
            scroll,
            "Windows",
            "Startup, tray, and launch behavior.",
        )
        windows_card.grid(row=2, column=0, sticky="ew", pady=(0, 14), padx=2)

        self.startup_var = tk.BooleanVar(value=False)
        self._setting_row_switch(
            windows,
            "Start with Windows",
            "Uses a Startup-folder shortcut named AutoClaudeContinue.",
            self.startup_var,
        )
        self.tray_var = tk.BooleanVar(value=True)
        self._setting_row_switch(
            windows,
            "Minimize to tray",
            "Closing the window leaves the app running in the tray.",
            self.tray_var,
        )
        self.launch_var = tk.BooleanVar(value=False)
        self._setting_row_switch(
            windows,
            "Launch if missing",
            "Only at send time, never while polling.",
            self.launch_var,
        )
        self.wake_entry = self._setting_row_entry(
            windows,
            "Wake early",
            "Seconds before the reset to check the app.",
            84,
        )

        safety_card, safety = self._section(
            scroll,
            "Safety",
            "Keep the app from interrupting you or taking over the keyboard.",
        )
        safety_card.grid(row=3, column=0, sticky="ew", pady=(0, 14), padx=2)

        self.drafts_var = tk.BooleanVar(value=True)
        self._setting_row_switch(
            safety,
            "Preserve drafts",
            "Put your unsent composer text back after attaching images.",
            self.drafts_var,
        )
        self.active_var = tk.BooleanVar(value=True)
        self._setting_row_switch(
            safety,
            "Wait while I'm using the PC",
            "Do not take focus until the keyboard and mouse have been idle.",
            self.active_var,
        )
        self.grace_entry = self._setting_row_entry(
            safety,
            "Idle grace",
            "How long the PC must be idle before the app may take focus. Default 20 seconds.",
            84,
        )
        self.locked_var = tk.BooleanVar(value=True)
        self._setting_row_switch(
            safety,
            "Pause while locked",
            "Never send while the Windows session is locked.",
            self.locked_var,
        )

        appearance_card, appearance = self._section(
            scroll,
            "Appearance",
            "Pick the palette that is easiest on your eyes.",
        )
        appearance_card.grid(row=4, column=0, sticky="ew", pady=(0, 20), padx=2)

        self.ui_scale_segment = self._setting_row_widget(
            appearance,
            "Interface scale",
            "Make text and controls larger or smaller. The window still fits your screen.",
            lambda parent: ctk.CTkSegmentedButton(
                parent,
                values=[f"{value}%" for value in UI_SCALES],
                width=250,
                height=30,
                corner_radius=8,
                font=self.font_button,
                selected_color=ACCENT,
                selected_hover_color=ACCENT_HOVER,
                unselected_color=CARD_ALT,
                unselected_hover_color=BUTTON_HOVER,
                text_color=TEXT,
                command=self._on_ui_scale_change,
            ),
        )

        self.light_var = tk.BooleanVar(value=False)
        self._setting_row_switch(
            appearance,
            "Light theme",
            "Use a bright background. The app keeps your choice after restart.",
            self.light_var,
            command=self._on_theme_toggle,
        )

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def _build_misc(self) -> None:
        page = ctk.CTkFrame(self.page_host, fg_color=BG, corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(0, weight=1)
        page.grid_columnconfigure(0, weight=1)
        self.pages["misc"] = page

        scroll = ctk.CTkScrollableFrame(
            page,
            fg_color="transparent",
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=SCROLLBAR_HOVER,
        )
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        creator = ctk.CTkFrame(
            scroll,
            fg_color=CARD,
            corner_radius=16,
            border_width=1,
            border_color=BORDER,
        )
        creator.grid(row=0, column=0, sticky="ew", pady=(0, 14), padx=2)
        creator.grid_columnconfigure(1, weight=1)

        portrait = _circular_photo(logo_path(), 88)
        if portrait is not None:
            image = ctk.CTkImage(light_image=portrait, dark_image=portrait, size=(88, 88))
            self._images.append(image)
            ctk.CTkLabel(creator, image=image, text="", width=88, height=88).grid(
                row=0, column=0, rowspan=3, padx=(22, 18), pady=22, sticky="w"
            )

        ctk.CTkLabel(
            creator,
            text="KutaOS",
            font=self.font_heading,
            text_color=TEXT,
            anchor="w",
        ).grid(row=0, column=1, sticky="w", pady=(24, 0))
        ctk.CTkLabel(
            creator,
            text="Creator",
            font=self.font_small,
            text_color=ACCENT,
            anchor="w",
        ).grid(row=1, column=1, sticky="w")
        ctk.CTkLabel(
            creator,
            text="Tools for people who would rather keep building than babysit a rate limit.",
            font=self.font_body,
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=620,
        ).grid(row=2, column=1, sticky="w", pady=(6, 24), padx=(0, 24))

        links_card, links = self._section(
            scroll,
            "Links",
            "Find the creator and say hello.",
        )
        links_card.grid(row=1, column=0, sticky="ew", pady=(0, 14), padx=2)

        self._link_row(
            links,
            youtube_logo_path(),
            "YouTube",
            "@Thekutamc",
            "https://www.youtube.com/@Thekutamc",
        )
        self._link_row(
            links,
            discord_logo_path(),
            "Discord",
            "discord.gg/VgqfppxR7G",
            "https://discord.gg/VgqfppxR7G",
        )

        license_card, license_body = self._section(
            scroll,
            "License",
            "Proprietary software. Personal use only — no redistribution.",
        )
        license_card.grid(row=2, column=0, sticky="ew", pady=(0, 14), padx=2)

        license_summary = ctk.CTkFrame(
            license_body,
            fg_color=CARD_ALT,
            corner_radius=12,
        )
        license_summary.pack(fill="x", pady=(0, 12))
        license_summary.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            license_summary,
            text=(
                "AutoClaudeContinue is licensed, not sold. You may use it "
                "yourself, but you may not copy, share, publish, resell, or "
                "bundle it — or reuse its code, design, or assets in another "
                "product."
            ),
            font=self.font_body,
            text_color=TEXT,
            anchor="w",
            justify="left",
            wraplength=520,
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            license_summary,
            text="End User License Agreement  ·  Version 1.0",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 14))

        license_actions = ctk.CTkFrame(license_body, fg_color="transparent")
        license_actions.pack(fill="x")
        ctk.CTkButton(
            license_actions,
            text="Read license",
            width=120,
            height=30,
            corner_radius=8,
            fg_color=ACCENT_SOFT,
            hover_color=ACCENT_SOFT_HOVER,
            text_color=BLUE,
            font=self.font_small,
            command=self._show_license_dialog,
        ).pack(side="left")

        files_card, files = self._section(scroll, "App files")
        files_card.grid(row=3, column=0, sticky="ew", pady=(0, 20), padx=2)

        ctk.CTkButton(
            files,
            text="Open data folder",
            width=132,
            height=30,
            corner_radius=8,
            fg_color=CARD_ALT,
            hover_color=BUTTON_HOVER,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=self.font_small,
            command=lambda: self._open_path(default_data_dir()),
        ).pack(side="left")
        ctk.CTkButton(
            files,
            text="Open activity log",
            width=132,
            height=30,
            corner_radius=8,
            fg_color="transparent",
            hover_color=GHOST_HOVER,
            border_width=1,
            border_color=BORDER,
            text_color=MUTED,
            font=self.font_small,
            command=lambda: self._open_path(default_data_dir() / "app.log"),
        ).pack(side="left", padx=(8, 0))

    # ------------------------------------------------------------------
    # License dialog
    # ------------------------------------------------------------------

    def _show_license_dialog(self) -> None:
        existing = self._license_dialog
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                pass
            self._license_dialog = None

        dialog = ctk.CTkToplevel(self.root)
        self._license_dialog = dialog
        dialog.title("License Agreement")
        dialog.geometry(self._centered_geometry(900, 680))
        dialog.minsize(620, 420)
        dialog.configure(fg_color=BG)
        dialog.transient(self.root)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(dialog, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 10))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            head,
            text="License Agreement",
            font=self.font_heading,
            text_color=TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            head,
            text=f"AutoClaudeContinue  ·  v{__version__}  ·  KutaOS",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        body = ctk.CTkTextbox(
            dialog,
            corner_radius=12,
            fg_color=CARD,
            border_width=1,
            border_color=BORDER,
            text_color=MUTED,
            font=self.font_label,
            wrap="word",
            activate_scrollbars=True,
        )
        body.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 12))
        body.insert("1.0", self._license_text())
        body.mark_set("insert", "1.0")
        body.see("1.0")
        body.configure(state="disabled")

        footer = ctk.CTkFrame(dialog, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 20))
        ctk.CTkLabel(
            footer,
            text="© 2026 KutaOS. All rights reserved.",
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        ).pack(side="left")
        ctk.CTkButton(
            footer,
            text="Close",
            width=100,
            height=30,
            corner_radius=8,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            text_color=TEXT,
            font=self.font_small,
            command=self._close_license_dialog,
        ).pack(side="right")

        dialog.protocol("WM_DELETE_WINDOW", self._close_license_dialog)
        dialog.bind("<Escape>", lambda _event: self._close_license_dialog())
        try:
            dialog.grab_set()
        except tk.TclError:
            log.debug("Could not make the license dialog modal", exc_info=True)

        def focus_body() -> None:
            if self._license_dialog is not dialog:
                return
            try:
                body.focus_set()
                body.see("1.0")
            except Exception:
                log.debug("Could not focus the license text", exc_info=True)

        def reset_view() -> None:
            if self._license_dialog is not dialog:
                return
            try:
                body.see("1.0")
            except Exception:
                log.debug("Could not scroll the license text", exc_info=True)

        dialog.after(40, dialog.focus_force)
        dialog.after(80, focus_body)
        dialog.after(250, lambda: self._style_dialog_titlebar(dialog))
        dialog.after(320, reset_view)

    def _style_dialog_titlebar(self, dialog: ctk.CTkToplevel) -> None:
        """Match the Windows title bar to the active light or dark theme."""
        if self._license_dialog is not dialog:
            return
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetParent(dialog.winfo_id())
            value = ctypes.c_int(1 if self._theme == "dark" else 0)
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(value), ctypes.sizeof(value)
            )
            if result != 0:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 19, ctypes.byref(value), ctypes.sizeof(value)
                )
        except Exception:
            log.debug("Could not set the license title bar color", exc_info=True)

    def _close_license_dialog(self) -> None:
        dialog = self._license_dialog
        self._license_dialog = None
        if dialog is None:
            return
        try:
            dialog.grab_release()
        except Exception:
            pass
        try:
            dialog.destroy()
        except Exception:
            pass

    @staticmethod
    def _license_text() -> str:
        path = license_path()
        if path is None:
            log.warning("License file is missing from the installation")
            return LICENSE_FALLBACK
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            log.exception("Could not read the license from %s", path)
            return LICENSE_FALLBACK

    # ------------------------------------------------------------------
    # Shared builders
    # ------------------------------------------------------------------

    def _section(
        self,
        parent,
        title: str,
        subtitle: str | None = None,
    ) -> tuple[ctk.CTkFrame, ctk.CTkFrame]:
        card = ctk.CTkFrame(
            parent,
            fg_color=CARD,
            corner_radius=16,
            border_width=1,
            border_color=BORDER,
        )
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(18, 0))
        ctk.CTkLabel(
            head,
            text=title,
            font=self.font_section,
            text_color=TEXT,
            anchor="w",
        ).pack(anchor="w")
        if subtitle:
            ctk.CTkLabel(
                head,
                text=subtitle,
                font=self.font_small,
                text_color=FAINT,
                anchor="w",
            ).pack(anchor="w", pady=(2, 0))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=20, pady=(8, 18))
        return card, body

    def _setting_row_widget(self, parent, label: str, hint: str, factory):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=7)
        row.grid_columnconfigure(0, weight=1)
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew")
        ctk.CTkLabel(
            left,
            text=label,
            font=self.font_body,
            text_color=TEXT,
            anchor="w",
        ).pack(anchor="w")
        hint_label = ctk.CTkLabel(
            left,
            text=hint,
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
            justify="left",
            wraplength=320,
        )
        hint_label.pack(anchor="w", fill="x")
        right = ctk.CTkFrame(row, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e", padx=(18, 0))
        widget = factory(right)
        if widget.winfo_manager() != "pack":
            widget.pack(side="left")

        def wrap_hint(event) -> None:
            room = event.width - right.winfo_reqwidth() - 30
            scale = hint_label._get_widget_scaling()
            hint_label.configure(wraplength=max(160, int(room / scale)))

        row.bind("<Configure>", wrap_hint, add="+")
        return widget

    def _setting_row_switch(
        self,
        parent,
        label: str,
        hint: str,
        variable: tk.BooleanVar,
        command=None,
    ) -> ctk.CTkSwitch:
        return self._setting_row_widget(
            parent,
            label,
            hint,
            lambda holder: ctk.CTkSwitch(
                holder,
                text="",
                width=48,
                switch_width=40,
                switch_height=22,
                fg_color=SWITCH_OFF,
                progress_color=ACCENT,
                button_color=SWITCH_KNOB,
                button_hover_color=SWITCH_KNOB_HOVER,
                variable=variable,
                command=command or self._apply_settings,
            ),
        )

    def _setting_row_entry(
        self,
        parent,
        label: str,
        hint: str,
        width: int,
    ) -> ctk.CTkEntry:
        def build(holder):
            entry = ctk.CTkEntry(
                holder,
                width=width,
                height=30,
                corner_radius=8,
                border_width=1,
                border_color=BORDER,
                fg_color=CARD_ALT,
                text_color=TEXT,
                font=self.font_body,
                justify="center",
            )
            entry.pack(side="left")
            ctk.CTkLabel(
                holder,
                text="min" if label == "Check interval" else "sec",
                font=self.font_small,
                text_color=FAINT,
            ).pack(side="left", padx=(6, 0))
            entry.bind("<FocusOut>", self._apply_settings)
            entry.bind("<Return>", self._apply_settings)
            return entry

        return self._setting_row_widget(parent, label, hint, build)

    def _link_row(
        self,
        parent,
        logo: Path | None,
        title: str,
        subtitle: str,
        url: str,
    ) -> None:
        row = ctk.CTkFrame(
            parent,
            fg_color=CARD_ALT,
            corner_radius=12,
            height=60,
            cursor="hand2",
        )
        row.pack(fill="x", pady=5)
        row.grid_columnconfigure(1, weight=1)

        if logo is not None:
            image = _square_photo(logo, 28)
            if image is not None:
                ctk_image = ctk.CTkImage(light_image=image, dark_image=image, size=(28, 28))
                self._thumb_refs.append(ctk_image)
                ctk.CTkLabel(row, image=ctk_image, text="", width=28, height=28).grid(
                    row=0, column=0, rowspan=2, padx=(14, 12), pady=14
                )

        title_label = ctk.CTkLabel(
            row,
            text=title,
            font=self.font_button,
            text_color=TEXT,
            anchor="w",
        )
        title_label.grid(row=0, column=1, sticky="sw", pady=(10, 0))
        subtitle_label = ctk.CTkLabel(
            row,
            text=subtitle,
            font=self.font_small,
            text_color=FAINT,
            anchor="w",
        )
        subtitle_label.grid(row=1, column=1, sticky="nw", pady=(0, 10))
        arrow = ctk.CTkLabel(
            row,
            text="↗",
            font=self.font_section,
            text_color=MUTED,
            width=24,
        )
        arrow.grid(row=0, column=2, rowspan=2, padx=(8, 16))

        def open_link(_event=None):
            webbrowser.open(url)

        for widget in (row, title_label, subtitle_label, arrow):
            widget.bind("<Button-1>", open_link)
            widget.bind("<Enter>", lambda _e: row.configure(fg_color=LINK_HOVER))
            widget.bind("<Leave>", lambda _e: row.configure(fg_color=CARD_ALT))

    # ------------------------------------------------------------------
    # Configuration -> widgets
    # ------------------------------------------------------------------

    def _load_from_config(self, config: Config) -> None:
        self._loading = True
        try:
            self.app_segment_home.set(APP_LABELS.get(config.app_target, "Claude"))
            self.app_segment_settings.set(APP_LABELS.get(config.app_target, "Claude"))

            self.detection_switch.select() if config.detection_enabled else self.detection_switch.deselect()
            self._update_detection_appearance()

            self.light_var.set(config.theme == "light")
            self._apply_theme(config.theme)
            self.ui_scale_segment.set(f"{config.ui_scale}%")

            self.message_box.delete("1.0", "end")
            self.message_box.insert("1.0", config.message)
            self._render_presets()

            self.interval_entry.delete(0, "end")
            self.interval_entry.insert(0, str(max(1, config.poll_seconds // 60)))
            self.wake_entry.delete(0, "end")
            self.wake_entry.insert(0, str(config.wake_early_seconds))
            self.grace_entry.delete(0, "end")
            self.grace_entry.insert(0, str(config.active_grace_seconds))

            self.tray_var.set(config.minimize_to_tray)
            self.launch_var.set(config.launch_if_missing)
            self.dev_mode_var.set(config.claude_dev_mode)
            self.drafts_var.set(config.preserve_drafts)
            self.active_var.set(config.defer_when_active)
            self.locked_var.set(config.pause_when_locked)

            self._render_attachments()
            self._rebuild_chat_menu()
        finally:
            self._loading = False

    def _sync_startup(self) -> None:
        config = self.store.get()
        enabled = startup_is_enabled()
        if config.start_with_windows and not enabled:
            enabled = set_startup_enabled(True)
        elif not config.start_with_windows and enabled:
            self.store.update(start_with_windows=True)
        self.startup_var.set(bool(enabled))

    # ------------------------------------------------------------------
    # Page navigation
    # ------------------------------------------------------------------

    def _show_page(self, page: str) -> None:
        if page not in self.pages:
            return
        self._page = page
        for key, frame in self.pages.items():
            if key == page:
                frame.grid()
            else:
                frame.grid_remove()
        for key, button in self.nav_buttons.items():
            if key == page:
                button.configure(fg_color=ACCENT_SOFT, text_color=TEXT)
            else:
                button.configure(fg_color="transparent", text_color=MUTED)
        title, subtitle = PAGE_META[page]
        self.page_title.configure(text=title)
        self.page_subtitle.configure(text=subtitle)
        if page == "settings" and not self._chats:
            self._refresh_chats(silent=True)
        if page == "home":
            self.root.after_idle(self._fit_home_content)

    # ------------------------------------------------------------------
    # Home actions
    # ------------------------------------------------------------------

    def _on_app_change(self, value: str) -> None:
        if self._loading:
            return
        key = APP_KEYS.get(value, "claude")
        self.store.update(app_target=key)
        self.app_segment_home.set(APP_LABELS[key])
        self.app_segment_settings.set(APP_LABELS[key])
        self.automator.submit("config_changed")
        self._append_activity(f"Target app set to {APP_LABELS[key]}.")
        self._refresh_chats(silent=True)

    def _on_detection_toggle(self) -> None:
        if self._loading:
            return
        enabled = bool(self.detection_switch.get())
        self.store.update(detection_enabled=enabled)
        self._update_detection_appearance()
        self.automator.submit("config_changed")
        self._append_activity("Detection resumed." if enabled else "Detection paused.")

    def _update_detection_appearance(self) -> None:
        enabled = bool(self.detection_switch.get())
        self.detection_switch.configure(
            text="Detection on" if enabled else "Detection off",
            progress_color=ACCENT if enabled else SWITCH_OFF,
        )
        if not enabled:
            self.sidebar_status.configure(text="Paused", text_color=FAINT)
            self.sidebar_dot.configure(fg_color=GRAY)

    def _check_now(self) -> None:
        # Make sure the text currently in the box is the text that gets sent,
        # even if the autosave debounce has not fired yet.
        self._save_message()
        self.automator.submit("check_now")
        self._append_activity("Manual message send requested.")
        self.check_button.configure(text="Sending…", state="disabled")
        # Stay disabled until the automator reports a terminal state; this
        # prevents a second click from queueing another send mid-flight.
        if self._check_reset_after is not None:
            try:
                self.root.after_cancel(self._check_reset_after)
            except Exception:
                pass
        self._check_reset_after = self.root.after(120000, self._reset_check_button)

    def _reset_check_button(self) -> None:
        if self._check_reset_after is not None:
            try:
                self.root.after_cancel(self._check_reset_after)
            except Exception:
                pass
            self._check_reset_after = None
        try:
            self.check_button.configure(text="Send now", state="normal")
        except Exception:
            pass

    def _on_message_key(self, event=None) -> None:
        if self._loading:
            return
        if self._message_after is not None:
            try:
                self.root.after_cancel(self._message_after)
            except Exception:
                pass
        self._message_after = self.root.after(700, self._save_message)

    def _save_message(self, event=None) -> None:
        if self._loading:
            return
        text = self.message_box.get("1.0", "end").strip()
        if not text:
            return
        if text != self.store.get().message:
            self.store.update(message=text)
            self.automator.submit("config_changed")
        self.message_saved.configure(text="Saved just now", text_color=GREEN)
        self.root.after(
            1600,
            lambda: self.message_saved.configure(
                text="Draft saves automatically",
                text_color=FAINT,
            ),
        )

    def _render_presets(self) -> None:
        config = self.store.get()
        values = ["Presets"] + list(config.saved_messages)
        self.preset_menu.configure(values=values)
        self.preset_menu.set("Presets")

    def _on_preset_selected(self, value: str) -> None:
        if value and value != "Presets":
            self._use_preset(value)
        self.root.after(10, lambda: self.preset_menu.set("Presets"))

    def _use_preset(self, value: str) -> None:
        self.message_box.delete("1.0", "end")
        self.message_box.insert("1.0", value)
        self.store.update(message=value)
        self.automator.submit("config_changed")
        self.message_saved.configure(text="Preset loaded", text_color=ACCENT)
        self.root.after(
            1400,
            lambda: self.message_saved.configure(
                text="Draft saves automatically",
                text_color=FAINT,
            ),
        )

    def _save_preset(self) -> None:
        text = self.message_box.get("1.0", "end").strip()
        if not text:
            return
        config = self.store.get()
        self.store.update(
            message=text,
            saved_messages=tuple(config.saved_messages) + (text,),
        )
        self._render_presets()
        self.message_saved.configure(text="Preset saved", text_color=GREEN)
        self.root.after(
            1600,
            lambda: self.message_saved.configure(
                text="Draft saves automatically",
                text_color=FAINT,
            ),
        )

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def _paste_images(self) -> None:
        paths = save_clipboard_images()
        if not paths:
            self._set_attachment_hint("No image found on the clipboard.")
            return
        self._add_paths(paths)

    def _on_paste(self, event=None):
        focus = self.root.focus_get()
        # Let entries and text boxes paste text normally.
        if isinstance(focus, (tk.Entry, tk.Text)):
            return None
        paths = save_clipboard_images()
        if not paths:
            self._set_attachment_hint("No image found on the clipboard.")
            return "break"
        self._add_paths(paths)
        return "break"

    def _add_images(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(IMAGE_EXTENSIONS))
        paths = filedialog.askopenfilenames(
            title="Add images",
            filetypes=[("Images", patterns), ("All files", "*.*")],
        )
        if paths:
            self._add_paths(list(paths))

    def _add_paths(self, paths: list[str]) -> None:
        added = 0
        skipped = 0
        for path in paths:
            if not is_image(path) or not Path(path).is_file():
                skipped += 1
                continue
            if path in self._attachments:
                continue
            if len(self._attachments) >= MAX_ATTACHMENTS:
                skipped += 1
                continue
            self._attachments.append(path)
            added += 1
        self.store.update(attachments=tuple(self._attachments))
        self.automator.submit("config_changed")
        self._render_attachments()
        if added:
            message = f"{added} image{'s' if added != 1 else ''} attached."
            if skipped:
                message += f" {skipped} skipped."
            self._set_attachment_hint(message, GREEN)
            self._append_activity(message)
        elif skipped:
            self._set_attachment_hint("No new images added.", ORANGE)

    def _remove_attachment(self, path: str) -> None:
        if path in self._attachments:
            self._attachments.remove(path)
            self.store.update(attachments=tuple(self._attachments))
            self.automator.submit("config_changed")
            self._render_attachments()
            self._set_attachment_hint("Image removed.", MUTED)

    def _render_attachments(self) -> None:
        for child in self.attachment_list.winfo_children():
            child.destroy()
        self._attachment_refs.clear()

        if not self._attachments:
            self.empty_state.grid()
            self.attachment_list.grid_remove()
            self._set_attachment_hint("Images are optional.", FAINT)
            self.root.after_idle(self._fit_home_content)
            return

        self.empty_state.grid_remove()
        self.attachment_list.grid()
        for path in self._attachments:
            row = ctk.CTkFrame(
                self.attachment_list,
                fg_color=CARD_ALT,
                corner_radius=9,
                height=44,
            )
            row.pack(fill="x", pady=3, padx=2)
            row.grid_columnconfigure(1, weight=1)

            thumb = _thumbnail(path, 32)
            if thumb is not None:
                image = ctk.CTkImage(light_image=thumb, dark_image=thumb, size=(32, 32))
                self._attachment_refs.append(image)
                ctk.CTkLabel(row, image=image, text="", width=32, height=32).grid(
                    row=0, column=0, padx=(8, 8), pady=6
                )

            name = Path(path).name
            display = name if len(name) <= 30 else name[:27] + "…"
            exists = Path(path).is_file()
            text = display if exists else f"{display}  (missing)"
            ctk.CTkLabel(
                row,
                text=text,
                font=self.font_small,
                text_color=TEXT if exists else RED,
                anchor="w",
            ).grid(row=0, column=1, sticky="w")
            if exists:
                ctk.CTkLabel(
                    row,
                    text=format_size(Path(path).stat().st_size),
                    font=self.font_small,
                    text_color=FAINT,
                    anchor="e",
                ).grid(row=0, column=2, sticky="e", padx=(6, 8))
            ctk.CTkButton(
                row,
                text="×",
                width=24,
                height=24,
                corner_radius=6,
                fg_color="transparent",
                hover_color=ROW_HOVER,
                text_color=FAINT,
                font=self.font_body,
                command=lambda value=path: self._remove_attachment(value),
            ).grid(row=0, column=3, padx=(0, 8))

        count = len(self._attachments)
        self._set_attachment_hint(
            f"{count} image{'s' if count != 1 else ''} attached.",
            MUTED,
        )
        self.root.after_idle(self._fit_home_content)

    def _set_attachment_hint(self, text: str, color: str = FAINT) -> None:
        self.attachment_hint.configure(text=text, text_color=color)

    # ------------------------------------------------------------------
    # Settings actions
    # ------------------------------------------------------------------

    def _apply_settings(self, event=None) -> None:
        if self._loading:
            return

        try:
            minutes = int(self.interval_entry.get().strip() or "15")
            if minutes < 1 or minutes > 1440:
                raise ValueError
        except ValueError:
            current = self.store.get()
            self.interval_entry.delete(0, "end")
            self.interval_entry.insert(0, str(max(1, current.poll_seconds // 60)))
            self._settings_feedback("Check interval must be 1–1440 minutes.", RED)
            return

        try:
            grace = int(self.grace_entry.get().strip() or "0")
            if grace < 0 or grace > 3600:
                raise ValueError
        except ValueError:
            grace = self.store.get().active_grace_seconds
            self.grace_entry.delete(0, "end")
            self.grace_entry.insert(0, str(grace))

        try:
            wake = int(self.wake_entry.get().strip() or "0")
            if wake < 0 or wake > 300:
                raise ValueError
        except ValueError:
            wake = self.store.get().wake_early_seconds
            self.wake_entry.delete(0, "end")
            self.wake_entry.insert(0, str(wake))

        wanted_startup = bool(self.startup_var.get())
        if wanted_startup != startup_is_enabled():
            if not set_startup_enabled(wanted_startup):
                self.startup_var.set(not wanted_startup)
                self._settings_feedback("Could not update the startup shortcut.", RED)
                return

        self.store.update(
            start_with_windows=wanted_startup,
            minimize_to_tray=bool(self.tray_var.get()),
            launch_if_missing=bool(self.launch_var.get()),
            claude_dev_mode=bool(self.dev_mode_var.get()),
            preserve_drafts=bool(self.drafts_var.get()),
            defer_when_active=bool(self.active_var.get()),
            pause_when_locked=bool(self.locked_var.get()),
            poll_seconds=minutes * 60,
            active_grace_seconds=grace,
            wake_early_seconds=wake,
        )
        self.automator.submit("config_changed")
        self._settings_feedback("Saved just now", GREEN)

    def _on_theme_toggle(self) -> None:
        if self._loading:
            return
        theme = "light" if bool(self.light_var.get()) else "dark"
        self._apply_theme(theme, persist=True)

    def _on_ui_scale_change(self, value: str) -> None:
        if self._loading:
            return
        try:
            scale = int(str(value).strip().rstrip("%"))
        except ValueError:
            return
        self._apply_ui_scale(scale, persist=True)

    def _apply_ui_scale(self, scale: int, persist: bool = False) -> None:
        if scale not in UI_SCALES:
            scale = 100
        self._ui_scale = scale
        ctk.set_widget_scaling(scale / 100)
        width, height = self._fit_window_size(1080, 780)
        self.root.minsize(*self._fit_window_size(940, 600))
        self.root.geometry(self._centered_geometry(width, height))
        if hasattr(self, "ui_scale_segment"):
            self.ui_scale_segment.set(f"{scale}%")
        self.root.after(220, self._fit_home_content)
        if persist:
            self.store.update(ui_scale=scale)
            self._append_activity(f"Interface scale set to {scale}%.")

    def _apply_theme(self, theme: str, persist: bool = False) -> None:
        theme = "light" if theme == "light" else "dark"
        self._theme = theme
        ctk.set_appearance_mode(theme)
        dialog = self._license_dialog
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    self.root.after(60, lambda: self._style_dialog_titlebar(dialog))
            except Exception:
                pass
        if persist:
            self.store.update(theme=theme)
            self._append_activity(
                "Light theme enabled." if theme == "light" else "Dark theme enabled."
            )

    def _settings_feedback(self, text: str, color: str) -> None:
        self.settings_status.configure(text=text, text_color=color)
        if self._settings_after is not None:
            try:
                self.root.after_cancel(self._settings_after)
            except Exception:
                pass
        self._settings_after = self.root.after(
            2200,
            lambda: self.settings_status.configure(
                text="Changes save automatically",
                text_color=FAINT,
            ),
        )

    def _refresh_chats(self, silent: bool = False) -> None:
        if self._loading:
            return
        self.automator.submit("refresh_chats")
        self.refresh_chats_button.configure(state="disabled", text="Reading…")
        if not silent:
            self.chat_hint.configure(text="Reading the sidebar…", text_color=FAINT)
        self.root.after(5000, self._reset_refresh_button)

    def _reset_refresh_button(self) -> None:
        try:
            self.refresh_chats_button.configure(state="normal", text="Refresh")
        except Exception:
            pass

    def _on_chat_change(self, value: str) -> None:
        if self._loading:
            return
        name = "" if value == "Current chat" else value
        self.store.update(chat_name=name)
        self.automator.submit("config_changed")
        if name:
            self._append_activity(f'Chat set to "{name}".')
        else:
            self._append_activity("Using the currently open chat.")

    def _rebuild_chat_menu(self) -> None:
        current = self.store.get().chat_name
        values = ["Current chat"] + list(self._chats)
        if current and current not in values:
            values.insert(1, current)
        self.chat_menu.configure(values=values)
        self.chat_menu.set(current if current else "Current chat")
        if self._chats:
            self.chat_hint.configure(
                text=f"{len(self._chats)} chats found in the sidebar.",
                text_color=FAINT,
            )
        elif current:
            self.chat_hint.configure(
                text=f'Using "{current}" from your settings.',
                text_color=FAINT,
            )
        else:
            self.chat_hint.configure(
                text="Use the currently open chat, or refresh to list chats.",
                text_color=FAINT,
            )

    # ------------------------------------------------------------------
    # Events and countdown
    # ------------------------------------------------------------------

    def _report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        log.error(
            "Tk callback failed",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def _poll_events(self) -> None:
        if self._quitting:
            return
        try:
            for _ in range(60):
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                kind = event.get("type")
                if kind == "state":
                    self._on_state(event)
                elif kind == "chats":
                    self._on_chats(event)
                elif kind == "draft_reset":
                    self._on_draft_reset(event)
                elif kind == "log":
                    self._append_activity(event.get("message", ""))
                elif kind == "dev_mode":
                    self.dev_mode_var.set(bool(event.get("enabled")))
                    self._append_activity(
                        "Claude developer mode detected and enabled."
                    )
        except Exception:
            log.exception("Could not process UI events")
        self.root.after(150, self._poll_events)

    def _on_state(self, event: dict) -> None:
        state = event.get("state", "idle")
        color = COLORS.get(event.get("color", "gray"), GRAY)
        self._state = state
        self._status_color = color

        if self._check_reset_after is not None and state in {
            "sent",
            "error",
            "not_found",
            "deferred",
            "waiting",
            "stopped",
        }:
            self._reset_check_button()

        self.status_dot.configure(fg_color=color)
        self.status_state.configure(
            text=STATE_LABELS.get(state, state.title()),
            text_color=color,
        )
        self.status_title.configure(text=event.get("title", ""))
        self.status_detail.configure(text=event.get("detail", ""))

        self.sidebar_status.configure(
            text=STATE_LABELS.get(state, "Monitoring"),
            text_color=color,
        )
        self.sidebar_dot.configure(fg_color=color)

        app_label = event.get("app_label") or ""
        self.status_app.configure(text=app_label or "AutoClaudeContinue")

        target: datetime | None = None
        raw = event.get("reset_at")
        if raw:
            try:
                target = datetime.fromisoformat(raw)
            except ValueError:
                target = None

        retry_target: datetime | None = None
        raw_retry = event.get("retry_at")
        if raw_retry:
            try:
                retry_target = datetime.fromisoformat(raw_retry)
            except ValueError:
                retry_target = None

        self._wait_target = target
        self._retry_target = retry_target

        if retry_target is not None:
            self.status_reset.configure(text="Reset passed — retrying")
        elif target is not None:
            self.status_reset.configure(
                text="Estimated reset time"
                if event.get("estimated")
                else "Wake timer armed"
            )
        elif state == "idle":
            self.status_reset.configure(text="No reset scheduled")
        elif state == "disabled":
            self.status_reset.configure(text="Detection is paused")
        elif state == "sent":
            self.status_reset.configure(text="Message delivered")
        else:
            self.status_reset.configure(text="No reset scheduled")

        self._update_countdown()

    def _on_draft_reset(self, event: dict) -> None:
        """Reflect a successful send: preset saved, draft back to continue."""

        message = event.get("message") or "continue"
        self._loading = True
        try:
            self.message_box.delete("1.0", "end")
            self.message_box.insert("1.0", message)
            self._render_presets()
            self._attachments = [str(p) for p in event.get("attachments") or []]
            self._render_attachments()
        finally:
            self._loading = False
        self.message_saved.configure(
            text="Sent message saved as a preset",
            text_color=GREEN,
        )
        self.root.after(
            2200,
            lambda: self.message_saved.configure(
                text="Draft saves automatically",
                text_color=FAINT,
            ),
        )

    def _on_chats(self, event: dict) -> None:
        self._chats = list(event.get("chats") or [])
        self._rebuild_chat_menu()
        self._reset_refresh_button()
        app_label = event.get("app_label") or ""
        if self._chats:
            self.chat_hint.configure(
                text=f"{len(self._chats)} chats from {app_label}.",
                text_color=FAINT,
            )
        elif app_label:
            self.chat_hint.configure(
                text=f"{app_label} is open, but no sidebar chats were readable.",
                text_color=FAINT,
            )
        else:
            self.chat_hint.configure(
                text="No app window found. Open it and focus it once, then refresh.",
                text_color=FAINT,
            )

    def _tick_clock(self) -> None:
        if self._quitting:
            return
        try:
            self._update_countdown()
        except Exception:
            log.debug("Countdown update failed", exc_info=True)
        self.root.after(1000, self._tick_clock)

    def _update_countdown(self) -> None:
        now = datetime.now()
        target = self._wait_target
        retrying = False

        if (
            target is not None
            and target <= now
            and self._retry_target is not None
        ):
            target = self._retry_target
            retrying = True

        if self._state == "disabled":
            self.countdown_label.configure(text="Paused", text_color=FAINT)
            self.countdown_caption.configure(text="detection is off")
            return

        if target is None:
            self.countdown_label.configure(
                text="—",
                text_color=TEXT if self._state != "not_found" else MUTED,
            )
            caption = {
                "idle": "no active limit",
                "sent": "message sent",
                "sending": "sending now",
                "not_found": "waiting for the app",
                "not_ready": "waiting for the app",
                "error": "needs attention",
                "deferred": "waiting for you",
            }.get(self._state, "no active limit")
            self.countdown_caption.configure(text=caption)
            return

        remaining = (target - now).total_seconds()
        if remaining <= 0:
            self.countdown_label.configure(text="Ready", text_color=GREEN)
            self.countdown_caption.configure(text="checking the app now")
            return

        hours, rem = divmod(int(remaining), 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            text = f"{minutes:02d}:{seconds:02d}"
        self.countdown_label.configure(text=text, text_color=TEXT)
        self.countdown_caption.configure(
            text="retrying after reset" if retrying else "until reset"
        )

    # ------------------------------------------------------------------
    # Activity log
    # ------------------------------------------------------------------

    def _append_activity(self, message: str) -> None:
        if not message:
            return
        stamp = datetime.now().strftime("%H:%M")
        try:
            self.activity_box.configure(state="normal")
            self.activity_box.insert("end", f"{stamp}  {message}\n")
            lines = int(self.activity_box.index("end-1c").split(".")[0])
            if lines > 120:
                self.activity_box.delete("1.0", "20.0")
            self.activity_box.see("end")
            self.activity_box.configure(state="disabled")
        except Exception:
            log.debug("Could not append to the activity log", exc_info=True)

    def _clear_activity(self) -> None:
        try:
            self.activity_box.configure(state="normal")
            self.activity_box.delete("1.0", "end")
            self.activity_box.configure(state="disabled")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Tray, close, and external links
    # ------------------------------------------------------------------

    def _poll_tray(self) -> None:
        if self._quitting:
            return
        try:
            for _ in range(10):
                try:
                    command, _payload = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                if command == "show":
                    self._show_window()
                elif command == "check":
                    self._check_now()
                elif command == "toggle":
                    self.detection_switch.toggle()
                    self._on_detection_toggle()
                elif command == "quit":
                    self._quit()
        except Exception:
            log.exception("Could not process tray commands")
        self.root.after(250, self._poll_tray)

    def _show_window(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _on_close(self) -> None:
        if self.store.get().minimize_to_tray and self.tray.available and not self._quitting:
            self._save_message()
            self.root.withdraw()
            self.tray.notify_running_in_tray()
            return
        self._quit()

    def _quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        try:
            self._save_message()
        except Exception:
            pass
        try:
            self.automator.stop()
            self.automator.join(timeout=2.0)
        except Exception:
            pass
        try:
            self.tray.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _open_path(self, path: Path) -> None:
        try:
            if path.exists() or path.suffix == "":
                os.startfile(str(path))
            else:
                self._append_activity(f"Not found: {path}")
        except Exception:
            log.exception("Could not open %s", path)
            self._append_activity(f"Could not open {path}")
