"""
Valorant Guard
A speed bump between you and VALORANT before game time (7:00 PM by default).

How it stops the Riot Client from opening first:
  1. Your VALORANT shortcuts (desktop and Start menu) run Valorant Guard
     instead of the Riot Client. The warning shows first, and the Riot
     Client only starts if you press "Continue anyway".
  2. A background guard starts with Windows. If the Riot Client is started
     any other way (an old shortcut, Riot's own autostart), it is shut down
     within a fraction of a second, before its window loads, and the same
     warning appears. Continue relaunches it for you.

Every open before game time asks again. A "Continue" covers one sitting
only. When you close VALORANT before game time, or close the Riot Client
window, the Riot Client is shut down completely (including its tray
process), so the next open is a fresh start and the warning shows again.

Modes:
  ValorantGuard.exe              background guard (starts with Windows)
  ValorantGuard.exe --launch     what your VALORANT shortcut runs
  ValorantGuard.exe --install    creates the shortcuts (install.bat runs it)
  python valorant_guard.py --test    preview the popup, nothing is launched

Standard library only. It never touches game memory, it only reads the
process list and closes Riot processes the way Task Manager would.
Riot Vanguard (vgc.exe) is never touched.
"""

import csv
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import os
import subprocess
import sys
import queue
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

# ---------------- Settings ----------------
CUTOFF_HOUR = 19        # 24h clock. 19 = 7 PM
CUTOFF_MINUTE = 0
CONTINUE_DELAY = 15     # seconds before "Continue anyway" unlocks. 0 = instant
POLL_MS = 300           # how often the background guard checks

# Any of these starting before game time is caught. RiotClientServices
# is the first process the Riot Client starts, well before its window.
WATCH_PROCESSES = [
    "RiotClientServices.exe",
    "Riot Client.exe",
    "RiotClientUx.exe",
    "RiotClientUxRender.exe",
    "VALORANT.exe",
    "VALORANT-Win64-Shipping.exe",
]
KILL_PROCESSES = WATCH_PROCESSES
GAME_PROCESSES = ["VALORANT.exe", "VALORANT-Win64-Shipping.exe"]
UI_PROCESSES = ["Riot Client.exe", "RiotClientUx.exe", "RiotClientUxRender.exe"]
IDLE_END_SECONDS = 4    # no game and no Riot window this long = sitting over
STARTUP_GRACE = 90      # seconds allowed for the client to show up after Continue
# ------------------------------------------

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ValorantGuard")
LOG_FILE = os.path.join(APP_DIR, "log.csv")
APPROVAL_FILE = os.path.join(APP_DIR, "approved")
APPROVAL_WINDOW = 90    # seconds a "Continue" counts as permission to launch
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
_WATCH = {n.lower() for n in WATCH_PROCESSES}
_GAME = {n.lower() for n in GAME_PROCESSES}
_UI = {n.lower() for n in UI_PROCESSES}
_mutex = None

# Palette
KEY = "#010203"          # transparent key for the rounded window corners
BG = "#0B1118"
SURFACE = "#131D27"
BORDER = "#223140"
ACCENT = "#FF4655"
ACCENT_HOVER = "#FF6470"
TEXT = "#F3EFE8"
MUTED = "#8A96A3"
DIM = "#3A4855"


# ================= Windows process helpers =================

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


if sys.platform == "win32":
    _k32 = ctypes.windll.kernel32
    _k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    _k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    _k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    _k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _INVALID = ctypes.c_void_p(-1).value
    _u32 = ctypes.windll.user32
    _EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    _u32.EnumWindows.argtypes = [_EnumProc, wt.LPARAM]
    _u32.IsWindowVisible.argtypes = [wt.HWND]
    _u32.GetWindowTextLengthW.argtypes = [wt.HWND]
    _u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]


def watched_procs():
    """{pid: exe name} for Riot / VALORANT processes, read straight from Windows."""
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == _INVALID:
        return {}
    procs = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.lower()
            if name in _WATCH:
                procs[entry.th32ProcessID] = name
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return procs


def visible_window_pids():
    """PIDs that own at least one visible, titled top-level window."""
    pids = set()

    def cb(hwnd, _):
        if _u32.IsWindowVisible(hwnd) and _u32.GetWindowTextLengthW(hwnd) > 0:
            pid = wt.DWORD()
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(pid.value)
        return True

    _u32.EnumWindows(_EnumProc(cb), 0)
    return pids


