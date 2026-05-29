"""Get/set/restore the Windows per-user proxy via the registry."""

import ctypes
import json
import os
import sys
import winreg

REG_PATH    = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_NAME = "Fortyfive45Restore"
BACKUP_FILE = os.path.join(os.path.expanduser("~"), ".fokus_proxy_backup.json")


def _register_startup_restore():
    """Register a login-time auto-restore so a hard crash can't leave the
    proxy stuck — Windows will call 'Fortyfive.exe --restore' on next login."""
    try:
        exe = sys.executable  # the .exe when frozen by PyInstaller
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.SetValueEx(k, STARTUP_NAME, 0, winreg.REG_SZ, f'"{exe}" --restore')
    except Exception:
        pass


def _unregister_startup_restore():
    """Remove the login-time restore entry once the proxy is cleanly shut down."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            try:
                winreg.DeleteValue(k, STARTUP_NAME)
            except FileNotFoundError:
                pass
    except Exception:
        pass


def _read_proxy_state():
    state = {"ProxyEnable": 0, "ProxyServer": ""}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_READ) as k:
            try:
                state["ProxyEnable"] = int(winreg.QueryValueEx(k, "ProxyEnable")[0])
            except FileNotFoundError:
                pass
            try:
                state["ProxyServer"] = str(winreg.QueryValueEx(k, "ProxyServer")[0])
            except FileNotFoundError:
                pass
    except OSError:
        pass
    return state


def _write_proxy_state(enable, server):
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE
    ) as k:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enable else 0)
        winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, server or "")
    _notify_change()


def _notify_change():
    try:
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH
    except Exception:
        pass


def enable_proxy(server):
    """Set Windows proxy to `server` (e.g. '127.0.0.1:7878'), backing up the prior state.
    Also registers a login-time restore entry so a hard crash can't leave the proxy stuck."""
    if not os.path.exists(BACKUP_FILE):
        original = _read_proxy_state()
        with open(BACKUP_FILE, "w") as f:
            json.dump(original, f)
    _write_proxy_state(True, server)
    _register_startup_restore()


def restore_proxy():
    """Restore the proxy state captured by the last enable_proxy() call.
    Also removes the login-time restore entry since we're cleaning up cleanly."""
    _unregister_startup_restore()
    if os.path.exists(BACKUP_FILE):
        try:
            with open(BACKUP_FILE) as f:
                original = json.load(f)
            _write_proxy_state(
                bool(original.get("ProxyEnable", 0)),
                original.get("ProxyServer", ""),
            )
        finally:
            try:
                os.remove(BACKUP_FILE)
            except OSError:
                pass
    else:
        _write_proxy_state(False, "")


def proxy_is_fokus(port):
    """True if Windows is currently routed through our proxy on the given port."""
    s = _read_proxy_state()
    return s["ProxyEnable"] == 1 and s["ProxyServer"] == f"127.0.0.1:{port}"
