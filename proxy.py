"""Asyncio HTTP/HTTPS proxy that asks an AI per-domain whether to allow each request."""

import asyncio
import json
import threading
import time
from collections import deque

# Pure infrastructure — allowed without AI evaluation to keep first page loads snappy.
# Site-specific CDNs (licdn, twimg, fbcdn, etc.) are also safe to always-allow:
# they're only requested as subresources of their parent sites, so if the parent
# is blocked, the CDN never gets hit anyway.
INFRA_ALLOWLIST = {
    # Our own dependencies — must never be blocked or we deadlock
    "openai.com",
    "workers.dev",  # the Klar Cloudflare Worker backend

    # Search engines — useful for almost any task
    "google.com", "bing.com", "duckduckgo.com", "kagi.com",
    # AI assistants — these are tools the user works WITH, like an IDE or terminal
    "claude.ai", "anthropic.com",
    "chatgpt.com",
    "perplexity.ai",
    "copilot.microsoft.com",
    "cursor.sh", "cursor.com",
    "phind.com",
    # Generic CDNs / cloud / infra
    "googleapis.com", "gstatic.com", "googleusercontent.com",
    "cloudflare.com", "cloudflareinsights.com",
    "cloudfront.net", "fastly.net", "fastlylb.net",
    "akamai.net", "akamaized.net", "edgekey.net", "edgesuite.net",
    "jsdelivr.net", "unpkg.com", "cdnjs.com", "bootstrapcdn.com",
    "azureedge.net", "windows.net", "trafficmanager.net",
    "gvt1.com",  # Google update servers
    # Windows network checks
    "msftncsi.com", "msftconnecttest.com",
    # Fonts / typography
    "typekit.net", "fontawesome.com",
    # Site-specific static CDNs (loaded only if their parent site is allowed)
    "licdn.com",           # LinkedIn
    "twimg.com",           # Twitter / X
    "fbcdn.net",           # Facebook
    "cdninstagram.com",    # Instagram
    "pinimg.com",          # Pinterest
    "redditstatic.com", "redditmedia.com",  # Reddit
    "ytimg.com", "ggpht.com", "googlevideo.com",  # YouTube
    "githubassets.com", "githubusercontent.com", "github.io",  # GitHub
    "ttwstatic.com", "tiktokcdn.com",  # TikTok
    "muscache.com",        # Airbnb
    "wikimedia.org",       # Wikipedia static
    "wp.com",              # WordPress
    "shopify.com",         # Shopify storefronts share this CDN
    # Critical third-party services pages depend on
    "stripe.com", "stripe.network",
    "recaptcha.net", "hcaptcha.com",
    # Cookie-consent platforms — allow so banners can be dismissed.
    "cookielaw.org", "onetrust.com", "cookiepro.com",
    "trustarc.com", "didomi.io", "sourcepoint.com",
    # Common SSO / auth — allow so users can sign into work tools.
    "auth0.com", "okta.com",
    "login.microsoftonline.com", "microsoftonline.com",
    "accounts.google.com",
    "adobelogin.com",
    "withgoogle.com",              # Google Sign-In button embed
    # Upload / media CDNs for chat tools — required for paste-screenshot
    # and file-attachment flows. The parent app (ChatGPT, WhatsApp, etc.)
    # has its own AI judgement; the upload endpoint is plumbing.
    "oaiusercontent.com",          # ChatGPT file uploads
    "whatsapp.net",                # WhatsApp media servers
    "media.whatsapp.com",
    "telegram.org",                # Telegram media
    "discordapp.com", "discordapp.net",  # Discord media (parent is judged separately)
    "slack-edge.com", "slack-files.com",  # Slack uploads
    # SaaS-specific asset / CDN domains — loaded as subresources of their
    # parent app; if the parent is blocked, these never get hit anyway.
    "zohocdn.com", "zohopublic.com", "zohowebstatic.com",  # Zoho
    "sfdc-content.com", "force.com", "salesforceliveagent.com",  # Salesforce
    "squarespace-cdn.com", "sqspcdn.com",  # Squarespace
    "wixstatic.com", "wixsite.com",        # Wix
    "hubspot.net", "hsforms.net", "hs-analytics.net",  # HubSpot assets
    "fast.wistia.net", "wistia.net",       # Wistia video CDN
    "embed.typeform.com",                  # Typeform embed
    "assets.calendly.com",                 # Calendly widget assets
}