def kill_all():
    cmd = ["taskkill", "/F", "/T"]
    for name in KILL_PROCESSES:
        cmd += ["/IM", name]
    subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)


MUTEX_NAME = "Local\\ValorantGuardMutex"


def single_instance():
    global _mutex
    _mutex = _k32.CreateMutexW(None, False, MUTEX_NAME)
    if _k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)


def guard_is_running():
    h = _k32.OpenMutexW(0x00100000, False, MUTEX_NAME)  # SYNCHRONIZE
    if h:
        _k32.CloseHandle(h)
        return True
    return False


PAUSE_FILE = os.path.join(APP_DIR, "paused")


def set_paused(on):
    """Quit from the tray pauses the guard until your next Windows login,
    so the keepalive task doesn't bring it straight back."""
    try:
        if on:
            os.makedirs(APP_DIR, exist_ok=True)
            open(PAUSE_FILE, "w").close()
        elif os.path.exists(PAUSE_FILE):
            os.remove(PAUSE_FILE)
    except OSError:
        pass


def is_paused():
    return os.path.exists(PAUSE_FILE)


def start_guard():
    """Start the background guard if it isn't running (used by the launcher)."""
    if getattr(sys, "frozen", False) and not guard_is_running():
        subprocess.Popen([sys.executable, "--keepalive"], close_fds=True,
                         creationflags=DETACHED_PROCESS)


def log_error(text):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(os.path.join(APP_DIR, "error.log"), "a") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {text}\n")
    except Exception:
        pass


# ================= Riot Client =================

def riot_client_path():
    cfg = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                       "Riot Games", "RiotClientInstalls.json")
    try:
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
        for key in ("rc_default", "rc_live"):
            path = data.get(key)
            if path and os.path.exists(path):
                return os.path.normpath(path)
    except Exception:
        pass
    fallback = r"C:\Riot Games\Riot Client\RiotClientServices.exe"
    return fallback if os.path.exists(fallback) else None


def valorant_icon():
    for path in (r"C:\Riot Games\VALORANT\live\VALORANT.exe",
                 r"D:\Riot Games\VALORANT\live\VALORANT.exe"):
        if os.path.exists(path):
            return path
    return riot_client_path()


def launch_riot():
    path = riot_client_path()
    if not path:
        return False
    subprocess.Popen([path, "--launch-product=valorant", "--launch-patchline=live"],
                     close_fds=True, creationflags=DETACHED_PROCESS)
    return True


# ================= Time, log, approval =================

def cutoff_today():
    now = dt.datetime.now()
    return now.replace(hour=CUTOFF_HOUR, minute=CUTOFF_MINUTE, second=0, microsecond=0)


def before_cutoff():
    return dt.datetime.now() < cutoff_today()


def clock_text(t):
    return t.strftime("%I:%M %p").lstrip("0")


def log(decision):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([dt.datetime.now().isoformat(timespec="seconds"), decision])


def approve():
    os.makedirs(APP_DIR, exist_ok=True)
    with open(APPROVAL_FILE, "w") as f:
        f.write(str(time.time()))


def consume_approval():
    """True if Continue was pressed in the last APPROVAL_WINDOW seconds.
    One use only: the approval is deleted so it can't cover a later open."""
    try:
        with open(APPROVAL_FILE) as f:
            fresh = time.time() - float(f.read().strip()) < APPROVAL_WINDOW
    except Exception:
        return False
    try:
        os.remove(APPROVAL_FILE)
    except OSError:
        pass
    return fresh


def week_history():
    """Oldest to today: (day letter, 'skipped' | 'held' | None, is_today)."""
    today = dt.date.today()
    days = [today - dt.timedelta(days=i) for i in range(6, -1, -1)]
    status = {d: None for d in days}
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, newline="") as f:
            for row in csv.reader(f):
                try:
                    d = dt.datetime.fromisoformat(row[0]).date()
                except (IndexError, ValueError):
                    continue
                if d not in status:
                    continue
                if row[1] == "continued":
                    status[d] = "skipped"
                elif row[1] == "quit" and status[d] != "skipped":
                    status[d] = "held"
    return [(d.strftime("%a")[0], status[d], d == today) for d in days]


# ================= Install =================

