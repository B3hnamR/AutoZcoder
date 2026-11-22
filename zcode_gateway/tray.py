"""Notification-area icon for the desktop shell (Windows).

Pure ctypes: pystray + Pillow would add ~15 MB to the onefile exe for one
16x16 icon, and the app already ships assets/zcode.ico. Shell_NotifyIcon on a
hidden top-level window is the same API Explorer itself drives.

The tray is optional infrastructure. start() returns False when the shell is
unavailable (a service session, a stripped Windows install) and the caller
keeps its previous behaviour; nothing here may raise into the gateway.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

SUPPORTED = sys.platform == "win32"

# Window class is fixed, not per-PID: the single-instance mutex already
# guarantees one tray per session, and a stable name makes the icon's window
# findable from outside (FindWindowW) when diagnosing a missing icon.
TRAY_WINDOW_CLASS = "ZcodeGatewayTrayWindow"

WM_APP = 0x8000
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_TRAY = WM_APP + 20

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_WARNING = 0x1, 0x2
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x10, 0x40
SM_CXSMICON = 49
IDI_APPLICATION = 32512
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
# WINFUNCTYPE is Windows-only; the struct is never used elsewhere, but the
# module still has to import on the Linux target (CI runs the suite there).
_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
WNDPROC = _FUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hWnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


if SUPPORTED:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _user32.RegisterClassW.restype = ctypes.c_ushort
    _user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
    _user32.DefWindowProcW.restype = LRESULT
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                    wintypes.UINT, wintypes.UINT]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    _user32.DispatchMessageW.restype = LRESULT
    _user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterWindowMessageW.restype = wintypes.UINT
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, WPARAM,
                                    wintypes.LPCWSTR]
    _user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.LPVOID]
    _user32.TrackPopupMenu.restype = ctypes.c_int
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    # resource ids arrive as MAKEINTRESOURCE pointers, not strings
    _user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                   wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                   wintypes.UINT]
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.DestroyIcon.argtypes = [wintypes.HICON]
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int
    _user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.OpenClipboard.restype = wintypes.BOOL
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.CloseClipboard.restype = wintypes.BOOL
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.SetClipboardData.restype = wintypes.HANDLE

    _shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                           ctypes.POINTER(NOTIFYICONDATAW)]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
                                        ctypes.POINTER(wintypes.HICON),
                                        ctypes.POINTER(wintypes.HICON),
                                        wintypes.UINT]
    _shell32.ExtractIconExW.restype = wintypes.UINT

    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.restype = wintypes.HGLOBAL


def asset_path(*parts: str) -> Path:
    """Bundle-relative asset (PyInstaller unpacks --add-data under _MEIPASS)."""
    root = getattr(sys, "_MEIPASS", None)
    base = Path(root) if root else Path(__file__).resolve().parent.parent
    return base.joinpath(*parts)


def copy_text(text: str) -> bool:
    """Put text on the clipboard (CF_UNICODETEXT). False when it cannot."""
    if not SUPPORTED or not text:
        return False
    if not _user32.OpenClipboard(None):
        return False
    handle = None
    try:
        _user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return False
        ctypes.memmove(pointer, buf, size)
        _kernel32.GlobalUnlock(handle)
        if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False
        handle = None          # the clipboard owns the block now
        return True
    finally:
        if handle:
            _kernel32.GlobalFree(handle)
        _user32.CloseClipboard()


class TrayIcon:
    """One notification-area icon with a right-click menu.

    menu: ordered (command_id, label) pairs; command_id None is a separator.
    on_activate runs on a left click, on_command(command_id) on a menu pick.
    Both run on the tray thread and must return quickly.
    """

    def __init__(self, tooltip: str, menu, on_activate=None, on_command=None,
                 icon_path=None, class_name: str = TRAY_WINDOW_CLASS) -> None:
        self.tooltip = (tooltip or "Zcode Gateway")[:127]
        self.menu = list(menu)
        self.on_activate = on_activate
        self.on_command = on_command
        self.icon_path = str(icon_path) if icon_path else ""
        self.class_name = class_name
        self._hwnd = None
        self._hicon = None
        self._thread = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._wndproc = None
        self._taskbar_created = 0
        self._last_activate = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeout: float = 5.0) -> bool:
        if not SUPPORTED:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._started
            self._ready.clear()
            self._started = False
            self._thread = threading.Thread(target=self._run, name="tray-icon",
                                            daemon=True)
            self._thread.start()
        self._ready.wait(timeout)
        return self._started

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            hwnd, thread = self._hwnd, self._thread
        if not hwnd or thread is None:
            return
        if thread is threading.current_thread():
            return
        _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        thread.join(timeout)

    @property
    def alive(self) -> bool:
        thread = self._thread
        return bool(self._started and thread is not None and thread.is_alive())

    # -- shell updates (any thread) ---------------------------------------
    def set_tooltip(self, text: str) -> None:
        self.tooltip = (text or "")[:127]
        self._modify(NIF_TIP)

    def notify(self, title: str, message: str, warning: bool = False) -> None:
        """Balloon/toast in the corner; silently ignored when unsupported."""
        if not SUPPORTED or not self._hwnd:
            return
        data = self._nid()
        data.uFlags = NIF_INFO
        data.szInfoTitle = (title or "")[:63]
        data.szInfo = (message or "")[:255]
        data.dwInfoFlags = NIIF_WARNING if warning else NIIF_INFO
        try:
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        except OSError:
            pass

    def _modify(self, flags: int) -> None:
        if not SUPPORTED or not self._hwnd:
            return
        data = self._nid()
        data.uFlags = flags
        try:
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        except OSError:
            pass

    # -- internals ---------------------------------------------------------
    def _nid(self) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        # without these the shell registers a nameless slot: NIM_ADD still
        # returns TRUE but draws no icon and sends no click messages
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._hicon
        data.szTip = self.tooltip
        return data

    def _load_icon(self):
        if self.icon_path and Path(self.icon_path).is_file():
            size = _user32.GetSystemMetrics(SM_CXSMICON) or 16
            handle = _user32.LoadImageW(None, self.icon_path, IMAGE_ICON,
                                        size, size, LR_LOADFROMFILE)
            if handle:
                return handle
        if getattr(sys, "frozen", False):
            # the build embeds assets/zcode.ico as the exe icon
            large, small = wintypes.HICON(), wintypes.HICON()
            if _shell32.ExtractIconExW(sys.executable, 0, ctypes.byref(large),
                                       ctypes.byref(small), 1) and small.value:
                return small.value
        return _user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _window_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
                elif event == WM_LBUTTONUP:
                    self._activate()
                return 0
            if msg == self._taskbar_created:
                # Explorer restarted and dropped every tray icon with it
                self._add_icon()
                return 0
            if msg == WM_CLOSE:
                self._remove_icon()
                _user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _activate(self) -> None:
        if self.on_activate is None:
            return
        now = time.monotonic()
        if now - self._last_activate < 0.4:
            # a double click also delivers two LBUTTONUPs
            return
        self._last_activate = now
        try:
            self.on_activate()
        except Exception:
            pass

    def _show_menu(self) -> None:
        if not self._hwnd:
            return
        menu = _user32.CreatePopupMenu()
        if not menu:
            return
        try:
            for command_id, label in self.menu:
                if command_id is None:
                    _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                else:
                    _user32.AppendMenuW(menu, MF_STRING, command_id, label)
            point = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(point))
            # the foreground + WM_NULL pair is required or the menu stays up
            # after the user clicks elsewhere
            _user32.SetForegroundWindow(self._hwnd)
            chosen = _user32.TrackPopupMenu(
                menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                point.x, point.y, 0, self._hwnd, None)
            _user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            _user32.DestroyMenu(menu)
        if chosen and self.on_command is not None:
            try:
                self.on_command(chosen)
            except Exception:
                pass

    def _add_icon(self) -> bool:
        if not self._hwnd:
            return False
        return bool(_shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid())))

    def _remove_icon(self) -> None:
        if not self._hwnd:
            return
        try:
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
        except OSError:
            pass

    def _create_window(self):
        module = _kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._window_proc)
        wndclass = WNDCLASSW()
        wndclass.lpfnWndProc = self._wndproc
        wndclass.hInstance = module
        wndclass.lpszClassName = self.class_name
        # a class left behind by an earlier crashed run still points at a dead
        # Python callback: re-registering is the only safe recovery
        _user32.UnregisterClassW(self.class_name, module)
        _user32.RegisterClassW(ctypes.byref(wndclass))
        # WS_OVERLAPPED and never ShowWindow: top-level (so the shell's
        # TaskbarCreated broadcast reaches it) but invisible
        return _user32.CreateWindowExW(
            0, self.class_name, "Zcode Gateway", 0, 0, 0, 0, 0,
            None, None, module, None)

    def _run(self) -> None:
        try:
            self._taskbar_created = _user32.RegisterWindowMessageW("TaskbarCreated")
            self._hicon = self._load_icon()
            self._hwnd = self._create_window()
            if not self._hwnd:
                return
            self._started = self._add_icon()
            if not self._started:
                return
            self._ready.set()
            message = MSG()
            while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(message))
                _user32.DispatchMessageW(ctypes.byref(message))
        except Exception:
            self._started = False
        finally:
            self._started = False
            self._ready.set()
            hwnd = self._hwnd
            if hwnd:
                self._remove_icon()
                try:
                    _user32.DestroyWindow(hwnd)
                except OSError:
                    pass
            self._hwnd = None
            if self._hicon:
                try:
                    _user32.DestroyIcon(self._hicon)
                except OSError:
                    pass
                self._hicon = None
            try:
                _user32.UnregisterClassW(self.class_name,
                                         _kernel32.GetModuleHandleW(None))
            except OSError:
                pass


def find_window(class_name: str = TRAY_WINDOW_CLASS):
    """HWND of a running tray window, or None. Diagnostics/tests only."""
    if not SUPPORTED:
        return None
    return _user32.FindWindowW(class_name, None)


def wait_until_ready(tray: TrayIcon, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tray.alive:
            return True
        time.sleep(0.05)
    return False
