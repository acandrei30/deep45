# Deploying the Klar backend (Cloudflare Worker)

One-time, ~5 minutes. After this, friends can use Klar without their own OpenAI key.

## 1. Create a Cloudflare account
Free. https://dash.cloudflare.com/sign-up

## 2. Install Wrangler (Cloudflare's CLI)
Needs Node.js installed first (https://nodejs.org).

```powershell
npm install -g wrangler
wrangler login
```

A browser window opens; click Allow.

## 3. Deploy the worker
From inside this `backend/` folder:

```powershell
cd C:\Users\Leia\Desktop\Python\fokus\backend
wrangler deploy
```

You'll see a URL like:
```
https://klar-proxy.YOUR-NAME.workers.dev
```
**Copy this URL.**

## 4. Set the secrets
Two secrets need to be set, one at a time:

```powershell
wrangler secret put OPENAI_API_KEY
# paste your sk-... key when prompted, press Enter

wrangler secret put KLAR_SHARED_SECRET
# paste a random string (e.g. from a password manager), press Enter
```

The shared secret is what your app sends so randos on the internet who find your Worker URL can't run up your OpenAI bill. Keep a copy — you need it in step 5.

## 5. Tell Klar to use the backend
Edit `..\klar_config.py` and fill in:

```python
DEFAULT_BACKEND_URL = "https://klar-proxy.YOUR-NAME.workers.dev"
DEFAULT_BACKEND_SECRET = "the-random-string-from-step-4"
```

Then rebuild the exe:

```powershell
cd ..
pyinstaller --clean --onefile --windowed --icon=icon.ico --name=Klar `
  --collect-data sv_ttk --collect-all sounddevice --collect-all psutil `
  --collect-all win32 --collect-all win32com --collect-all pywintypes `
  --hidden-import PIL.ImageTk --hidden-import win32gui --hidden-import win32process `
  fokus.py
```

The new `dist\Klar.exe` calls your Worker. Friends running it never need an OpenAI key.

## Monitoring & rotating
- Live logs: `wrangler tail`
- Rotate the shared secret if it leaks: `wrangler secret put KLAR_SHARED_SECRET` (new value), then update `klar_config.py` and rebuild.
- Watch OpenAI billing dashboard for unexpected spikes. The Worker only allows two endpoints (chat completions, transcriptions) so even a leaked secret can't trigger fine-tunes or image generation.
