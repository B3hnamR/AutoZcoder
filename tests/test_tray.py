import ctypes
import os
import sys
from ctypes import wintypes

import pytest

from zcode_gateway import tray


def _read_clipboard_text():
    user32 = ctypes.WinDLL("user32")
    kernel32 = ctypes.WinDLL("kernel32")
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(tray.CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def test_tooltip_is_truncated_to_the_shell_limit():
    assert len(tray.TrayIcon(tooltip="x" * 300, menu=[]).tooltip) == 127


def test_notify_and_tooltip_before_start_do_not_raise():
    icon = tray.TrayIcon(tooltip="Zcode Gateway", menu=[])
    icon.set_tooltip("still not started")
    icon.notify("title", "message")
    assert icon.alive is False


def test_unsupported_platform_is_a_noop(monkeypatch):
    monkeypatch.setattr(tray, "SUPPORTED", False)
    icon = tray.TrayIcon(tooltip="Zcode Gateway", menu=[])
    assert icon.start() is False
    assert icon.alive is False
    icon.stop()
    icon.notify("title", "message")
    assert tray.copy_text("hello") is False
    assert tray.find_window() is None


def test_asset_path_follows_the_pyinstaller_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert tray.asset_path("assets", "zcode.ico") == tmp_path / "assets" / "zcode.ico"


@pytest.mark.skipif(sys.platform != "win32", reason="Shell_NotifyIcon is Windows-only")
def test_icon_starts_stops_and_restarts():
    # unique class: a real gateway may be running on this machine and its tray
    # window would be the one find_window() returns
    class_name = f"ZcodeGatewayTrayTest{os.getpid()}"
    icon = tray.TrayIcon(tooltip="Zcode Gateway test",
                         menu=[(1, "Open dashboard"), (None, ""), (9, "Quit")],
                         class_name=class_name)
    if not icon.start():
        pytest.skip("no interactive shell (no notification area)")
    try:
        assert icon.alive is True
        assert tray.find_window(class_name) == icon._hwnd
        assert tray.wait_until_ready(icon, timeout=2) is True
    finally:
        icon.stop()
    assert icon.alive is False
    assert tray.find_window(class_name) is None
    if not icon.start():
        pytest.skip("shell refused a second icon")
    icon.stop()
    assert icon.alive is False


@pytest.mark.skipif(sys.platform != "win32", reason="clipboard APIs are Windows-only")
def test_copy_text_puts_text_on_the_clipboard():
    previous = _read_clipboard_text()
    try:
        if not tray.copy_text("zcode-tray-test"):
            pytest.skip("no clipboard in this session")
        assert _read_clipboard_text() == "zcode-tray-test"
    finally:
        if previous is not None:
            tray.copy_text(previous)


def test_registration_flags_the_icon_message_and_tooltip():
    icon = tray.TrayIcon(tooltip="Zcode Gateway", menu=[])
    icon._hwnd = 1234
    data = icon._nid()
    assert data.uFlags == tray.NIF_MESSAGE | tray.NIF_ICON | tray.NIF_TIP
    assert data.uCallbackMessage == tray.WM_TRAY
    assert data.hWnd == 1234


def test_desktop_menu_opens_the_dashboard_and_can_quit():
    import gui_main
    commands = [command for command, _ in gui_main.TRAY_MENU if command is not None]
    assert commands[0] == gui_main.CMD_OPEN
    assert commands[-1] == gui_main.CMD_QUIT
    assert len(commands) == len(set(commands))
    assert all(label for command, label in gui_main.TRAY_MENU if command is not None)