# Background plumbing domains the user never actively visits — analytics,
# telemetry, update servers, ad networks, error trackers. We silently block
# these without an AI call AND without an entry in the decisions feed.
NOISE_DOMAINS = {
    # Analytics / tracking
    "datadoghq.com", "datadoghq.eu",
    "google-analytics.com", "googletagmanager.com", "googleadservices.com",
    "doubleclick.net", "googlesyndication.com",
    "segment.io", "segment.com",
    "amplitude.com", "mixpanel.com",
    "hotjar.com", "fullstory.com", "clarity.ms",
    "newrelic.com", "sentry.io", "rollbar.com",
    "snowplowanalytics.com",
    "intercomcdn.com",  # Intercom embed CDN
    # Facebook / Meta tracking pixels
    "facebook.net", "fbevents.com", "fbsbx.com",
    # Adobe analytics + ad tracking
    "demdex.net", "everesttech.net", "adobedtm.com",
    "omtrdc.net", "2o7.net",
    # Ad networks + tag managers (silent block, never reach AI)
    "amazon-adsystem.com", "rubiconproject.com", "criteo.com",
    "scorecardresearch.com", "quantserve.com", "moatads.com",
    "casalemedia.com", "openx.net", "pubmatic.com",
    "s-onetag.com", "onetag-sys.com",
    "taboola.com", "outbrain.com",
    "adsrvr.org", "adnxs.com",
    # Windows / Microsoft telemetry
    "events.data.microsoft.com", "vortex.data.microsoft.com",
    "settings-win.data.microsoft.com", "telemetry.microsoft.com",
    "watson.microsoft.com",
    # Browser/OS plumbing (auto-updates, captive portal checks)
    "gvt1.com", "gvt2.com", "gvt3.com",  # Google update
    "msftncsi.com", "msftconnecttest.com",
    "edge.microsoft.com", "config.edge.skype.com",
    # NOTE: oaiusercontent.com REMOVED from noise — it's ChatGPT's file CDN.
    # Blocking it broke file uploads and screenshot pastes. Moved to
    # INFRA_ALLOWLIST below.
    # Embedded chat / support widgets — always load as silent third-party
    # subresources, never a primary destination.
    "intercom.io", "intercom.com",         # Intercom chat widget
    "widget.intercom.io",
    "drift.com", "driftt.com",             # Drift chat
    "crisp.chat", "crisp.email",           # Crisp chat
    "tawk.to",                             # Tawk.to chat
    "freshchat.com", "freshworks.com",     # Freshdesk/Freshchat widget
    "zopim.com",                           # Zendesk Chat (old)
    "wistia.com",                          # Wistia video embeds
    "canny.io",                            # Product feedback widget
    "uservoice.com",                       # Feedback widget
    "appcues.com", "appcues.net",          # User onboarding overlays
    "pendo.io",                            # Product analytics overlay
    "walkme.com",                          # Guided tours overlay
    "survicate.com",                       # Survey widget
    "delighted.com",                       # NPS survey widget
}

# Hostname pattern noise (regex on the FULL hostname, not just root).
# Matches things like "browser-intake-*.datadoghq.com", "events.foo.com",
# "telemetry.bar.com", "metrics.baz.com".
import re
NOISE_PATTERNS = [
    re.compile(r"(^|[.-])(analytics|tracking|telemetry|metrics|events|logs|"
               r"intake|beacon|pixel|sentry)([.-]|$)"),
    re.compile(r"^(stats|stat|track)\."),
]

# Root-domain patterns that indicate a pure CDN / asset host.
# These are silently ALLOWED (not blocked) — they're plumbing for sites
# the AI already judged. We check them separately from NOISE.
import re as _re
_CDN_ROOT_RE = _re.compile(
    r"(cdn|static|assets|edge|media|pub|public|thumbs|images|img|fonts)"
    r"\d*\.(com|net|io|org|co)$"
)


