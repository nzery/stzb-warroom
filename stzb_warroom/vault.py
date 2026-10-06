"""The window's tokens, kept encrypted with Windows DPAPI (only this Windows user can read them).

Nothing is saved elsewhere: on other systems the tokens come from ST_CLIENT_TOKEN.
"""

import os
from pathlib import Path

AVAILABLE = os.name == "nt"


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    call.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                     wintypes.DWORD, ctypes.POINTER(Blob)]
    call.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    if not call(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)):  # UI_FORBIDDEN
        raise OSError(ctypes.get_last_error(), "DPAPI failed")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def protect(data):
    return _dpapi(data, True)


def unprotect(data):
    return _dpapi(data, False)


def load(path):
    """The saved tokens (comma separated), or "" when none can be read."""
    try:
        return unprotect(Path(path).read_bytes()).decode("utf-8")
    except (OSError, ValueError):
        return ""


def save(path, tokens):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(protect(tokens.encode("utf-8")))
    temporary.replace(path)


def forget(path):
    Path(path).unlink(missing_ok=True)
