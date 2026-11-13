"""Нативное приложение Shalost FOTUR для Windows"""
from __future__ import annotations

import json
import hmac
import logging
import os
import queue
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse
from uuid import UUID, uuid4

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

APP_NAME = "Shalost FOTUR"
PULSE_HOME = "https://pulse.mirea.ru/"
PULSE_RPC = "https://pulse.mirea.ru/rtu_tc.attendance.api.AttendanceService/SelfApproveAttendanceThroughQRCode"
CLOUDTIPS_URL = "https://pay.cloudtips.ru/p/b58c4bc1"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Shalost"
CHROME_PROFILE = DATA_DIR / "pulse-chrome-profile"
PROFILES_DIR = DATA_DIR / "profiles"
ACCOUNTS_PATH = DATA_DIR / "accounts.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
RUN_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", RUN_DIR))
QR_IMAGE = BUNDLE_DIR / "assets" / "cloudtips-donation-qr.png"
APP_ICON = BUNDLE_DIR / "assets" / "shalost-fotur.ico"
APP_ICON_PNG = BUNDLE_DIR / "assets" / "shalost-fotur.png"
EXTENSION_DIR = BUNDLE_DIR / "extension"


class FrameReceiver:
    def __init__(self) -> None:
        self.key = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.frame = None
        self.received_at = 0.0
        self.enabled = lambda: False
        self.presence_confirmed = lambda: None
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path not in ("/frame", "/check", "/mode", "/presence") or not hmac.compare_digest(self.headers.get("X-Shalost-Key", ""), receiver.key):
                    self.send_error(403)
                    return
                if self.path == "/check":
                    self.send_response(204)
                    self.end_headers()
                    return
                if self.path == "/mode":
                    data = json.dumps({"scanning": receiver.enabled()}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.path == "/presence":
                    if receiver.enabled():
                        receiver.presence_confirmed()
                    self.send_response(204)
                    self.end_headers()
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4_000_000:
                        raise ValueError("invalid length")
                    data = self.rfile.read(length)
                    if len(data) != length:
                        raise ValueError("incomplete frame")
                    with receiver.lock:
                        receiver.frame = data
                        receiver.received_at = time.monotonic()
                    self.send_response(204)
                    self.end_headers()
                except (OSError, ValueError):
                    self.send_error(400)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 47881), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def latest(self):
        with self.lock:
            if self.frame is None or time.monotonic() - self.received_at > 8:
                return None
            return self.frame


def log_path() -> Path:
    candidate = RUN_DIR / "pulseqr.log"
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        with candidate.open("a", encoding="utf-8"):
            pass
        return candidate
    except OSError:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR / "pulseqr.log"


LOG_PATH = log_path()

STATE_JS = r"""() => {
  // Ударения не должны мешать распознаванию имени, букву «ё» сохраняем.
  const clean = value => value.normalize('NFD').replace(/\u0301/g, '').normalize('NFC').trim();
  const namePattern = /^[\p{L}][\p{L}\p{M}'’\-]+(?:\s+[\p{L}][\p{L}\p{M}'’\-]+)*\s+[\p{L}]\.\s*[\p{L}]\.?$/u;
  const accountName = [...document.querySelectorAll('a, button, [role="button"]')]
    .map(x => clean(x.innerText || ''))
    .find(x => namePattern.test(x)) || '';
  const pageText = document.body?.innerText || '';
  const hasLoginForm = [...document.querySelectorAll('input')].some(x =>
    !x.disabled && x.getClientRects().length > 0 && getComputedStyle(x).visibility !== 'hidden' &&
    /password|email|login|username/i.test(`${x.type} ${x.name} ${x.autocomplete}`)
  );
  const authenticatedUI = Boolean(accountName && pageText.includes('Мои занятия') && pageText.includes('Сервисы') && !hasLoginForm);
  return {onPulse: location.hostname === 'pulse.mirea.ru', name: accountName.split(/\s+/)[0], authenticatedUI};
}"""


def logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    result = logging.getLogger("shalost")
    result.handlers.clear()
    result.setLevel(logging.INFO)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    result.addHandler(handler)
    return result


LOGGER = logger()


def load_settings() -> dict:
    data = {"cooldown_minutes": 10}
    try:
        data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        pass
    return data


def qr_token(value: str) -> str | None:
    value = unquote(value.strip())
    if "token=" in value:
        value = parse_qs(urlparse(value).query).get("token", [""])[0]
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        return None


def grpc_body(token: str) -> bytes:
    raw = token.encode("utf-8")
    proto = b"\x0a" + bytes([len(raw)]) + raw
    return b"\x00" + struct.pack(">I", len(proto)) + proto


def grpc_web_trailer(data: bytes) -> tuple[str | None, str]:
    """Извлекает grpc-status и grpc-message из кадров gRPC-Web в теле ответа"""
    offset = 0
    status: str | None = None
    message = ""
    while offset + 5 <= len(data):
        flags = data[offset]
        size = struct.unpack(">I", data[offset + 1:offset + 5])[0]
        offset += 5
        if offset + size > len(data):
            break
        payload = data[offset:offset + size]
        offset += size
        if flags & 0x80:
            for line in payload.decode("utf-8", errors="replace").splitlines():
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                if key.lower() == "grpc-status":
                    status = value.strip()
                elif key.lower() == "grpc-message":
                    message = unquote_plus(value.strip())
    return status, message