def _is_cdn_root(root):
    """True for roots like 'zohocdn.com', 'staticfiles.net', 'assets.io' etc."""
    return bool(_CDN_ROOT_RE.search(root))

# Country-code TLDs that take a second-level TLD prefix (so we don't reduce
# "bbc.co.uk" → "co.uk" and bucket every UK site together).
TWO_PART_TLDS = {
    "co.uk", "ac.uk", "gov.uk", "org.uk", "net.uk",
    "com.au", "net.au", "org.au", "gov.au", "edu.au",
    "co.nz", "net.nz", "org.nz", "govt.nz",
    "co.za", "org.za", "gov.za",
    "com.br", "net.br", "org.br",
    "co.jp", "ne.jp", "or.jp", "go.jp",
    "co.in", "net.in", "org.in", "gov.in",
    "com.mx", "com.ar", "com.tr",
}


def root_domain(host):
    """Strip www and reduce subdomains to a registrable root."""
    host = (host or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last_two = ".".join(parts[-2:])
    if last_two in TWO_PART_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


EVAL_PROMPT = """You are a focus assistant deciding whether to allow a web domain during a work sprint.

Sprint task: "{task}"
Domain requested: "{domain}"

{keyword_context}

Your job is to block clear time-wasters, not to over-police. Default to ALLOW when uncertain.

ALWAYS BLOCK:
- Social media used for browsing/entertainment: Facebook, Instagram, TikTok, Reddit, Twitter/X, Pinterest, Snapchat
- Video entertainment: YouTube, Netflix, Twitch, Disney+, Prime Video (unless task is explicitly about video)
- News sites, sports, gossip, shopping (Amazon, eBay, etc.) unless task is about those

ALWAYS ALLOW:
- Work tools, SaaS, productivity apps, documentation, Wikipedia, search engines
- Auth/SSO domains (accounts.google.com, login.microsoftonline.com, okta.com, auth0.com, etc.)
- Any domain that contains a word, name, or number from the sprint task — even as a substring.
  This is the most important rule. Examples:
  · Task mentions "45" → deep45.app contains "45" → ALLOW (user's own product)
  · Task mentions "eldy" → eldy.ch contains "eldy" → ALLOW (user's own site)
  · Task mentions "invoice" → invoiceninja.com contains "invoice" → ALLOW
  · Task mentions "john" → johnsmith.com contains "john" → ALLOW
  Numbers, brand names, project names, person names — if it appears in the domain, ALLOW it.
- CDN, static asset, upload, media domains → ALLOW (plumbing, not distractions)
- Unknown foreign or niche domains that could plausibly serve the task → ALLOW
- Any domain that looks like it could be the user's own website, product, or client → ALLOW

When in doubt: ALLOW. Blocking the wrong thing destroys a work session.

Reply with JSON only: {{"allow": true|false, "reason": "5-12 word explanation"}}"""


PROCESS_EVAL_PROMPT = """You are a focus assistant deciding whether to allow a desktop application during a focused work sprint.

Sprint task: "{task}"

Process: "{process_name}"

Be strict about distractions but cautious — killing a process loses the user's unsaved work, so when uncertain, ALLOW.
- Games, music streamers (Spotify), video players, casual chat → BLOCK unless explicitly part of the task
- Productivity (IDEs, terminals, editors, browsers, design tools, Office apps, file manager, Task Manager, Settings) → ALLOW
- Communication apps (Discord, Slack, Telegram, Teams, WhatsApp, Signal) → ALLOW only if the task is communication- or work-related, otherwise BLOCK
- Unknown / unrecognized process names that could plausibly be a relevant tool → ALLOW (do not kill things you don't recognize)
- Background services, helpers, updaters → ALLOW (they aren't user-facing distractions)

Reply with JSON only: {{"allow": true|false, "reason": "5-12 word explanation"}}"""


# Process names we should never consider — they're either Klar itself or critical Windows infrastructure.
PROCESS_NEVER_KILL = {
    "klar.exe", "python.exe", "pythonw.exe",
    "explorer.exe", "dwm.exe", "svchost.exe", "winlogon.exe", "csrss.exe",
    "system", "registry", "smss.exe", "wininit.exe", "services.exe",
    "lsass.exe", "fontdrvhost.exe", "taskhostw.exe", "ctfmon.exe",
    "searchindexer.exe", "searchhost.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "textinputhost.exe", "applicationframehost.exe",
    "runtimebroker.exe", "audiodg.exe", "conhost.exe",
}


OVERRIDE_LIMIT = 3   # manual overrides allowed per session


class FokusProxy:
    def __init__(self, port=7878):
        self.port = port
        self.task = ""
        self.active = False
        self._cache = {}            # root_domain -> bool
        self._locks = {}            # root_domain -> asyncio.Lock (dedup concurrent evals)
        self._overrides = set()     # root_domains the user manually unblocked this session
        self._override_count = 0    # how many overrides used so far this session
        self._decisions = deque(maxlen=100)
        self._loop = None
        self._client = None
        self._thread = None
        # Callable returning the auth headers to send on every AI request.
        # Set by fokus.py once at startup; we call it on every request so the
        # current user token is picked up (even after sign-in happens AFTER
        # the proxy has already started).
        self._headers_getter = None
        # Process-monitor state
        self._baseline_pids = set()
        self._baseline_names = set()
        self._process_cache = {}    # process_name (lower) -> bool
        self._process_locks = {}
        self._monitor_task = None

    # --- thread-safe state changes from the UI thread ---

    def start_sprint(self, task):
        def setup():
            self.task = task
            self.active = True
            self._cache.clear()
            self._locks.clear()
            self._overrides.clear()
            self._override_count = 0
            self._process_cache.clear()
            self._process_locks.clear()
            self._decisions.clear()
            self._snapshot_processes()
            if self._monitor_task is None or self._monitor_task.done():
                self._monitor_task = asyncio.create_task(self._monitor_loop())
        if self._loop:
            self._loop.call_soon_threadsafe(setup)
        else:
            setup()

    def end_sprint(self):
        def reset():
            self.task = ""
            self.active = False
            self._cache.clear()
            self._locks.clear()
            self._process_cache.clear()
            self._process_locks.clear()
            if self._monitor_task and not self._monitor_task.done():
                self._monitor_task.cancel()
        if self._loop:
            self._loop.call_soon_threadsafe(reset)
        else:
            reset()

    def _snapshot_processes(self):
        """Record everything currently running so we leave it alone."""
        try:
            import psutil
            self._baseline_pids = {p.pid for p in psutil.process_iter()}
            names = set()
            for p in psutil.process_iter(["name"]):
                try:
                    n = p.info["name"]
                    if n:
                        names.add(n.lower())
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            self._baseline_names = names
        except Exception:
            self._baseline_pids = set()
            self._baseline_names = set()

    def recent_decisions(self, n=20):
        return list(self._decisions)[-n:]

    def override_domain(self, domain):
        """Allow a previously-blocked domain for the rest of this session."""
        def add():
            root = root_domain(domain)
            self._overrides.add(root)
            self._override_count += 1
            self._cache[root] = True   # instant allow on next browser request
            # Flip the existing feed entry green so the UI updates immediately.
            for d in self._decisions:
                if d.get("domain") == root and not d.get("allowed") and d.get("kind") == "web":
                    d["allowed"] = True
                    d["reason"] = "allowed by user override"
                    break
        if self._loop:
            self._loop.call_soon_threadsafe(add)
        else:
            add()

    # --- per-domain evaluation ---

    @staticmethod
    def _is_noise(host, root):
        """True for background plumbing the user doesn't actively visit
        (analytics, telemetry, ad networks, update servers)."""
        if root in NOISE_DOMAINS:
            return True
        h = (host or "").lower()
        for pattern in NOISE_PATTERNS:
            if pattern.search(h):
                return True
        return False

    @staticmethod
    def _is_junk_host(host):
        """Reject IP fragments, numeric-only hosts, single-label nonsense."""
        if not host or len(host) < 4:
            return True
        # Numeric-only host like "97.3" or "192.168.1.1" — not a real domain.
        no_dot = host.replace(".", "")
        if no_dot.isdigit():
            return True
        if "." not in host:
            return True
        return False

    async def _evaluate(self, host):
        if not self.active or not self.task:
            return True
        # Silently allow IP fragments / junk — never enters the feed.
        if self._is_junk_host(host):
            return True
        root = root_domain(host)
        if root in self._overrides:   # user manually unblocked this session
            return True
        if root in INFRA_ALLOWLIST:
            return True
        # CDN-named root domains are always safe to allow silently — they're
        # subresource plumbing for sites the AI already judged.
        if _is_cdn_root(root):
            return True
        # Silently block known background noise — never enters the feed.
        if self._is_noise(host, root):
            return False
        if root in self._cache:
            return self._cache[root]
        lock = self._locks.setdefault(root, asyncio.Lock())
        async with lock:
            if root in self._cache:
                return self._cache[root]
            allowed, reason = await self._ask_ai(root)
            self._cache[root] = allowed
            self._decisions.append({
                "time": time.time(),
                "domain": root,
                "allowed": allowed,
                "reason": reason,
                "kind": "web",
            })
            return allowed

    # --- per-process evaluation ---

    async def _monitor_loop(self):
        try:
            while self.active:
                try:
                    await self._scan_processes()
                except Exception:
                    pass
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            pass

    async def _scan_processes(self):
        import psutil
        import win32gui
        import win32process

        # Collect PIDs that own a visible top-level window with a title.
        visible_pids = set()

        def _enum_cb(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                if not win32gui.GetWindowText(hwnd):
                    return True
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                visible_pids.add(pid)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(_enum_cb, None)
        except Exception:
            return

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pid = proc.info["pid"]
                name = proc.info["name"]
                if not name:
                    continue
                name_lower = name.lower()
                if name_lower in PROCESS_NEVER_KILL:
                    continue
                if pid not in visible_pids:
                    continue
                if pid in self._baseline_pids:
                    continue
                # Decision already cached?
                if name_lower in self._process_cache:
                    if not self._process_cache[name_lower]:
                        self._kill(proc)
                    continue
                lock = self._process_locks.setdefault(name_lower, asyncio.Lock())
                async with lock:
                    if name_lower in self._process_cache:
                        if not self._process_cache[name_lower]:
                            self._kill(proc)
                        continue
                    allowed, reason = await self._ask_ai_process(name)
                    self._process_cache[name_lower] = allowed
                    self._decisions.append({
                        "time": time.time(),
                        "domain": name,
                        "allowed": allowed,
                        "reason": reason,
                        "kind": "app",
                    })
                    if not allowed:
                        self._kill(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    @staticmethod
    def _kill(proc):
        try:
            proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    async def _ask_ai_process(self, name):
        if self._client is None:
            return True, "AI not ready"
        try:
            prompt = PROCESS_EVAL_PROMPT.format(task=self.task, process_name=name)
            resp = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model="gpt-4o-mini",
                    max_tokens=80,
                    response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": prompt}],
                    extra_headers=self._current_extra_headers(),
                ),
                timeout=8,
            )
            data = json.loads(resp.choices[0].message.content)
            # Default to ALLOW on parse failure for processes — safer than killing.
            return bool(data.get("allow", True)), str(data.get("reason", "") or "")[:80]
        except asyncio.TimeoutError:
            return True, "AI timeout — keeping app"
        except Exception as e:
            return True, f"AI error: {type(e).__name__} — keeping app"

    @staticmethod
    def _keyword_context(root, task):
        """Python-computed keyword overlap injected into the prompt so the AI
        can't fabricate a match it wasn't given."""
        _STOP = frozenset({
            "a", "an", "the", "and", "or", "for", "on", "in", "at", "to",
            "of", "by", "my", "our", "do", "get", "use", "via", "with",
            "from", "work", "working", "app", "apps", "this", "that",
            "make", "build", "fix", "add", "some", "all", "any", "new",
        })
        task_words = {
            w for w in re.split(r"\W+", task.lower())
            if len(w) > 2 and w not in _STOP
        }
        domain_parts = set(re.split(r"[.\-]", root.lower()))
        matched = sorted(task_words & domain_parts)
        if matched:
            return f"Keyword match (Python-verified): domain contains task word(s) {matched} → strong signal to ALLOW."
        return "Keyword match (Python-verified): domain shares NO words with the task — do not claim a keyword match."

    async def _ask_ai(self, root):
        if self._client is None:
            return False, "AI not ready"
        try:
            keyword_line = self._keyword_context(root, self.task)
            prompt = EVAL_PROMPT.format(task=self.task, domain=root,
                                        keyword_context=keyword_line)
            resp = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model="gpt-4o-mini",
                    max_tokens=80,
                    response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": prompt}],
                    extra_headers=self._current_extra_headers(),
                ),
                timeout=8,
            )
            data = json.loads(resp.choices[0].message.content)
            return bool(data.get("allow", False)), str(data.get("reason", "") or "")[:80]
        except asyncio.TimeoutError:
            return False, "AI timeout"
        except Exception as e:
            return False, f"error: {type(e).__name__}"

    # --- proxy plumbing ---

    async def _pipe(self, reader, writer):
        try:
            while True:
                data = await reader.read(16384)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not line:
                return
            parts = line.decode("latin-1", errors="ignore").strip().split()
            if len(parts) < 2:
                return
            method, target = parts[0].upper(), parts[1]

            host_header = ""
            while True:
                hl = await asyncio.wait_for(reader.readline(), timeout=10)
                if hl in (b"\r\n", b"\n", b""):
                    break
                if hl.lower().startswith(b"host:"):
                    host_header = (
                        hl.split(b":", 1)[1].decode("latin-1", errors="ignore")
                        .strip().split(":")[0]
                    )

            if method == "CONNECT":
                host = target.split(":")[0]
                try:
                    port = int(target.split(":")[1]) if ":" in target else 443
                except ValueError:
                    port = 443
                if not await self._evaluate(host):
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return
                try:
                    upr, upw = await asyncio.wait_for(
                        asyncio.open_connection(host, port), timeout=10
                    )
                except Exception:
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(
                    self._pipe(reader, upw),
                    self._pipe(upr, writer),
                    return_exceptions=True,
                )
            else:
                host = host_header
                if not host and target.startswith("http://"):
                    host = target[7:].split("/", 1)[0].split(":")[0]
                if not await self._evaluate(host):
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return
                path = target
                if path.startswith("http://"):
                    rest = path[7:].split("/", 1)
                    path = "/" + rest[1] if len(rest) > 1 else "/"
                try:
                    upr, upw = await asyncio.wait_for(
                        asyncio.open_connection(host, 80), timeout=10
                    )
                except Exception:
                    return
                upw.write(
                    f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
                    .encode("latin-1")
                )
                await upw.drain()
                await asyncio.gather(
                    self._pipe(reader, upw),
                    self._pipe(upr, writer),
                    return_exceptions=True,
                )
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _run(self):
        # AsyncOpenAI created inside this loop. We DON'T bake auth headers into
        # the client (token isn't known yet at startup) — they're passed per
        # request via extra_headers in _ask_ai / _ask_ai_process.
        from openai import AsyncOpenAI
        import httpx
        from fokus import get_openai_config
        _, base_url, _ = get_openai_config()
        http_client = httpx.AsyncClient(trust_env=False, timeout=15.0)
        kwargs = {"api_key": "klar-backend", "http_client": http_client}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)
        server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        async with server:
            await server.serve_forever()

    def _current_extra_headers(self):
        if not self._headers_getter:
            return {}
        try:
            return self._headers_getter() or {}
        except Exception:
            return {}

    def start(self):
        if self._thread:
            return

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._run())
            except Exception:
                pass

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
