"""What the window needs from Windows: start at login, a file picker, Edge, Npcap, the tray icon,
bringing the window to the front.

Everything here does nothing (or says "not available") on other systems, where the window
is only used for development.
"""

import os
import subprocess
import sys
import threading
from pathlib import Path

WINDOWS = os.name == "nt"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "率土战局"


def autostart_command():
    """What Windows runs at login: this exe in the background (only a packaged exe can)."""
    if not getattr(sys, "frozen", False):
        return None
    return f'"{sys.executable}" --background'


def autostart():
    """Whether this exe starts at login."""
    if not WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0] == autostart_command()
    except OSError:
        return False


def set_autostart(enabled):
    import winreg
    command = autostart_command()
    if command is None:
        raise RuntimeError("只有打包好的 exe 才能设置开机启动")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def npcap_installed():
    """True or False on Windows (the Npcap driver's service is registered), None elsewhere."""
    if not WINDOWS:
        return None
    import winreg
    try:
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\npcap").Close()
        return True
    except OSError:
        return False


def edge():
    """msedge.exe, or None."""
    if not WINDOWS:
        return None
    import winreg
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                path = winreg.QueryValueEx(key, "")[0].strip('"')
            if os.path.isfile(path):
                return path
        except OSError:
            pass
    for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        path = os.path.join(os.environ.get(name, ""), "Microsoft", "Edge", "Application", "msedge.exe")
        if os.environ.get(name) and os.path.isfile(path):
            return path
    return None


def open_window(url, profile):
    """Show `url` as an app window (Edge without its address bar), else in the default browser.

    `profile`: Edge's data folder for this app, apart from the player's own browsing.
    """
    path = edge()
    if path:
        subprocess.Popen([path, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                          "--no-default-browser-check", "--window-size=1200,820", "--disable-features=Translate"])
        return
    import webbrowser
    webbrowser.open(url)


def sharp():
    """Draw this process's own windows (tray icon, its menu, the file picker) at the screen's
    real scale; unaware, Windows stretches them, blurred, on a screen above 100%."""
    if not WINDOWS:
        return
    import ctypes
    user32 = ctypes.WinDLL("user32")
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-2)):  # system aware
            return
    except AttributeError:  # before Windows 10 1703
        pass
    user32.SetProcessDPIAware()


def focus_window(title):
    """Bring the open window titled `title` (Edge's app window) to the front: True, or False if none."""
    if not WINDOWS:
        return False
    import ctypes
    from ctypes import wintypes
    api = _api()
    user32, found = api.user32, []

    def visit(hwnd, _):
        name, kind = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(hwnd, name, len(name))
        user32.GetClassNameW(hwnd, kind, len(kind))
        if name.value == title and kind.value.startswith("Chrome_WidgetWin") and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(api.WNDENUMPROC(visit), 0)
    if not found:
        return False
    hwnd = found[0]
    user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)  # SW_RESTORE, SW_SHOW
    user32.SetForegroundWindow(hwnd)
    return True


def let_others_focus():
    """Let the program already running bring its window to the front (only the program the
    player just started may)."""
    if WINDOWS:
        import ctypes
        ctypes.WinDLL("user32").AllowSetForegroundWindow(-1)  # ASFW_ANY


def open_folder(path):
    if WINDOWS:
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", str(path)])


def pick_file(title, pattern):
    """A path chosen in Windows' open dialog, or None (cancelled, or not Windows).

    `pattern`: (description, "dumpcap.exe") of the files shown.
    """
    if not WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes

    class OpenFileName(ctypes.Structure):
        _fields_ = [("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
                    ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", wintypes.LPCWSTR),
                    ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
                    ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
                    ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
                    ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
                    ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
                    ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
                    ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
                    ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR),
                    ("pvReserved", ctypes.c_void_p), ("dwReserved", wintypes.DWORD),
                    ("FlagsEx", wintypes.DWORD)]

    buffer = ctypes.create_unicode_buffer(1024)
    description, files = pattern
    start = os.environ.get("ProgramFiles", "C:\\")
    dialog = OpenFileName(lStructSize=ctypes.sizeof(OpenFileName),
                          hwndOwner=ctypes.windll.user32.GetForegroundWindow(),
                          lpstrFilter=f"{description}\0{files}\0所有文件\0*.*\0\0", lpstrFile=buffer,
                          nMaxFile=len(buffer), lpstrInitialDir=start, lpstrTitle=title,
                          Flags=0x00001000 | 0x00000800 | 0x00080000 | 0x00000008)  # file/path must exist, explorer, no chdir
    comdlg32 = ctypes.WinDLL("comdlg32")
    comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(OpenFileName)]
    comdlg32.GetOpenFileNameW.restype = wintypes.BOOL
    if not comdlg32.GetOpenFileNameW(ctypes.byref(dialog)):
        return None
    return buffer.value or None


