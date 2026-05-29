"""Get/set/restore the macOS system proxy via networksetup."""

import json
import os
import subprocess
import sys

BACKUP_FILE = os.path.join(os.path.expanduser("~"), ".fokus_proxy_backup.json")
LAUNCH_AGENT_LABEL = "com.fortyfive.restore"
LAUNCH_AGENT_PLIST = os.path.join(
    os.path.expanduser("~"), "Library", "LaunchAgents",
    f"{LAUNCH_AGENT_LABEL}.plist"
)


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""


def _get_network_services():
    out = _run(["networksetup", "-listallnetworkservices"])
    services = []
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("An asterisk") and not line.startswith("*"):
            services.append(line)
    return services


def _get_proxy_state(service, proxy_type="webproxy"):
    out = _run(["networksetup", f"-get{proxy_type}", service])
    state = {"enabled": False, "server": "", "port": 0}
    for line in out.splitlines():
        if line.startswith("Enabled:"):
            state["enabled"] = line.split(":", 1)[1].strip().lower() == "yes"
        elif line.startswith("Server:"):
            state["server"] = line.split(":", 1)[1].strip()
        elif line.startswith("Port:"):
            try:
                state["port"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return state


def _read_proxy_state():
    state = {}
    for svc in _get_network_services():
        state[svc] = {
            "http": _get_proxy_state(svc, "webproxy"),
            "https": _get_proxy_state(svc, "securewebproxy"),
        }
    return state


def _set_proxy(service, host, port):
    _run(["networksetup", "-setwebproxy", service, host, str(port)])
    _run(["networksetup", "-setwebproxystate", service, "on"])
    _run(["networksetup", "-setsecurewebproxy", service, host, str(port)])
    _run(["networksetup", "-setsecurewebproxystate", service, "on"])


def _clear_proxy(service):
    _run(["networksetup", "-setwebproxystate", service, "off"])
    _run(["networksetup", "-setsecurewebproxystate", service, "off"])


def _notify_change():
    pass  # networksetup changes take effect immediately on macOS


def _register_startup_restore():
    try:
        exe = sys.executable
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCH_AGENT_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exe}</string>
    <string>--restore</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>"""
        os.makedirs(os.path.dirname(LAUNCH_AGENT_PLIST), exist_ok=True)
        with open(LAUNCH_AGENT_PLIST, "w") as f:
            f.write(plist)
        _run(["launchctl", "load", LAUNCH_AGENT_PLIST])
    except Exception:
        pass


def _unregister_startup_restore():
    try:
        _run(["launchctl", "unload", LAUNCH_AGENT_PLIST])
        try:
            os.remove(LAUNCH_AGENT_PLIST)
        except OSError:
            pass
    except Exception:
        pass


def enable_proxy(server):
    """Set macOS proxy to `server` (e.g. '127.0.0.1:7878'), backing up prior state."""
    host, port = server.rsplit(":", 1)
    if not os.path.exists(BACKUP_FILE):
        original = _read_proxy_state()
        with open(BACKUP_FILE, "w") as f:
            json.dump(original, f)
    for svc in _get_network_services():
        _set_proxy(svc, host, int(port))
    _register_startup_restore()


def restore_proxy():
    """Restore the proxy state captured by enable_proxy()."""
    _unregister_startup_restore()
    if os.path.exists(BACKUP_FILE):
        try:
            with open(BACKUP_FILE) as f:
                original = json.load(f)
            for svc in _get_network_services():
                svc_state = original.get(svc, {})
                http = svc_state.get("http", {})
                https = svc_state.get("https", {})
                if http.get("enabled") and http.get("server"):
                    _run(["networksetup", "-setwebproxy", svc,
                          http["server"], str(http["port"])])
                    _run(["networksetup", "-setwebproxystate", svc, "on"])
                else:
                    _run(["networksetup", "-setwebproxystate", svc, "off"])
                if https.get("enabled") and https.get("server"):
                    _run(["networksetup", "-setsecurewebproxy", svc,
                          https["server"], str(https["port"])])
                    _run(["networksetup", "-setsecurewebproxystate", svc, "on"])
                else:
                    _run(["networksetup", "-setsecurewebproxystate", svc, "off"])
        finally:
            try:
                os.remove(BACKUP_FILE)
            except OSError:
                pass
    else:
        for svc in _get_network_services():
            _clear_proxy(svc)


def proxy_is_fokus(port):
    """True if macOS is currently routed through our proxy on the given port."""
    for svc in _get_network_services():
        state = _get_proxy_state(svc, "webproxy")
        if state["enabled"] and state["server"] == "127.0.0.1" and state["port"] == port:
            return True
    return False
