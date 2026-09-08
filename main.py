"""Нативное приложение Shalost FOTUR для Windows"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import struct
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse
from uuid import UUID

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

APP_NAME = "Shalost FOTUR"
PULSE_HOME = "https://pulse.mirea.ru/"
PULSE_RPC = "https://pulse.mirea.ru/rtu_tc.attendance.api.AttendanceService/SelfApproveAttendanceThroughQRCode"
CLOUDTIPS_URL = "https://pay.cloudtips.ru/p/b58c4bc1"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Shalost"
CHROME_PROFILE = DATA_DIR / "pulse-chrome-profile"
SETTINGS_PATH = DATA_DIR / "settings.json"
RUN_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", RUN_DIR))
QR_IMAGE = BUNDLE_DIR / "assets" / "cloudtips-donation-qr.png"
APP_ICON = BUNDLE_DIR / "assets" / "shalost-fotur.ico"
APP_ICON_PNG = BUNDLE_DIR / "assets" / "shalost-fotur.png"


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

STATE_JS = """() => {
  const settingsLinks = [...document.querySelectorAll('a[href*="/settings"]')];
  const values = settingsLinks.map(x => (x.innerText || '').trim())
    .filter(x => x && !/настройки|settings/i.test(x));
  return {onPulse: location.hostname === 'pulse.mirea.ru', name: values.at(-1) || ''};
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
        CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
        self.settings = load_settings()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop = threading.Event()
        self.scanning = False
        self.browser_busy = False
        self.account = ""
        self.last_success = 0.0
        self.last_presence_click = 0.0
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
        self.root.after(1000, self.check_session)

    def styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=(12, 8), background=self.BLUE, foreground="white", borderwidth=0)
        style.map("TButton", background=[("active", "#5b92ff")])
        style.configure("Secondary.TButton", background=self.SECONDARY)
        style.configure("Danger.TButton", background=self.RED)
        style.configure("TEntry", fieldbackground=self.FIELD, foreground=self.TEXT)

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
        self.account_label = self.label(state, "Pulse: проверяю сессию…", bold=True, color="#cfe1ff")
        self.account_label.pack(anchor="w")
        self.status_label = self.label(state, "Запуск интерфейса…", color=self.MUTED)
        self.status_label.pack(anchor="w", pady=(6, 10))
        controls = tk.Frame(state, bg=self.CARD)
        controls.pack(anchor="w")
        self.login_button = self.button(controls, text="Войти в Pulse", command=self.open_login)
        self.login_button.pack(side="left")
        self.button(controls, text="Проверить сессию", style="Secondary.TButton", command=self.check_session).pack(side="left", padx=8)
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
        scan = self.card(self.settings_page)
        scan.pack(fill="x", pady=(0, 10))
        self.label(scan, "Сканирование", 11, True).pack(anchor="w")
        self.label(scan, "Пауза после успешного подтверждения, минут.", 9, color=self.MUTED).pack(anchor="w", pady=(6, 5))
        row = tk.Frame(scan, bg=self.CARD)
        row.pack(anchor="w")
        self.cooldown = tk.StringVar(value=str(self.settings["cooldown_minutes"]))
        self.entry(row, width=8, textvariable=self.cooldown).pack(side="left")
        self.label(row, "минут").pack(side="left", padx=8)
        self.button(row, text="Сохранить", command=self.save_cooldown).pack(side="left")
        support = self.card(self.settings_page)
        support.pack(fill="x", pady=(0, 10))
        self.label(support, "Поддержка", 11, True).pack(anchor="w")
        support_row = tk.Frame(support, bg=self.CARD)
        support_row.pack(fill="x", pady=(6, 0))
        left = tk.Frame(support_row, bg=self.CARD)
        left.pack(side="left", fill="x", expand=True)
        self.label(left, "Поддержите автора: подключите FOTUR VPN (Работаем через Hiddify)\nили поддержите используя QR.", 9, color=self.MUTED, justify="left").pack(anchor="w")
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
        profile = self.card(self.settings_page)
        profile.pack(fill="x")
        self.label(profile, "Профиль сессии Pulse", 11, True).pack(anchor="w")
        self.label(profile, str(CHROME_PROFILE), 9, True, "#cfe1ff").pack(anchor="w", pady=(6, 3))
        self.label(profile, "Chrome хранит здесь Cookie и данные входа. Не передавайте папку другим людям.", 9, color=self.MUTED).pack(anchor="w")
        buttons = tk.Frame(profile, bg=self.CARD)
        buttons.pack(anchor="w", pady=(8, 0))
        self.button(buttons, text="Копировать путь", command=self.copy_profile).pack(side="left")
        self.button(buttons, text="Открыть в Проводнике", style="Secondary.TButton", command=lambda: os.startfile(CHROME_PROFILE)).pack(side="left", padx=8)
        self.button(buttons, text="Выйти", style="Danger.TButton", command=self.logout).pack(side="left")

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
                elif kind == "account":
                    self.account = value
                    if value:
                        self.account_label.configure(text=f"Pulse: {value}")
                        self.login_button.pack_forget()
                    else:
                        self.account_label.configure(text="Pulse: вход не выполнен")
                        if not self.login_button.winfo_ismapped():
                            self.login_button.pack(side="left")
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
        self.root.clipboard_append(str(CHROME_PROFILE))
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
                self.post("account", name)
                self.post("status", "Сессия активна")
                self.log("AUTH", f"Сессия активна: {name}.")
            else:
                self.post("account", "")
                self.post("status", "Войдите в Pulse")
                self.log("AUTH", "Сессия не найдена")
        except Exception as exc:
            LOGGER.exception("Session check failure")
            self.post("account", "")
            self.post("status", "Не удалось проверить сессию — откройте вход")
            self.log("AUTH", "Проверка сессии недоступна: " + self.short(exc))
        finally:
            self.browser_busy = False

    def open_login(self) -> None:
        if self.browser_busy:
            self.post("status", "Уже выполняется проверка или вход…")
            return
        self.browser_busy = True
        self.post("status", "Завершите вход и 2FA в окне Chrome")
        self.log("AUTH", "Открыто официальное окно Pulse для входа")
        threading.Thread(target=self.login_worker, daemon=True).start()

    def login_worker(self) -> None:
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(str(CHROME_PROFILE), channel="chrome", headless=False)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(PULSE_HOME, wait_until="domcontentloaded", timeout=60000)
                for _ in range(600):
                    if self.stop.wait(1):
                        break
                    try:
                        state = page.evaluate(STATE_JS)
                        name = str(state.get("name", "")).strip()
                        if name and state.get("onPulse"):
                            ctx.close()
                            self.post("account", name)
                            self.post("status", "Сессия активна")
                            self.log("AUTH", f"Вход завершён: {name}.")
                            return
                    except PlaywrightError:
                        break
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
            self.stop.set()
            self.stop = threading.Event()
            self.post("scan", False)
            self.post("status", "Сканирование остановлено")
            self.log("SCAN", "Поиск QR остановлен")
            return
        if not self.account:
            self.post("status", "Сначала войдите в Pulse")
            return
        self.scanning = True
        self.post("scan", True)
        self.post("status", "Ищу QR на экранах…")
        self.log("SCAN", "Начат поиск QR на всех экранах (каждые 2 секунды)")
        threading.Thread(target=self.scan_worker, daemon=True).start()

    def scan_worker(self) -> None:
        try:
            from PIL import ImageGrab
            import zxingcpp
        except Exception as exc:
            self.log("SCAN", "Не удалось загрузить модуль сканирования: " + self.short(exc))
            self.scanning = False
            self.post("scan", False)
            return
        previous = ""
        while self.scanning and not self.stop.is_set():
            try:
                if time.time() - self.last_success < int(self.settings["cooldown_minutes"]) * 60:
                    self.post("status", "Пауза после подтверждения")
                    self.stop.wait(2)
                    continue
                screenshot = ImageGrab.grab(all_screens=True)
                if time.time() - self.last_presence_click >= 30:
                    presence_button = self.find_presence_button(screenshot)
                    if presence_button:
                        self.click_screen_point(*presence_button)
                        self.last_presence_click = time.time()
                        self.log("PRESENCE", "Найдено окно контроля присутствия. Нажата «ПОДТВЕРЖДАЮ»")
                        self.post("status", "Присутствие в онлайн-мероприятии подтверждено")
                        self.stop.wait(1)
                        continue
                codes = zxingcpp.read_barcodes(screenshot)
                token = None
                for code in codes:
                    token = qr_token(code.text)
                    if token:
                        break
                if token and token != previous:
                    previous = token
                    self.log("SCAN", "Найден QR Pulse. Отправляю запрос")
                    self.post("status", "Найден QR — подтверждаю…")
                    ok, detail = self.approve(token)
                    if ok:
                        self.last_success = time.time()
                        self.log("PULSE", "Посещение подтверждено")
                        self.post("status", "Посещение подтверждено")
                    else:
                        self.log("PULSE", "Подтверждение отклонено: " + detail)
                        self.post("status", "Ошибка Pulse: " + detail)
                self.stop.wait(2)
            except Exception as exc:
                LOGGER.exception("Scanner failure")
                self.log("SCAN", "Ошибка сканирования: " + self.short(exc))
                self.stop.wait(2)

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

    def approve(self, token: str) -> tuple[bool, str]:
        if self.browser_busy:
            return False, "завершите вход в Pulse"
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(str(CHROME_PROFILE), channel="chrome", headless=True)
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
            detail = grpc_message or f"HTTP {result.get('status')}; gRPC {grpc_status or 'не указан'}"
            return False, detail
        except Exception as exc:
            LOGGER.exception("Approval failure")
            return False, self.short(exc)

    def logout(self) -> None:
        if self.browser_busy:
            messagebox.showinfo(APP_NAME, "Сначала завершите проверку или вход в Pulse")
            return
        if not messagebox.askyesno(APP_NAME, "Сбросить сохранённую сессию Pulse? Потребуется войти заново"):
            return
        try:
            shutil.rmtree(CHROME_PROFILE)
            CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
            self.post("account", "")
            self.post("status", "Сессия сброшена")
            self.log("AUTH", "Сохранённая сессия удалена пользователем")
        except OSError as exc:
            messagebox.showerror(APP_NAME, "Не удалось удалить профиль. Закройте окно Chrome Pulse и повторите\n\n" + str(exc))

    @staticmethod
    def short(exc: BaseException) -> str:
        return str(exc).replace("\n", " ").strip()[:180] or exc.__class__.__name__

    def close(self) -> None:
        self.scanning = False
        self.stop.set()
        self.log("UI", "Приложение закрыто")
        self.root.after(100, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