def system():
    """A short description of the system, for the diagnostics."""
    import platform
    return f"{platform.system()} {platform.release()} ({platform.version()}, {platform.machine()})"


def frozen_folder():
    """The folder of the exe (packaged) or of the package (source)."""
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


class Tray:
    """The icon in the notification area while the program runs: a click opens the window,
    the right button offers 打开窗口 and 退出. Both callbacks run on the icon's own thread."""

    OPEN, QUIT = 1, 2

    def __init__(self, tip, on_open, on_quit, icon=None):
        """`icon`: an .ico file with the sizes the notification area may want (16 to 32 pixels)."""
        self.tip, self.on_open, self.on_quit, self.icon = tip, on_open, on_quit, icon
        self.hwnd, self.thread = None, None
        self.ready = threading.Event()

    def start(self):
        if WINDOWS:
            self.thread = threading.Thread(target=self._run, name="tray", daemon=True)
            self.thread.start()
            self.ready.wait(5)

    def close(self):
        if self.hwnd:
            _api().user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE

    def _run(self):
        try:
            self._loop()
        except Exception as exc:  # no icon is better than no program
            print(f"tray icon: {exc}", file=sys.stderr)
        finally:
            self.ready.set()

    def _loop(self):
        import ctypes
        from ctypes import wintypes
        api = _api()
        user32, shell32 = api.user32, api.shell32
        WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_NULL = 0x0002, 0x0010, 0x0111, 0x0000
        WM_LBUTTONUP, WM_RBUTTONUP, CALLBACK = 0x0202, 0x0205, 0x8000 + 1  # WM_APP + 1
        NIM_ADD, NIM_DELETE, NIF_MESSAGE, NIF_ICON, NIF_TIP = 0, 2, 1, 2, 4
        taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")  # Explorer restarted

        hinstance = api.kernel32.GetModuleHandleW(None)
        # The size the notification area shows at this screen's scale (16 at 100%, 24 at 150%),
        # picked from the file rather than a 16-pixel icon stretched.
        side = user32.GetSystemMetrics(49)  # SM_CXSMICON
        icon = wintypes.HICON()
        if self.icon:
            icon = wintypes.HICON(user32.LoadImageW(None, str(self.icon), 1, side, side, 0x0010))  # IMAGE_ICON, LR_LOADFROMFILE
        if not icon.value:
            icon = wintypes.HICON(user32.LoadIconW(None, ctypes.c_void_p(32512)))  # IDI_APPLICATION
        data = api.NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(data)
        data.uID, data.uFlags, data.uCallbackMessage = 1, NIF_MESSAGE | NIF_ICON | NIF_TIP, CALLBACK
        data.hIcon, data.szTip = icon, self.tip[:127]

        def menu():
            handle = user32.CreatePopupMenu()
            user32.AppendMenuW(handle, 0, self.OPEN, "打开窗口")
            user32.AppendMenuW(handle, 0x0800, 0, None)  # MF_SEPARATOR
            user32.AppendMenuW(handle, 0, self.QUIT, "退出（停止上报）")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self.hwnd)  # or the menu would not close on a click elsewhere
            chosen = user32.TrackPopupMenu(handle, 0x0100 | 0x0080 | 0x0002, point.x, point.y, 0, self.hwnd, None)
            user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
            user32.DestroyMenu(handle)
            return chosen

        def procedure(hwnd, message, wparam, lparam):
            if message == CALLBACK:
                if lparam == WM_LBUTTONUP:
                    self.on_open()
                elif lparam == WM_RBUTTONUP:
                    chosen = menu()
                    if chosen == self.OPEN:
                        self.on_open()
                    elif chosen == self.QUIT:
                        self.on_quit()
                return 0
            if message == taskbar_created and taskbar_created:
                shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
                return 0
            if message == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._procedure = api.WNDPROC(procedure)  # kept alive as long as the window
        window_class = api.WNDCLASSW(lpfnWndProc=self._procedure, hInstance=hinstance, lpszClassName="stzb-warroom-tray")
        user32.RegisterClassW(ctypes.byref(window_class))
        # A hidden top-level window (not message-only, which would miss TaskbarCreated).
        self.hwnd = user32.CreateWindowExW(0, "stzb-warroom-tray", self.tip, 0, 0, 0, 0, 0,
                                           None, None, hinstance, None)
        if not self.hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
        data.hWnd = self.hwnd
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
        self.ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))