class App:
    BG, CARD, FIELD = "#0f1929", "#192841", "#0a1424"
    TEXT, MUTED, BLUE, SECONDARY, RED = "#f4f7ff", "#9fc5ff", "#397cff", "#29466f", "#ca435b"

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        self.settings = load_settings()
        try:
            raw_accounts = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
            self.accounts = [item for item in raw_accounts if isinstance(item, dict) and isinstance(item.get("name"), str) and (item.get("id") == "legacy" or self.valid_account_id(item.get("id")))]
        except (OSError, ValueError, TypeError):
            self.accounts = []
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop = threading.Event()
        self.scan_stop = threading.Event()
        self.scan_thread = None
        self.scanning = False
        self.browser_busy = False
        self.last_success = 0.0
        try:
            self.receiver = FrameReceiver()
            self.receiver.enabled = lambda: self.scanning
            self.receiver.presence_confirmed = lambda: self.log("PRESENCE", "Окно MTS Link исчезло после нажатия на подключённой вкладке")
        except OSError:
            self.receiver = None
            LOGGER.exception("Не удалось запустить приём фоновой вкладки")
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("760x670")
        self.root.minsize(680, 670)
        self.root.configure(bg=self.BG)
        if APP_ICON.exists():
            self.root.iconbitmap(default=str(APP_ICON))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.styles()
        self.build()
        self.root.update_idletasks()
        self.enable_rounded_window()
        self.log("UI", "Интерфейс готов.")
        self.root.after(150, self.pump)
        if self.receiver is None:
            self.post("status", "Фоновая вкладка недоступна: занят порт 47881")
        if not self.accounts and CHROME_PROFILE.exists() and not ACCOUNTS_PATH.exists():
            self.root.after(1000, self.check_session)

    @staticmethod
    def valid_account_id(value) -> bool:
        try:
            return str(UUID(value)) == value
        except (ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def account_path(account_id: str) -> Path:
        if account_id == "legacy":
            return CHROME_PROFILE
        if not App.valid_account_id(account_id):
            raise ValueError("Некорректный профиль")
        return PROFILES_DIR / account_id

    def save_accounts(self) -> None:
        temporary = ACCOUNTS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.accounts, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(ACCOUNTS_PATH)

    def refresh_accounts(self) -> None:
        self.account_label.configure(text=f"Аккаунты добавлены: {len(self.accounts)}")
        self.account_list.delete(0, "end")
        for account in self.accounts:
            self.account_list.insert("end", account["name"])

    def styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=(12, 8), background=self.BLUE, foreground="white", borderwidth=0)
        style.map("TButton", background=[("active", "#5b92ff")])
        style.configure("Secondary.TButton", background=self.SECONDARY)
        style.configure("Danger.TButton", background=self.RED)
        style.configure("TEntry", fieldbackground=self.FIELD, foreground=self.TEXT)
        style.layout("Slim.Vertical.TScrollbar", [
            ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})
            ]})
        ])
        style.configure("Slim.Vertical.TScrollbar", background="#38577f", troughcolor=self.BG, borderwidth=0, relief="flat", width=10)
        style.map("Slim.Vertical.TScrollbar", background=[("active", "#5986bd"), ("pressed", "#397cff")])

    def enable_rounded_window(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            preference = ctypes.c_int(2)  # Скруглённый режим DWM
            hwnd = ctypes.windll.user32.GetAncestor(self.root.winfo_id(), 2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference))
        except Exception:
            pass

    def label(self, parent, text, size=10, bold=False, color=None, **kwargs):
        return tk.Label(parent, text=text, bg=self.CARD, fg=color or self.TEXT, font=("Segoe UI", size, "bold" if bold else "normal"), **kwargs)

    def card(self, parent):
        return self.round_widget(tk.Frame(parent, bg=self.CARD, highlightbackground="#2d4971", highlightthickness=1, padx=14, pady=12), 12)

    def round_widget(self, widget, radius=9):
        """Обрезает дочернее окно Tk по скруглённому прямоугольнику Windows"""
        if sys.platform != "win32":
            return widget
        def apply(_event=None):
            try:
                import ctypes
                width, height = widget.winfo_width(), widget.winfo_height()
                if width > 2 and height > 2:
                    region = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
                    ctypes.windll.user32.SetWindowRgn(widget.winfo_id(), region, True)
            except Exception:
                pass
        widget.bind("<Configure>", apply, add="+")
        widget.after_idle(apply)
        return widget

    def button(self, parent, **kwargs):
        return self.round_widget(ttk.Button(parent, **kwargs), 8)

    def entry(self, parent, **kwargs):
        return self.round_widget(ttk.Entry(parent, **kwargs), 7)

    def build(self) -> None:
        outer = tk.Frame(self.root, bg=self.BG, padx=20, pady=18)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text=APP_NAME, bg=self.BG, fg=self.TEXT, font=("Segoe UI", 22, "bold")).pack(anchor="w")
        nav = tk.Frame(outer, bg=self.BG, pady=12)
        nav.pack(fill="x")
        self.button(nav, text="Главная", command=self.show_main).pack(side="left")
        self.button(nav, text="Настройки", style="Secondary.TButton", command=self.show_settings).pack(side="left", padx=8)
        self.content = tk.Frame(outer, bg=self.BG)
        self.content.pack(fill="both", expand=True)
        self.main_page, self.settings_page = tk.Frame(self.content, bg=self.BG), tk.Frame(self.content, bg=self.BG)
        self.build_main()
        self.build_settings()
        footer = tk.Frame(outer, bg=self.BG)
        footer.pack(fill="x", pady=(8, 0))
        tk.Label(footer, text="Сделано ", bg=self.BG, fg="#9db1ce", font=("Segoe UI", 9)).pack(side="left")
        self.link(footer, "FOTUR", "https://fotur.tech").pack(side="left")
        tk.Label(footer, text=" для студентов by ", bg=self.BG, fg="#9db1ce", font=("Segoe UI", 9)).pack(side="left")
        self.link(footer, "@Woonze", "https://github.com/Woonze").pack(side="left")
        self.show_main()

    def link(self, parent, text, url):
        item = tk.Label(parent, text=text, bg=parent.cget("bg"), fg="#77adff", cursor="hand2", font=("Segoe UI", 9, "underline"))
        item.bind("<Button-1>", lambda _event: webbrowser.open(url))
        return item

    def build_main(self) -> None:
        state = self.card(self.main_page)
        state.pack(fill="x", pady=(0, 10))
        self.account_label = self.label(state, f"Аккаунты добавлены: {len(self.accounts)}", bold=True, color="#cfe1ff")
        self.account_label.pack(anchor="w")
        self.status_label = self.label(state, "Готово к работе" if self.accounts else "Добавьте аккаунт Pulse в настройках", color=self.MUTED)
        self.status_label.pack(anchor="w", pady=(6, 10))
        controls = tk.Frame(state, bg=self.CARD)
        controls.pack(anchor="w")
        self.button(controls, text="Управление аккаунтами", style="Secondary.TButton", command=self.show_settings).pack(side="left", padx=(0, 8))
        self.scan_button = self.button(controls, text="Начать сканирование", command=self.toggle_scan)
        self.scan_button.pack(side="left")
        logs = self.card(self.main_page)
        logs.pack(fill="both", expand=True)
        self.label(logs, "Журнал работы", 11, True).pack(anchor="w")
        self.label(logs, f"Файл: {LOG_PATH}", 9, color=self.MUTED).pack(anchor="w", pady=(4, 8))
        self.console = tk.Text(logs, height=12, bg=self.FIELD, fg="#dcebff", insertbackground="white", relief="flat", wrap="word", font=("Cascadia Mono", 9), padx=10, pady=8)
        self.console.pack(fill="both", expand=True)
        self.console.configure(state="disabled")

    def build_settings(self) -> None:
        canvas = tk.Canvas(self.settings_page, bg=self.BG, highlightthickness=0)
        self.settings_canvas = canvas
        scrollbar = ttk.Scrollbar(self.settings_page, orient="vertical", command=canvas.yview, style="Slim.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = tk.Frame(canvas, bg=self.BG)
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.root.bind_all("<MouseWheel>", lambda event: self.scroll_settings(event, canvas), add="+")
        scan = self.card(content)
        scan.pack(fill="x", pady=(0, 10))
        self.label(scan, "Сканирование", 11, True).pack(anchor="w")
        self.label(scan, "Пауза после успешного подтверждения, минут.", 9, color=self.MUTED).pack(anchor="w", pady=(6, 5))
        row = tk.Frame(scan, bg=self.CARD)
        row.pack(anchor="w")
        self.cooldown = tk.StringVar(value=str(self.settings["cooldown_minutes"]))
        self.entry(row, width=8, textvariable=self.cooldown).pack(side="left")
        self.label(row, "минут").pack(side="left", padx=8)
        self.button(row, text="Сохранить", command=self.save_cooldown).pack(side="left")
        support = self.card(content)
        support.pack(fill="x", pady=(0, 10))
        self.label(support, "Поддержка", 11, True).pack(anchor="w")
        support_row = tk.Frame(support, bg=self.CARD)
        support_row.pack(fill="x", pady=(6, 0))
        left = tk.Frame(support_row, bg=self.CARD)
        left.pack(side="left", fill="x", expand=True)
        self.label(left, "Поддержите автора: подключите FOTUR VPN\nили поддержите используя QR.", 9, color=self.MUTED, justify="left").pack(anchor="w")
        buttons = tk.Frame(left, bg=self.CARD)
        buttons.pack(anchor="w", pady=(8, 0))
        self.button(buttons, text="Открыть @foturvpnbot", command=lambda: webbrowser.open("https://t.me/foturvpnbot")).pack(side="left")
        self.button(buttons, text="Поддержать донатом", style="Secondary.TButton", command=lambda: webbrowser.open(CLOUDTIPS_URL)).pack(side="left", padx=8)
        if QR_IMAGE.exists():
            try:
                from PIL import Image, ImageTk
                with Image.open(QR_IMAGE) as image:
                    image.thumbnail((110, 110))
                    self.donation_qr = ImageTk.PhotoImage(image.copy())
                qr = tk.Label(support_row, image=self.donation_qr, bg=self.CARD, cursor="hand2")
                qr.pack(side="right", padx=(10, 0))
                qr.bind("<Button-1>", lambda _event: webbrowser.open(CLOUDTIPS_URL))
            except tk.TclError:
                LOGGER.exception("Donation QR cannot be loaded")
        profile = self.card(content)
        profile.pack(fill="x")
        self.label(profile, "Аккаунты Pulse", 11, True).pack(anchor="w")
        self.label(profile, str(DATA_DIR), 9, True, "#cfe1ff").pack(anchor="w", pady=(6, 3))
        self.account_list = tk.Listbox(profile, height=3, bg=self.FIELD, fg=self.TEXT, selectbackground=self.SECONDARY, relief="flat", exportselection=False)
        self.account_list.pack(fill="x", pady=(3, 4))
        for account in self.accounts:
            self.account_list.insert("end", account["name"])
        buttons = tk.Frame(profile, bg=self.CARD)
        buttons.pack(anchor="w", pady=(8, 0))
        self.button(buttons, text="Копировать путь", command=self.copy_profile).pack(side="left")
        self.button(buttons, text="Открыть в Проводнике", style="Secondary.TButton", command=lambda: os.startfile(DATA_DIR)).pack(side="left", padx=8)
        actions = tk.Frame(profile, bg=self.CARD)
        actions.pack(anchor="w", pady=(6, 0))
        self.button(actions, text="Добавить аккаунт", command=self.open_login).pack(side="left")
        self.button(actions, text="Войти заново", style="Secondary.TButton", command=self.relogin).pack(side="left", padx=8)
        self.button(actions, text="Удалить выбранный", style="Danger.TButton", command=self.logout).pack(side="left")
        background = self.card(content)
        self.background_settings = background
        background.pack(fill="x", pady=(10, 0))
        self.label(background, "Фоновая вкладка Chrome", 11, True).pack(anchor="w")
        self.label(background, "1. Нажмите «Открыть папку расширения» и оставьте её открытой.", 9, color=self.MUTED).pack(anchor="w", pady=(6, 0))
        extension_step = tk.Frame(background, bg=self.CARD)
        extension_step.pack(anchor="w")
        self.label(extension_step, "2. В Chrome откройте ", 9, color=self.MUTED).pack(side="left")
        extension_link = tk.Label(extension_step, text="chrome://extensions", bg=self.CARD, fg="#77adff", cursor="hand2", font=("Segoe UI", 9, "underline"))
        extension_link.pack(side="left")
        extension_link.bind("<Button-1>", lambda _event: self.open_chrome_extensions())
        self.label(extension_step, ", включите «Режим разработчика»,", 9, color=self.MUTED).pack(side="left")
        self.label(
            background,
            "    нажмите «Загрузить распакованное расширение» и выберите эту папку.\n"
            "3. Откройте вкладку с трансляцией и нажмите значок расширения Shalost.\n"
            "4. Нажмите «Копировать ключ», вставьте его в расширение и выберите\n"
            "    «Подключить вкладку». Затем запустите сканирование на главной.\n\n"
            "Ключ связывает расширение с приложением и меняется после его перезапуска.",
            9, color=self.MUTED, justify="left", anchor="w",
        ).pack(anchor="w")
        background_buttons = tk.Frame(background, bg=self.CARD)
        background_buttons.pack(anchor="w", pady=(8, 0))
        self.button(background_buttons, text="Открыть папку расширения", command=lambda: os.startfile(EXTENSION_DIR)).pack(side="left")
        self.button(background_buttons, text="Копировать ключ", style="Secondary.TButton", command=self.copy_extension_key).pack(side="left", padx=8)

    def open_chrome_extensions(self) -> None:
        if sys.platform == "win32":
            try:
                if self.open_extensions_in_existing_chrome():
                    return
            except OSError as exc:
                messagebox.showerror(APP_NAME, "Не удалось открыть вкладку в существующем окне Chrome: " + self.short(exc))
                return
        candidates = [
            shutil.which("chrome"),
            str(Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ]
        chrome = next((path for path in candidates if path and Path(path).is_file()), None)
        if not chrome:
            messagebox.showerror(APP_NAME, "Google Chrome не найден. Откройте chrome://extensions вручную в Chrome")
            return
        try:
            subprocess.Popen([chrome, "--new-tab", "chrome://extensions"], close_fds=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, "Не удалось открыть Chrome: " + self.short(exc))

    @staticmethod
    def open_extensions_in_existing_chrome() -> bool:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        windows = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def collect(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            class_name = ctypes.create_unicode_buffer(256)
            title = ctypes.create_unicode_buffer(1024)
            user32.GetClassNameW(hwnd, class_name, len(class_name))
            user32.GetWindowTextW(hwnd, title, len(title))
            if class_name.value != "Chrome_WidgetWin_1" or not title.value.endswith(" - Google Chrome"):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = kernel32.OpenProcess(0x1000, False, pid.value)
            if process:
                try:
                    path = ctypes.create_unicode_buffer(32768)
                    length = wintypes.DWORD(len(path))
                    if kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(length)) and Path(path.value).name.lower() == "chrome.exe":
                        windows.append(hwnd)
                finally:
                    kernel32.CloseHandle(process)
            return True

        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.EnumWindows(collect, 0)
        if not windows:
            return False
        hwnd = windows[0]
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        if not user32.SetForegroundWindow(hwnd) or user32.GetForegroundWindow() != hwnd:
            raise OSError("Windows не разрешила активировать окно браузера")

        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

        class KeyboardInput(ctypes.Structure):
            _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

        class HardwareInput(ctypes.Structure):
            _fields_ = [("message", wintypes.DWORD), ("low", wintypes.WORD), ("high", wintypes.WORD)]

        class InputData(ctypes.Union):
            _fields_ = [("mouse", MouseInput), ("keyboard", KeyboardInput), ("hardware", HardwareInput)]

        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("data", InputData)]

        def key(vk: int, scan: int = 0, flags: int = 0) -> Input:
            return Input(1, InputData(keyboard=KeyboardInput(vk, scan, flags, 0, 0)))

        inputs = [key(0x11), key(0x54), key(0x54, flags=2), key(0x11, flags=2)]
        for character in "chrome://extensions":
            inputs.extend((key(0, ord(character), 4), key(0, ord(character), 6)))
        inputs.extend((key(0x0D), key(0x0D, flags=2)))
        batch = (Input * len(inputs))(*inputs)
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        if user32.SendInput(len(batch), batch, ctypes.sizeof(Input)) != len(batch):
            raise OSError("Windows не разрешила передать нажатия браузеру")
        return True

    def warn_if_tab_disconnected(self, scan_stop: threading.Event) -> None:
        if scan_stop is not self.scan_stop or scan_stop.is_set() or not self.scanning:
            return
        if self.receiver and self.receiver.latest():
            return
        toast = self.round_widget(tk.Frame(self.root, bg=self.CARD, highlightbackground="#42638c", highlightthickness=1, padx=16, pady=12), 10)
        title = self.label(toast, "Вы ещё не подключили вкладку через расширение.", 10, True)
        title.pack(anchor="w")
        description = self.label(toast, "Чтобы вы могли свернуть вкладку — подключите расширение.\nПока программе нужно, чтобы QR был виден на экране.", 9, color=self.MUTED, justify="left")
        description.pack(anchor="w", pady=(5, 0))
        actions = tk.Frame(toast, bg=self.CARD)
        actions.pack(anchor="w", pady=(10, 0))
        self.button(actions, text="Подключить", command=lambda: connect()).pack(side="left")
        self.button(actions, text="Позже", style="Secondary.TButton", command=lambda: dismiss()).pack(side="left", padx=(8, 0))
        toast.place(relx=1, rely=1, x=-24, y=-28, anchor="se")
        toast.lift()

        after_id = None
        opacity = 0.0

        def blend(start: str, end: str, value: float) -> str:
            first = self.root.winfo_rgb(start)
            last = self.root.winfo_rgb(end)
            return "#" + "".join(f"{round((a + (b - a) * value) / 257):02x}" for a, b in zip(first, last))

        def fade(value: float, direction: int) -> None:
            nonlocal after_id, opacity
            if not toast.winfo_exists():
                return
            value = max(0.0, min(1.0, value + direction * 0.1))
            opacity = value
            background = blend(self.BG, self.CARD, value)
            toast.configure(bg=background, highlightbackground=blend(self.BG, "#42638c", value))
            actions.configure(bg=background)
            title.configure(bg=background, fg=blend(self.BG, self.TEXT, value))
            description.configure(bg=background, fg=blend(self.BG, self.MUTED, value))
            if direction > 0 and value >= 1:
                after_id = None
            elif direction < 0 and value <= 0:
                toast.destroy()
            else:
                after_id = toast.after(35, lambda: fade(value, direction))

        def dismiss() -> None:
            if not toast.winfo_exists():
                return
            if after_id is not None:
                toast.after_cancel(after_id)
            fade(opacity, -1)

        def connect() -> None:
            self.show_settings()
            self.root.update_idletasks()
            region = self.settings_canvas.bbox("all")
            if region and region[3] > 0:
                self.settings_canvas.yview_moveto(self.background_settings.winfo_y() / region[3])
            dismiss()

        fade(0.0, 1)

    def scroll_settings(self, event, canvas) -> None:
        if not self.settings_page.winfo_ismapped():
            return
        widget = event.widget
        if isinstance(widget, tk.Listbox) and widget.yview() != (0.0, 1.0):
            return
        while widget is not None and widget is not self.settings_page:
            widget = getattr(widget, "master", None)
        if widget is self.settings_page and event.delta:
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def copy_extension_key(self) -> None:
        if self.receiver is None:
            messagebox.showerror(APP_NAME, "Фоновая вкладка недоступна: порт 47881 занят")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.receiver.key)
        self.post("status", "Ключ расширения скопирован")

    def show_main(self) -> None:
        self.settings_page.pack_forget()
        self.main_page.pack(fill="both", expand=True)

    def show_settings(self) -> None:
        self.main_page.pack_forget()
        self.settings_page.pack(fill="both", expand=True)

    def log(self, tag: str, message: str) -> None:
        LOGGER.info("[%s] %s", tag, message)
        self.events.put(("log", (tag, message)))

    def pump(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    tag, message = value
                    self.console.configure(state="normal")
                    self.console.insert("end", f"{time.strftime('%H:%M:%S')} | {tag:<7} {message}\n")
                    self.console.see("end")
                    self.console.configure(state="disabled")
                elif kind == "status":
                    self.status_label.configure(text=value)
                elif kind == "account_added":
                    if not any(item["id"] == value["id"] for item in self.accounts):
                        self.accounts.append(value)
                        self.save_accounts()
                    self.refresh_accounts()
                elif kind == "scan":
                    self.scan_button.configure(text="Остановить сканирование" if value else "Начать сканирование")
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.root.after(150, self.pump)

    def post(self, kind: str, value: object) -> None:
        self.events.put((kind, value))

    def save_cooldown(self) -> None:
        try:
            minutes = int(self.cooldown.get())
            if not 1 <= minutes <= 180:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Введите целое число от 1 до 180")
            return
        self.settings["cooldown_minutes"] = minutes
        SETTINGS_PATH.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log("SETTINGS", f"Пауза после успеха: {minutes} мин.")
        self.post("status", "Настройки сохранены")

    def copy_profile(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(str(DATA_DIR))
        self.post("status", "Путь профиля скопирован")

    def state(self, page) -> tuple[str, bool]:
        page.goto(PULSE_HOME, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        state = page.evaluate(STATE_JS)
        name = str(state.get("name", "")).strip()
        return name, bool(name) and bool(state.get("onPulse"))

    def check_session(self) -> None:
        if self.browser_busy:
            return
        self.browser_busy = True
        self.post("status", "Проверяю сохранённую сессию…")
        threading.Thread(target=self.check_worker, daemon=True).start()

    def check_worker(self) -> None:
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(str(CHROME_PROFILE), channel="chrome", headless=True)
                name, logged = self.state(ctx.pages[0] if ctx.pages else ctx.new_page())
                ctx.close()
            if logged:
                self.post("account_added", {"id": "legacy", "name": name})
                self.post("status", "Сессия активна")
                self.log("AUTH", f"Сессия активна: {name}.")
            else:
                self.post("status", "Войдите в Pulse")
                self.log("AUTH", "Сессия не найдена")
        except Exception as exc:
            LOGGER.exception("Session check failure")
            self.post("status", "Не удалось проверить сессию — откройте вход")
            self.log("AUTH", "Проверка сессии недоступна: " + self.short(exc))
        finally:
            self.browser_busy = False

    def open_login(self) -> None:
        if self.scan_thread and self.scan_thread.is_alive():
            self.post("status", "Остановите сканирование перед добавлением аккаунта")
            return
        if self.browser_busy:
            self.post("status", "Уже выполняется проверка или вход…")
            return
        self.browser_busy = True
        account_id = str(uuid4())
        self.post("status", "Завершите вход и 2FA в окне Chrome")
        self.log("AUTH", "Открыто официальное окно Pulse для входа")
        threading.Thread(target=self.login_worker, args=(account_id,), daemon=True).start()

    def relogin(self) -> None:
        if self.scan_thread and self.scan_thread.is_alive():
            self.post("status", "Остановите сканирование перед повторным входом")
            return
        if self.browser_busy:
            self.post("status", "Уже выполняется проверка или вход…")
            return
        selected = self.account_list.curselection()
        if not selected:
            messagebox.showinfo(APP_NAME, "Выберите аккаунт для повторного входа")
            return
        account = self.accounts[selected[0]]
        self.browser_busy = True
        self.post("status", "Войдите в выбранный аккаунт Pulse в окне Chrome")
        self.log("AUTH", f"Повторный вход: {account['name']}")
        threading.Thread(target=self.login_worker, args=(account["id"], True), daemon=True).start()

    def login_worker(self, account_id: str, reconnect: bool = False) -> None:
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(str(self.account_path(account_id)), channel="chrome", headless=False)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(PULSE_HOME, wait_until="domcontentloaded", timeout=60000)
                for _ in range(600):
                    if self.stop.wait(1):
                        break
                    pages = [candidate for candidate in ctx.pages if not candidate.is_closed()]
                    ui_auth = False
                    pulse_name = ""
                    for candidate in pages:
                        try:
                            state = candidate.evaluate(STATE_JS)
                            if state.get("onPulse"):
                                ui_auth = ui_auth or bool(state.get("authenticatedUI"))
                                if state.get("name"):
                                    pulse_name = str(state["name"]).strip()
                        except PlaywrightError:
                            continue

                    if ui_auth:
                        name = pulse_name or f"Аккаунт {len(self.accounts) + 1}"
                        ctx.close()
                        if not reconnect:
                            self.post("account_added", {"id": account_id, "name": name})
                        self.post("status", "Сессия активна")
                        self.log("AUTH", f"Вход завершён: {name}.")
                        return
                ctx.close()
                self.post("status", "Вход не подтверждён — откройте Pulse ещё раз")
                self.log("AUTH", "Окно входа закрыто до подтверждения сессии")
        except Exception as exc:
            LOGGER.exception("Login failure")
            self.post("status", "Не удалось открыть Chrome: " + self.short(exc))
            self.log("AUTH", "Ошибка запуска официального окна: " + self.short(exc))
        finally:
            self.browser_busy = False

    def toggle_scan(self) -> None:
        if self.scanning:
            self.scanning = False
            self.scan_stop.set()
            if self.receiver:
                with self.receiver.lock:
                    self.receiver.frame = None
            self.post("scan", False)
            self.post("status", "Сканирование остановлено")
            self.log("SCAN", "Поиск QR остановлен")
            return
        if self.scan_thread and self.scan_thread.is_alive():
            self.post("status", "Дождитесь завершения текущего запроса")
            return
        if not self.accounts:
            self.post("status", "Сначала добавьте аккаунт Pulse в настройках")
            return
        if self.browser_busy:
            self.post("status", "Дождитесь завершения входа")
            return
        self.scanning = True
        self.scan_stop = threading.Event()
        self.post("scan", True)
        self.post("status", "Ищу QR на экранах…")
        self.log("SCAN", "Начат поиск QR на всех экранах (каждые 2 секунды)")
        self.scan_thread = threading.Thread(target=self.scan_worker, args=(self.scan_stop,), daemon=True)
        self.scan_thread.start()
        self.root.after(5000, lambda scan_stop=self.scan_stop: self.warn_if_tab_disconnected(scan_stop))

    def scan_worker(self, scan_stop: threading.Event) -> None:
        try:
            from PIL import ImageGrab
            import zxingcpp
        except Exception as exc:
            self.log("SCAN", "Не удалось загрузить модуль сканирования: " + self.short(exc))
            self.scanning = False
            self.post("scan", False)
            return
        confirmed_tokens: set[str] = set()
        partial_successes: dict[str, set[str]] = {}
        qr_successes = 0
        presence_pending = False
        expired_accounts: set[str] = set()
        while not scan_stop.is_set() and not self.stop.is_set():
            try:
                screenshot = ImageGrab.grab(all_screens=True)
                presence_button = self.find_presence_button(screenshot)
                presence_activity = bool(presence_button)
                if presence_button:
                    self.click_screen_point(*presence_button)
                    presence_pending = True
                    self.post("status", "Подтверждаю присутствие в MTS Link…")
                elif presence_pending:
                    presence_pending = False
                    presence_activity = True
                    self.log("PRESENCE", "Окно контроля присутствия не найдено после нажатия")
                    self.post("status", "Окно контроля присутствия исчезло")

                if time.time() - self.last_success >= int(self.settings["cooldown_minutes"]) * 60:
                    if self.last_success:
                        self.last_success = 0.0
                        confirmed_tokens.clear()
                        partial_successes.clear()
                        qr_successes = 0
                    codes = list(zxingcpp.read_barcodes(screenshot))
                    frame = self.receiver.latest() if self.receiver else None
                    if frame:
                        from PIL import Image
                        with Image.open(BytesIO(frame)) as background_image:
                            codes.extend(zxingcpp.read_barcodes(background_image.convert("RGB")))
                    token = None
                    for code in codes:
                        candidate = qr_token(code.text)
                        if candidate and candidate not in confirmed_tokens:
                            token = candidate
                            break
                    if token and token not in confirmed_tokens:
                        attempt = qr_successes + 1
                        self.log("SCAN", f"Найден новый QR Pulse. Подтверждение {attempt}/3")
                        self.post("status", f"Найден новый QR — подтверждение {attempt}/3…")
                        completed = partial_successes.setdefault(token, set())
                        for account in list(self.accounts):
                            if scan_stop.is_set() or self.stop.is_set():
                                break
                            if account["id"] in completed or account["id"] in expired_accounts:
                                continue
                            ok, detail = self.approve(token, account)
                            if ok:
                                completed.add(account["id"])
                                self.log("PULSE", f"{account['name']}: подтверждено")
                            else:
                                if detail == "HTTP 401":
                                    expired_accounts.add(account["id"])
                                    detail = "сессия отклонена (HTTP 401); остановите сканирование и войдите заново в настройках"
                                self.log("PULSE", f"{account['name']}: ошибка — {detail}")
                        ok = all(account["id"] in completed for account in self.accounts)
                        detail = "не для всех аккаунтов" if not ok else ""
                        if ok:
                            partial_successes.pop(token, None)
                            confirmed_tokens.add(token)
                            qr_successes += 1
                            if qr_successes == 3:
                                self.last_success = time.time()
                                self.log("PULSE", "Три разных QR подтверждены: 3/3. Включена пауза QR")
                                self.post("status", "Посещение подтверждено: 3/3")
                            else:
                                self.log("PULSE", f"Новый QR подтверждён: {qr_successes}/3")
                                self.post("status", f"Посещение подтверждено: {qr_successes}/3")
                        else:
                            confirmed_tokens.clear()
                            qr_successes = 0
                            self.log("PULSE", "Подтверждение отклонено: " + detail)
                            if expired_accounts:
                                self.post("status", "Сессия отклонена: остановите сканирование и войдите заново в настройках")
                                # Восстановление входа требует остановленного сканирования.
                                self.scanning = False
                                self.post("scan", False)
                                return
                            self.post("status", "Ошибка Pulse: " + detail)
                elif not presence_activity:
                    self.post("status", "Пауза после подтверждения QR")
                scan_stop.wait(2)
            except Exception as exc:
                LOGGER.exception("Scanner failure")
                self.log("SCAN", "Ошибка сканирования: " + self.short(exc))
                scan_stop.wait(2)

    @staticmethod
    def find_presence_button(image) -> tuple[int, int] | None:
        """Возвращает центр кнопки подтверждения MTS Link, если она видна
        В рабочем окне и демо используется ярко-фиолетовая кнопка
        Перед кликом проверяется сплошной фиолетовый прямоугольник в нескольких строках
        Обычные элементы интерфейса и QR-коды этому условию не соответствуют
        """
        rgb = image.convert("RGB")
        width, height = rgb.size
        pixels = rgb.load()
        candidates: list[tuple[int, int, int]] = []
        for y in range(0, height, 2):
            x = 0
            while x < width:
                red, green, blue = pixels[x, y]
                if not (130 <= red <= 190 and green <= 65 and 185 <= blue <= 255):
                    x += 2
                    continue
                start = x
                while x < width:
                    red, green, blue = pixels[x, y]
                    if not (130 <= red <= 190 and green <= 65 and 185 <= blue <= 255):
                        break
                    x += 2
                run = x - start
                if 70 <= run <= 220:
                    candidates.append((start, y, run))
                x += 2

        for start, y, run in candidates:
            supporting_rows = sum(
                1
                for sx, sy, sw in candidates
                if abs(sx - start) <= 8 and abs(sw - run) <= 16 and abs(sy - y) <= 24
            )
            if supporting_rows >= 5:
                center_x = start + run // 2
                # Кнопка находится в белом модальном окне
                # Это исключает аватары, бейджи и другие фиолетовые элементы страницы
                white_samples = 0
                for sample_x, sample_y in (
                    (center_x, y - 42),
                    (center_x - 80, y - 42),
                    (center_x + 80, y - 42),
                    (center_x - 110, y + 16),
                    (center_x + 110, y + 16),
                ):
                    if 0 <= sample_x < width and 0 <= sample_y < height:
                        red, green, blue = pixels[sample_x, sample_y]
                        white_samples += red >= 205 and green >= 205 and blue >= 205
                if white_samples >= 3:
                    return center_x, y + 18
        return None

    @staticmethod
    def click_screen_point(x: int, y: int) -> None:
        if sys.platform != "win32":
            return
        import ctypes
        # PIL возвращает координаты внутри объединённого скриншота
        # Координаты мыши Windows отсчитываются от начала виртуального рабочего стола
        # При мониторе слева начало виртуального стола может быть отрицательным
        user32 = ctypes.windll.user32
        x += user32.GetSystemMetrics(76)  # Координата X виртуального стола
        y += user32.GetSystemMetrics(77)  # Координата Y виртуального стола
        user32.SetCursorPos(x, y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # Нажатие левой кнопки
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # Отпускание левой кнопки

    def approve(self, token: str, account: dict) -> tuple[bool, str]:
        if self.browser_busy:
            return False, "завершите вход в Pulse"
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(str(self.account_path(account["id"])), channel="chrome", headless=True)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(PULSE_HOME, wait_until="domcontentloaded", timeout=60000)
                result = page.evaluate("""async ({url, body}) => {
                  const r = await fetch(url, {method: 'POST', credentials: 'include', headers: {
                    'content-type': 'application/grpc-web+proto', 'x-grpc-web': '1',
                    'x-requested-with': 'XMLHttpRequest', 'pulse-app-type': 'pulse'}, body: Uint8Array.from(body)});
                  return {status:r.status, bytes:[...new Uint8Array(await r.arrayBuffer())]};
                }""", {"url": PULSE_RPC, "body": list(grpc_body(token))})
                ctx.close()
            grpc_status, grpc_message = grpc_web_trailer(bytes(result.get("bytes", [])))
            if result.get("status") == 200 and grpc_status == "0":
                return True, ""
            if result.get("status") == 401:
                return False, "HTTP 401"
            detail = grpc_message or f"HTTP {result.get('status')}; gRPC {grpc_status or 'не указан'}"
            return False, detail
        except Exception as exc:
            LOGGER.exception("Approval failure")
            return False, self.short(exc)

    def logout(self) -> None:
        if self.browser_busy or (self.scan_thread and self.scan_thread.is_alive()):
            messagebox.showinfo(APP_NAME, "Сначала остановите сканирование и завершите вход в Pulse")
            return
        selected = self.account_list.curselection()
        if not selected:
            messagebox.showinfo(APP_NAME, "Выберите аккаунт для удаления")
            return
        account = self.accounts[selected[0]]
        if not messagebox.askyesno(APP_NAME, f"Удалить аккаунт {account['name']}? Потребуется войти заново"):
            return
        try:
            shutil.rmtree(self.account_path(account["id"]))
            self.accounts.remove(account)
            self.save_accounts()
            self.refresh_accounts()
            self.post("status", "Аккаунт удалён")
            self.log("AUTH", f"Аккаунт удалён: {account['name']}")
        except OSError as exc:
            messagebox.showerror(APP_NAME, "Не удалось удалить профиль. Закройте окно Chrome Pulse и повторите\n\n" + str(exc))

    @staticmethod
    def short(exc: BaseException) -> str:
        return str(exc).replace("\n", " ").strip()[:180] or exc.__class__.__name__

    def close(self) -> None:
        self.scanning = False
        self.stop.set()
        self.scan_stop.set()
        if self.receiver:
            self.receiver.server.shutdown()
            self.receiver.server.server_close()
        self.log("UI", "Приложение закрыто")
        self.root.after(100, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