PS_INSTALL = r"""
$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject WScript.Shell
function New-Link([string]$Path, [string]$Arguments) {
    $s = $ws.CreateShortcut($Path)
    $s.TargetPath = $env:VG_EXE
    $s.Arguments = $Arguments
    $s.WorkingDirectory = Split-Path $env:VG_EXE
    $s.IconLocation = $env:VG_ICON
    $s.Save()
}
New-Link (Join-Path ([Environment]::GetFolderPath('Startup')) 'Valorant Guard.lnk') ''
New-Link (Join-Path ([Environment]::GetFolderPath('Desktop')) 'VALORANT.lnk') '--launch'
New-Link (Join-Path ([Environment]::GetFolderPath('Programs')) 'VALORANT.lnk') '--launch'
"""


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TASK_NAME = "Valorant Guard Keepalive"


def install():
    """Three independent ways to keep the guard running, none need admin:
    a Startup shortcut, a Run registry entry, and a scheduled task that
    checks every 5 minutes and restarts it if it was closed or crashed."""
    if not getattr(sys, "frozen", False):
        print("Run install.bat to build and install.")
        return 1
    import winreg
    exe = sys.executable
    env = dict(os.environ, VG_EXE=exe, VG_ICON=valorant_icon() or exe)
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-Command", PS_INSTALL],
                       env=env, capture_output=True, creationflags=CREATE_NO_WINDOW)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ValorantGuard", 0, winreg.REG_SZ, f'"{exe}"')
    subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "MINUTE",
                    "/MO", "5", "/TR", f'"{exe}" --keepalive'],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)
    return r.returncode


def uninstall():
    import winreg
    kill_self = ["taskkill", "/F", "/IM", "ValorantGuard.exe", "/FI", f"PID ne {os.getpid()}"]
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, "ValorantGuard")
    except OSError:
        pass
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Remove-Item -ErrorAction SilentlyContinue "
                    "(Join-Path ([Environment]::GetFolderPath('Startup')) 'Valorant Guard.lnk'),"
                    "(Join-Path ([Environment]::GetFolderPath('Desktop')) 'VALORANT.lnk'),"
                    "(Join-Path ([Environment]::GetFolderPath('Programs')) 'VALORANT.lnk')"],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)
    subprocess.run(kill_self, capture_output=True, creationflags=CREATE_NO_WINDOW)
    return 0


# ================= UI =================

def rrect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def spaced(s):
    return " ".join(s)


class Button:
    def __init__(self, c, box, text, kind, command, font, radius):
        self.c, self.kind, self.command = c, kind, command
        self.enabled = True
        self.hover = False
        self.tag = f"btn{id(self)}"
        x1, y1, x2, y2 = box
        self.shape = rrect(c, x1, y1, x2, y2, radius, tags=self.tag, width=1.5)
        self.label = c.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=text,
                                   font=font, tags=self.tag)
        c.tag_bind(self.tag, "<Enter>", self._enter)
        c.tag_bind(self.tag, "<Leave>", self._leave)
        c.tag_bind(self.tag, "<Button-1>", self._click)
        self.paint()

    def paint(self):
        c = self.c
        if self.kind == "primary":
            c.itemconfig(self.shape, fill=ACCENT_HOVER if self.hover else ACCENT, outline="")
            c.itemconfig(self.label, fill="#FFFFFF")
        elif not self.enabled:
            c.itemconfig(self.shape, fill=BG, outline=BORDER)
            c.itemconfig(self.label, fill=DIM)
        else:
            c.itemconfig(self.shape, fill=SURFACE if self.hover else BG,
                         outline=MUTED if self.hover else BORDER)
            c.itemconfig(self.label, fill=TEXT)

    def set(self, text=None, enabled=None):
        if text is not None:
            self.c.itemconfig(self.label, text=text)
        if enabled is not None:
            self.enabled = enabled
        self.paint()

    def _enter(self, _):
        self.hover = True
        if self.enabled:
            self.c.config(cursor="hand2")
        self.paint()

    def _leave(self, _):
        self.hover = False
        self.c.config(cursor="")
        self.paint()

    def _click(self, _):
        if self.enabled:
            self.command()