_API = None


def _api():
    """user32/shell32/kernel32 with the signatures the tray and focus_window need (64-bit safe)."""
    global _API
    if _API is not None:
        return _API
    import ctypes
    import types
    from ctypes import wintypes
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                    ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = [
        (user32.DefWindowProcW, LRESULT, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
        (user32.RegisterClassW, wintypes.ATOM, [ctypes.POINTER(WNDCLASSW)]),
        (user32.CreateWindowExW, wintypes.HWND, [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                                 wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]),
        (user32.DestroyWindow, wintypes.BOOL, [wintypes.HWND]),
        (user32.PostMessageW, wintypes.BOOL, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
        (user32.PostQuitMessage, None, [ctypes.c_int]),
        (user32.GetMessageW, wintypes.BOOL, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]),
        (user32.TranslateMessage, wintypes.BOOL, [ctypes.POINTER(wintypes.MSG)]),
        (user32.DispatchMessageW, LRESULT, [ctypes.POINTER(wintypes.MSG)]),
        (user32.RegisterWindowMessageW, wintypes.UINT, [wintypes.LPCWSTR]),
        (user32.LoadIconW, wintypes.HICON, [wintypes.HINSTANCE, ctypes.c_void_p]),
        (user32.LoadImageW, wintypes.HANDLE, [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int,
                                              ctypes.c_int, wintypes.UINT]),
        (user32.GetSystemMetrics, ctypes.c_int, [ctypes.c_int]),
        (user32.EnumWindows, wintypes.BOOL, [WNDENUMPROC, wintypes.LPARAM]),
        (user32.GetWindowTextW, ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
        (user32.GetClassNameW, ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
        (user32.IsWindowVisible, wintypes.BOOL, [wintypes.HWND]),
        (user32.IsIconic, wintypes.BOOL, [wintypes.HWND]),
        (user32.ShowWindow, wintypes.BOOL, [wintypes.HWND, ctypes.c_int]),
        (user32.CreatePopupMenu, wintypes.HMENU, []),
        (user32.AppendMenuW, wintypes.BOOL, [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]),
        (user32.TrackPopupMenu, wintypes.BOOL, [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                                ctypes.c_int, wintypes.HWND, ctypes.c_void_p]),
        (user32.DestroyMenu, wintypes.BOOL, [wintypes.HMENU]),
        (user32.GetCursorPos, wintypes.BOOL, [ctypes.POINTER(wintypes.POINT)]),
        (user32.SetForegroundWindow, wintypes.BOOL, [wintypes.HWND]),
        (shell32.Shell_NotifyIconW, wintypes.BOOL, [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]),
        (kernel32.GetModuleHandleW, wintypes.HMODULE, [wintypes.LPCWSTR]),
    ]
    for function, restype, argtypes in signatures:
        function.restype, function.argtypes = restype, argtypes
    _API = types.SimpleNamespace(user32=user32, shell32=shell32, kernel32=kernel32, WNDPROC=WNDPROC, WNDENUMPROC=WNDENUMPROC,
                                 WNDCLASSW=WNDCLASSW, NOTIFYICONDATAW=NOTIFYICONDATAW)
    return _API
