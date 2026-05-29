# Fokus

Type a task, OpenAI picks the domains you need, a local proxy blocks the rest for 45 min.

## Setup

Easiest: double-click **Fokus** on the Desktop. That's it.

### From source (for development)

1. Install Python deps:
   ```powershell
   pip install -r requirements.txt
   ```

2. Set your OpenAI API key (once, persists across reboots):
   ```powershell
   [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", "sk-...", "User")
   ```

3. Run the app:
   ```powershell
   python fokus.py
   ```

### Rebuilding the .exe

```powershell
python generate_icon.py
pyinstaller --onefile --windowed --icon=icon.ico --name=Fokus --collect-data sv_ttk fokus.py
```

The exe lands in `dist/Fokus.exe`. The Desktop shortcut points at that path, so rebuilds are picked up automatically.

## Use

1. Type what you're doing.
2. Adjust minutes if needed (default 45).
3. Click **Start sprint**.

During the sprint, Windows routes all browser traffic through `127.0.0.1:7878`. The proxy only allows hostnames returned by OpenAI for your task; everything else gets `403 Forbidden`.

When the timer hits zero (or you close the app), the Windows proxy is restored to whatever it was before.

## How it works

- `fokus.py` — tkinter UI + sprint state.
- `proxy.py` — asyncio CONNECT/HTTP proxy on `127.0.0.1:7878`. Filters by hostname; allows subdomains automatically (`github.com` allows `api.github.com`).
- `windows_proxy.py` — reads/writes per-user proxy settings in the registry (`HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings`). On sprint start, backs up your current settings to `~/.fokus_proxy_backup.json` and switches to the local proxy. On sprint end, restores from the backup.

## If your internet breaks

The proxy approach has one real failure mode: if the Python app crashes or is killed hard while a sprint is active, Windows is still routed through `127.0.0.1:7878` but the proxy isn't running, so **no browser can load anything**. Two ways to fix it:

- Open the Fokus app again and click **Restore proxy (panic)**, OR
- Run:
  ```powershell
  python fokus.py --restore
  ```

Both read the backup file and put your proxy settings back where they were.

## Notes

- Browser-only enforcement. Apps that ignore Windows system proxy (some games, some VPN clients) won't be blocked.
- Firefox respects system proxy by default but check **Settings → Network Settings → Use system proxy** if needed.
- There's no early-end button by design — closing the app ends the sprint and restores the proxy.