class Popup:
    W, H = 560, 388

    def __init__(self, app, mode, on_choice):
        self.app, self.mode, self.on_choice = app, mode, on_choice
        self.closed = False
        self.lock = CONTINUE_DELAY
        root = app.root
        p = self.p
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()

        # Dimmed backdrop over the whole screen
        self.back = tk.Toplevel(root)
        self.back.overrideredirect(True)
        self.back.configure(bg="#000000")
        self.back.geometry(f"{sw}x{sh}+0+0")
        self.back.attributes("-alpha", 0.0)
        self.back.attributes("-topmost", True)

        # Rounded card
        w, h = p(self.W), p(self.H)
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.configure(bg=KEY)
        self.win.attributes("-transparentcolor", KEY)
        self.win.attributes("-alpha", 0.0)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        self.c = tk.Canvas(self.win, width=w, height=h, bg=KEY, highlightthickness=0, bd=0)
        self.c.pack()

        self.draw()
        self.win.bind("<Escape>", lambda e: self.choose("quit"))
        self.win.bind("<Return>", lambda e: self.choose("quit"))
        self.win.after(60, self.win.focus_force)
        self.fade_in()
        self.tick()
        self.keep_on_top()

    def p(self, v):
        return int(round(v * self.app.S))

    def draw(self):
        c, p, W, H, f = self.c, self.p, self.W, self.H, self.app.fonts
        rrect(c, 0, 0, p(W) - 1, p(H) - 1, p(22), fill=BORDER, outline="")
        rrect(c, 1, 1, p(W) - 2, p(H) - 2, p(21), fill=BG, outline="")

        # Brand row
        c.create_rectangle(p(36), p(34), p(39), p(48), fill=ACCENT, outline="")
        c.create_text(p(48), p(41), text=spaced("VALORANT GUARD"), fill=TEXT,
                      font=(f["text"], 8, "bold"), anchor="w")
        self.clock = c.create_text(p(W - 36), p(41), fill=MUTED,
                                   font=(f["text"], 9), anchor="e")

        # Headline
        c.create_text(p(36), p(72), text="Not yet.", fill=TEXT,
                      font=(f["display"], 30, "bold"), anchor="nw")
        cutoff = clock_text(cutoff_today())
        if self.mode == "blocked":
            sub = f"Riot Client was stopped. Game time starts at {cutoff}."
        else:
            sub = f"Game time starts at {cutoff}. Riot Client stays closed for now."
        c.create_text(p(36), p(124), text=sub, fill=MUTED, font=(f["text"], 11),
                      anchor="nw", width=p(W - 72))

        c.create_line(p(36), p(172), p(W - 36), p(172), fill=BORDER)

        # Countdown
        c.create_text(p(36), p(196), text=spaced("GAME TIME IN"), fill=MUTED,
                      font=(f["text"], 7, "bold"), anchor="w")
        self.count = c.create_text(p(34), p(210), fill=TEXT,
                                   font=(f["display"], 32, "bold"), anchor="nw")

        # Last 7 days
        c.create_text(p(W - 36), p(196), text=spaced("LAST 7 DAYS"), fill=MUTED,
                      font=(f["text"], 7, "bold"), anchor="e")
        r, gap, cy = 7, 26, 228
        right = W - 36 - r
        for i, (letter, status, today) in enumerate(week_history()):
            cx = right - (6 - i) * gap
            if status == "skipped":
                c.create_oval(p(cx - r), p(cy - r), p(cx + r), p(cy + r), fill=ACCENT, outline="")
            elif status == "held":
                c.create_oval(p(cx - r), p(cy - r), p(cx + r), p(cy + r), fill=TEXT, outline="")
            else:
                c.create_oval(p(cx - r), p(cy - r), p(cx + r), p(cy + r),
                              fill="", outline=DIM, width=1.5)
            c.create_text(p(cx), p(cy + 20), text=letter, fill=TEXT if today else MUTED,
                          font=(f["text"], 7, "bold" if today else "normal"))

        # Legend
        small = tkfont.Font(family=f["text"], size=8)
        x = p(W - 36)
        for label, colour in (("skipped", ACCENT), ("held", TEXT)):
            c.create_text(x, p(270), text=label, fill=MUTED, font=small, anchor="e")
            x -= small.measure(label) + p(6)
            c.create_oval(x - p(8), p(270 - 4), x, p(270 + 4), fill=colour, outline="")
            x -= p(16)

        # Buttons
        self.primary = Button(c, (p(36), p(294), p(216), p(344)), "Not now", "primary",
                              lambda: self.choose("quit"), (f["text"], 11, "bold"), p(12))
        self.secondary = Button(c, (p(232), p(294), p(W - 36), p(344)), "", "secondary",
                                lambda: self.choose("continue"), (f["text"], 10), p(12))

    def tick(self):
        if self.closed:
            return
        now = dt.datetime.now()
        self.c.itemconfig(self.clock, text=clock_text(now))
        secs = int((cutoff_today() - now).total_seconds())
        if secs <= 0:
            self.c.itemconfig(self.count, text="00:00:00")
            self.lock = 0
        else:
            h, rem = divmod(secs, 3600)
            m, s = divmod(rem, 60)
            self.c.itemconfig(self.count, text=f"{h:02d}:{m:02d}:{s:02d}")
        if self.lock > 0:
            self.secondary.set(text=f"Continue in {self.lock}", enabled=False)
            self.lock -= 1
        else:
            self.secondary.set(text="Continue anyway", enabled=True)
        self.win.after(1000, self.tick)

    def fade_in(self, step=0):
        if self.closed:
            return
        a = min(1.0, step / 10)
        eased = 1 - (1 - a) ** 3
        self.back.attributes("-alpha", 0.72 * eased)
        self.win.attributes("-alpha", eased)
        if a < 1:
            self.win.after(15, self.fade_in, step + 1)

    def keep_on_top(self):
        if self.closed:
            return
        self.back.attributes("-topmost", True)
        self.win.attributes("-topmost", True)
        self.back.lift()
        self.win.lift()
        self.win.after(1500, self.keep_on_top)

    def choose(self, choice):
        if self.closed:
            return
        if choice == "continue" and not self.secondary.enabled:
            return
        self.closed = True
        self.fade_out(10, choice)

    def fade_out(self, step, choice):
        a = step / 10
        self.back.attributes("-alpha", 0.72 * a)
        self.win.attributes("-alpha", a)
        if step > 0:
            self.win.after(12, self.fade_out, step - 1, choice)
        else:
            self.win.destroy()
            self.back.destroy()
            self.on_choice(choice)


class App:
    def __init__(self, mode, show_toast=True):
        self.mode = mode
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = lambda *e: log_error(repr(e[1]))
        self.S = self.root.winfo_fpixels("1i") / 96
        fams = set(tkfont.families(self.root))
        self.fonts = {
            "display": "Segoe UI Variable Display" if "Segoe UI Variable Display" in fams else "Segoe UI",
            "text": "Segoe UI Variable Text" if "Segoe UI Variable Text" in fams else "Segoe UI",
        }
        self.popup = None
        self.popup_kind = None
        self.session_ok = False     # True while an approved sitting is going on
        self.session_start = 0.0
        self.seen_active = False    # sitting has shown a Riot window or the game
        self.game_was_running = False
        self.idle_since = None
        if mode == "watch":
            self.root.after(200, self.poll)
            self.popup_kind = None
            self.tray_q = queue.Queue()
            self.tray = None
            self.start_tray()
            if show_toast:
                self.root.after(1500, self.show_toast)
        else:
            self.root.after(100, lambda: self.open_popup("launch"))

    def show_toast(self):
        """Small 'guard is on' card, bottom right, so you know it started."""
        try:
            S = self.S
            p = lambda v: int(round(v * S))
            w, h = p(300), p(64)
            rect = wt.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # work area
            t = tk.Toplevel(self.root)
            t.overrideredirect(True)
            t.configure(bg=KEY)
            t.attributes("-transparentcolor", KEY)
            t.attributes("-topmost", True)
            t.attributes("-alpha", 0.0)
            t.geometry(f"{w}x{h}+{rect.right - w - p(16)}+{rect.bottom - h - p(16)}")
            c = tk.Canvas(t, width=w, height=h, bg=KEY, highlightthickness=0, bd=0)
            c.pack()
            rrect(c, 0, 0, w - 1, h - 1, p(14), fill=BORDER, outline="")
            rrect(c, 1, 1, w - 2, h - 2, p(13), fill=BG, outline="")
            c.create_rectangle(p(18), p(20), p(21), p(44), fill=ACCENT, outline="")
            c.create_text(p(32), p(24), text="Valorant Guard is on", fill=TEXT,
                          font=(self.fonts["text"], 10, "bold"), anchor="w")
            c.create_text(p(32), p(42), fill=MUTED, font=(self.fonts["text"], 9), anchor="w",
                          text=f"Game time starts at {clock_text(cutoff_today())}")

            def fade(a, step, then=None):
                t.attributes("-alpha", a)
                nxt = a + step
                if 0 <= nxt <= 1:
                    t.after(20, fade, nxt, step, then)
                elif then:
                    then()

            fade(0.0, 0.1, lambda: t.after(3500, fade, 1.0, -0.1, t.destroy))
        except Exception as e:
            log_error(f"toast: {e!r}")

    # ---------- system tray ----------
    def start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            log_error("pystray/Pillow missing, running without tray icon")
            return
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 2, size - 3, size - 3), radius=16, fill=(255, 70, 85, 255))
        # white V mark
        d.polygon([(14, 16), (24, 16), (32, 38), (40, 16), (50, 16), (36, 50), (28, 50)],
                  fill=(255, 255, 255, 255))
        cutoff = clock_text(cutoff_today())
        post = lambda cmd: (lambda icon, item: self.tray_q.put(cmd))
        menu = pystray.Menu(
            pystray.MenuItem("Valorant Guard is on", None, enabled=False),
            pystray.MenuItem(f"Game time starts at {cutoff}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Preview warning", post("preview")),
            pystray.MenuItem("Quit", post("quit")),
        )
        self.tray = pystray.Icon("ValorantGuard", img, f"Valorant Guard: on until {cutoff}", menu)
        self.tray.run_detached()

    def handle_tray(self):
        while True:
            try:
                cmd = self.tray_q.get_nowait()
            except queue.Empty:
                return
            if cmd == "preview" and not self.popup:
                self.open_popup("preview")
            elif cmd == "quit":
                set_paused(True)
                if self.tray:
                    self.tray.stop()
                self.root.destroy()
                return

    def open_popup(self, kind):
        self.popup_kind = kind
        self.popup = Popup(self, kind, lambda choice: self.on_choice(kind, choice))

    def start_session(self):
        self.session_ok = True
        self.session_start = time.time()
        self.seen_active = False
        self.game_was_running = False
        self.idle_since = None

    def end_session(self):
        self.session_ok = False
        if before_cutoff():
            kill_all()      # close leftovers (tray, services) so reopening starts fresh

    def poll(self):
        try:
            self.handle_tray()
            procs = watched_procs()
            now = time.time()

            if self.popup:
                if procs and self.popup_kind != "preview":
                    kill_all()          # tried to open again while the warning is up
                return

            if not self.session_ok:
                if not procs:
                    return
                if not before_cutoff() or consume_approval():
                    self.start_session()
                else:
                    kill_all()
                    self.open_popup("blocked")
                return

            # An approved sitting is in progress
            if not procs:
                self.session_ok = False
                return

            names = set(procs.values())
            game = bool(names & _GAME)
            ui_pids = {pid for pid, n in procs.items() if n in _UI}
            window = bool(ui_pids & visible_window_pids())

            if game or window:
                self.seen_active = True
                self.idle_since = None
            if self.game_was_running and not game and before_cutoff():
                self.end_session()      # closed VALORANT before game time
                return
            self.game_was_running = game

            if self.seen_active and not game and not window:
                self.idle_since = self.idle_since or now
                if now - self.idle_since >= IDLE_END_SECONDS:
                    self.end_session()  # Riot window closed or sent to tray
            elif not self.seen_active and now - self.session_start > STARTUP_GRACE:
                self.end_session()      # never actually opened
        except Exception:
            pass
        finally:
            self.root.after(POLL_MS, self.poll)

    def on_choice(self, kind, choice):
        self.popup = None
        self.popup_kind = None
        if kind == "preview":
            return
        if self.mode != "test":
            log("continued" if choice == "continue" else "quit")
            if choice == "continue":
                approve()
                if not launch_riot():
                    messagebox.showerror("Valorant Guard",
                                         "Couldn't find the Riot Client. Open it manually this time.")
            elif kind == "blocked":
                kill_all()
        if self.mode != "watch":
            self.root.destroy()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            if getattr(self, "tray", None):
                try:
                    self.tray.stop()
                except Exception:
                    pass


def main():
    if sys.platform != "win32":
        print("Valorant Guard runs on Windows only.")
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    args = sys.argv[1:]
    if "--install" in args:
        sys.exit(install())
    if "--uninstall" in args:
        sys.exit(uninstall())
    mode = "test" if "--test" in args else "launch" if "--launch" in args else "watch"
    keepalive = "--keepalive" in args
    if mode == "watch":
        if keepalive and is_paused():
            return          # you quit it from the tray; stay off until next login
        if not keepalive:
            set_paused(False)   # normal start (login) turns it back on
        single_instance()
    if mode == "launch":
        start_guard()   # bring the background guard back if it isn't running
        if not before_cutoff():
            if not launch_riot():
                messagebox.showerror("Valorant Guard", "Couldn't find the Riot Client.")
            return
    if mode != "watch":
        App(mode).run()
        return
    # Background guard: if anything ever crashes it, start it again.
    first = True
    while True:
        try:
            App(mode, show_toast=first and not keepalive).run()
            return
        except Exception as e:
            log_error(f"guard crashed: {e!r}")
            first = False
            time.sleep(3)


if __name__ == "__main__":
    main()
