"""45 — laptop sibling of 30min.day and the phone "45". 4 sprints × 45 min, locked.
   Per-domain and per-process AI judging, plus camera presence detection."""

import atexit
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import wave
import platform as _platform
_IS_MAC = _platform.system() == "Darwin"
_IS_WIN = _platform.system() == "Windows"
if _IS_WIN:
    import winreg
from tkinter import messagebox

import numpy as np
import sounddevice as sd
from openai import OpenAI

import auth
import daily_state
import history
import klar_config
import settings
from presence import PresenceMonitor
from proxy import FokusProxy, OVERRIDE_LIMIT
if _IS_MAC:
    from mac_proxy import enable_proxy, restore_proxy
else:
    from windows_proxy import enable_proxy, restore_proxy


def get_backend_base_url():
    return os.environ.get("KLAR_BACKEND_URL",
                          klar_config.DEFAULT_BACKEND_URL).strip().rstrip("/")


def get_openai_config():
    """Routes through the Klar backend with the user's auth token.
    Returns (api_key_placeholder, base_url, headers)."""
    base = get_backend_base_url()
    if not base:
        return ("", None, {})
    headers = {"X-User-Token": auth.token()}
    return ("klar-backend", base + "/v1", headers)


PROXY_PORT = 7878
SPRINT_MINUTES = 45
WHISPER_MODEL = "whisper-1"
SAMPLE_RATE = 16000

# Warm dark palette
BG = "#1a1a1a"
SURFACE = "#262626"
SURFACE_HOVER = "#303030"
TEXT_PRIMARY = "#f0f0f0"
TEXT_SECONDARY = "#a8a8a8"
TEXT_TERTIARY = "#6e6e6e"
ACCENT = "#f5b454"
ACCENT_HOVER = "#e0a23c"
ACCENT_DIM = "#3a2f1a"
BORDER = "#353535"
WARN = "#dc2626"

PLACEHOLDER = "Describe your session…"

# Rotated each time the idle screen is shown. Mix of literal + casual.
TAGLINES = [
    "What are you on?",
    "Time to lock in.",
    "Session?",
    "What's the mission?",
    "Heads down.",
    "What's first?",
    "Deep work, please.",
    "One thing at a time.",
    "Let's get into it.",
    "What's the work?",
    "Forty-five.",
    "Lock in.",
]

# Segoe Fluent Icons / MDL2 Assets glyph codepoints
ICON_MIC = ""
ICON_RECORDING = ""

state = {
    "active": False,
    "task": "",
    "end_time": 0,
    "start_time": 0,
    "current_sprint_index": 0,
}

proxy = FokusProxy(port=PROXY_PORT)


def safe_restore():
    try:
        restore_proxy()
    except Exception:
        pass


def load_user_env_from_registry():
    if not _IS_WIN:
        return
    keys = ("OPENAI_API_KEY", "KLAR_BACKEND_URL", "KLAR_BACKEND_SECRET")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            for name in keys:
                if os.environ.get(name):
                    continue
                try:
                    val, _ = winreg.QueryValueEx(k, name)
                    if val:
                        os.environ[name] = val
                except FileNotFoundError:
                    pass
    except OSError:
        pass


class Recorder:
    def __init__(self):
        self.chunks = []
        self.stream = None

    @property
    def active(self):
        return self.stream is not None

    def start(self):
        self.chunks = []

        def cb(indata, frames, time_info, status):
            self.chunks.append(indata.copy())

        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                     dtype="int16", callback=cb)
        self.stream.start()

    def stop(self):
        if self.stream is None:
            return None
        try:
            self.stream.stop()
            self.stream.close()
        finally:
            self.stream = None
        if not self.chunks:
            return None
        return np.concatenate(self.chunks)


def _make_openai_client():
    import httpx
    api_key, base_url, extra_headers = get_openai_config()
    http_client = httpx.Client(
        trust_env=False, timeout=30.0,
        headers=extra_headers or None,
    )
    kwargs = {"api_key": api_key or "missing", "http_client": http_client}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def transcribe(audio_int16):
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_int16.tobytes())
        with open(path, "rb") as f:
            resp = _make_openai_client().audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=f,
            )
        return resp.text
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def make_mic_photo(size, color, glyph=""):
    """Render a glyph from Segoe Fluent Icons (Win11) / MDL2 Assets (Win10) at
    the given size + color. Falls back to a simple capsule if no icon font found.

    Default glyph \\uE720 = Microphone. \\uE7C8 = StopRecord."""
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    for path in ("C:/Windows/Fonts/SegoeIcons.ttf",
                 "C:/Windows/Fonts/segmdl2.ttf"):
        try:
            font = ImageFont.truetype(path, int(s * 0.78))
        except OSError:
            continue
        bbox = d.textbbox((0, 0), glyph, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (s - tw) / 2 - bbox[0]
        y = (s - th) / 2 - bbox[1]
        d.text((x, y), glyph, fill=color, font=font)
        return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))

    # Fallback: just a colored dot.
    cx = s / 2
    d.ellipse((cx - s * 0.25, s * 0.25, cx + s * 0.25, s * 0.75), fill=color)
    return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))


def make_rounded_button_images(width, height, fill_color, hover_color):
    """Two PhotoImages (normal + hover) for a capsule-shaped button background."""
    from PIL import Image, ImageColor, ImageDraw, ImageTk
    ss = 4
    radius = int(height * 0.5)

    def render(color):
        rgb = ImageColor.getrgb(color)
        img = Image.new("RGBA", (width * ss, height * ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(
            (0, 0, width * ss - 1, height * ss - 1),
            radius=radius * ss, fill=rgb + (255,),
        )
        return ImageTk.PhotoImage(img.resize((width, height), Image.LANCZOS))

    return render(fill_color), render(hover_color)


class RoundedButton(tk.Canvas):
    """Capsule-shaped button: PIL-rendered rounded background + native tk text."""

    def __init__(self, parent, text, command, bg=BG, fill=None, hover=None,
                 text_color="#1a1a1a", font=None, pad_x=44, pad_y=14):
        from tkinter import font as tkfont
        fill = fill or ACCENT
        hover = hover or ACCENT_HOVER
        font = font or ("Segoe UI", 12, "bold")
        fnt = tkfont.Font(family=font[0], size=font[1],
                          weight=font[2] if len(font) > 2 else "normal")
        text_w = fnt.measure(text)
        text_h = fnt.metrics("linespace")
        w = text_w + 2 * pad_x
        h = text_h + 2 * pad_y
        super().__init__(parent, width=w, height=h, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._normal, self._hover = make_rounded_button_images(w, h, fill, hover)
        self._img_id = self.create_image(0, 0, image=self._normal, anchor="nw")
        self._text_id = self.create_text(w / 2, h / 2, text=text,
                                          fill=text_color, font=fnt)
        self.bind("<Enter>", lambda _: self.itemconfig(self._img_id, image=self._hover))
        self.bind("<Leave>", lambda _: self.itemconfig(self._img_id, image=self._normal))
        self.bind("<Button-1>", lambda _: command() if command else None)

    def set_text(self, text):
        self.itemconfig(self._text_id, text=text)


def make_dots_photo(completed, total=4, dot_size=16, gap=12,
                    filled_color=ACCENT, empty_color=BORDER):
    """Render `completed`/`total` dots as a single Tk PhotoImage. Filled dots
    are solid accent; remaining ones are thin outline rings."""
    from PIL import Image, ImageDraw, ImageTk
    ss = 4
    s = dot_size * ss
    g = gap * ss
    total_w = total * s + (total - 1) * g
    img = Image.new("RGBA", (total_w, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    stroke = max(2, ss)
    for i in range(total):
        x = i * (s + g)
        if i < completed:
            d.ellipse((x, 0, x + s, s), fill=filled_color)
        else:
            d.ellipse((x + stroke, stroke, x + s - stroke, s - stroke),
                      outline=empty_color, width=stroke)
    final_w = total * dot_size + (total - 1) * gap
    return ImageTk.PhotoImage(img.resize((final_w, dot_size), Image.LANCZOS))


def _resource_path(name):
    """Find a bundled resource in both dev mode and PyInstaller --onefile."""
    base = getattr(sys, "_MEIPASS",
                   os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def make_logo_photo(size, color=None):
    """Brand mark (/45) as a PhotoImage. Loads logo.jpg, crops square,
    drops the near-black background so it blends into the dark UI, and
    resizes with LANCZOS. The `color` arg is accepted but ignored — the
    brand mark is a fixed image, not generated text."""
    from PIL import Image, ImageTk
    path = _resource_path("logo.jpg")
    img = Image.open(path).convert("RGBA")
    # Center-crop to square.
    if img.width != img.height:
        s = min(img.width, img.height)
        left = (img.width - s) // 2
        top = (img.height - s) // 2
        img = img.crop((left, top, left + s, top + s))
    # Drop near-black pixels to transparent so the logo blends into the dark UI.
    gray = img.convert("L")
    mask = gray.point(lambda p: 255 if p > 30 else 0)
    img.putalpha(mask)
    img = img.resize((size, size), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


def pick_icon_font(size):
    from tkinter import font as tkfont
    available = set(tkfont.families())
    for name in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if name in available:
            return (name, size)
    return ("Segoe UI Symbol", size)


class App:
    def __init__(self, root):
        self.root = root
        root.title("45")
        root.configure(bg=BG)
        # Start maximized — feels like a full-page app, but X still works while idle.
        root.state("zoomed")
        # Intercept all close attempts (X button, Alt+F4).
        root.protocol("WM_DELETE_WINDOW", self._on_close_request)

        self.recorder = Recorder()
        self.presence = None
        self._icon_font = pick_icon_font(13)

        self._window_icons = [make_logo_photo(s, ACCENT) for s in (16, 24, 32, 48, 64, 96, 128)]
        try:
            root.iconphoto(True, *self._window_icons)
        except tk.TclError:
            pass

        # Sidebar on the left (Claude-style), main content fills the rest.
        self._build_sidebar()

        self.container = tk.Frame(root, bg=BG)
        self.container.pack(side="left", fill="both", expand=True)

        self._build_idle()
        self._build_active()
        self._build_done()
        self._build_warning_overlay()

        self.notified_complete = False
        self._route()
        self.update_loop()
        # Show welcome cards on the very first launch.
        self.root.after(400, self._maybe_show_onboarding)

    SB_BG = "#161616"
    SB_W_EXPANDED = 260
    SB_W_COLLAPSED = 56

    def _build_sidebar(self):
        # Sidebar starts COLLAPSED. Hamburger + "45" sit on the same row at top.
        self.sidebar_expanded = False
        self._sb_animating = False
        self.sidebar = tk.Frame(self.root, bg=self.SB_BG, width=self.SB_W_COLLAPSED)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        # --- Top row: ☰ + 45 brand ---
        self.sb_header = tk.Frame(self.sidebar, bg=self.SB_BG)
        self.sb_header.pack(fill="x", pady=(20, 0), padx=14)

        self.sb_toggle = tk.Button(self.sb_header, text="☰",
                                    bg=self.SB_BG, fg=TEXT_SECONDARY,
                                    activebackground=self.SB_BG, activeforeground=TEXT_PRIMARY,
                                    relief="flat", borderwidth=0,
                                    font=("Segoe UI", 18), cursor="hand2",
                                    command=self._toggle_sidebar)
        self.sb_toggle.pack(side="left")

        # Brand shown only when expanded — packed/unpacked dynamically.
        self._sb_brand_img = make_logo_photo(32)
        self.sb_brand = tk.Label(self.sb_header, image=self._sb_brand_img,
                                  bg=self.SB_BG, borderwidth=0,
                                  highlightthickness=0)

        # Body (camera + restore + account) — built once, packed/unpacked on toggle.
        self.sidebar_content = tk.Frame(self.sidebar, bg=self.SB_BG)
        self._build_sidebar_content()

    def _build_sidebar_content(self):
        SB_BG = self.SB_BG

        # Top spacer so content sits below the header row.
        tk.Frame(self.sidebar_content, bg=SB_BG, height=24).pack(fill="x")

        # Camera presence toggle — no section label, just the control.
        self.sb_camera_var = tk.BooleanVar(value=settings.get("presence_enabled"))
        def _toggle_cam():
            settings.set_value("presence_enabled", self.sb_camera_var.get())
        tk.Checkbutton(self.sidebar_content, text="  Camera presence",
                       variable=self.sb_camera_var, command=_toggle_cam,
                       bg=SB_BG, fg=TEXT_PRIMARY, selectcolor=SURFACE,
                       activebackground=SB_BG, activeforeground=TEXT_PRIMARY,
                       font=("Segoe UI", 11), borderwidth=0, highlightthickness=0,
                       anchor="w", cursor="hand2").pack(padx=20, anchor="w", fill="x")

        # --- Recents (click → confirm → start session with that task) ---
        tk.Label(self.sidebar_content, text="RECENT",
                 bg=SB_BG, fg=TEXT_TERTIARY,
                 font=("Segoe UI", 8, "bold")).pack(pady=(20, 8), padx=24, anchor="w")

        self.sb_recents_holder = tk.Frame(self.sidebar_content, bg=SB_BG)
        self.sb_recents_holder.pack(fill="x", padx=14)
        self._render_sidebar_recents()

        # --- "Stop session" / "Fix browser" — dual-purpose button.
        fix_holder = tk.Frame(self.sidebar_content, bg=SB_BG)
        fix_holder.pack(fill="x", padx=20, pady=(28, 0))
        self.fix_btn = RoundedButton(
            fix_holder, text="↺  Fix browser",
            command=self._panic_restore,
            fill=SURFACE, hover=SURFACE_HOVER,
            text_color=TEXT_PRIMARY,
            pad_x=18, pad_y=8,
            font=("Segoe UI", 10, "bold"),
        )
        self.fix_btn.pack(anchor="w")
        self.fix_label = tk.Label(fix_holder,
                 text="If your browser stops loading after\na session, click this.",
                 bg=SB_BG, fg=TEXT_TERTIARY,
                 font=("Segoe UI", 9), justify="left",
                 anchor="w")
        self.fix_label.pack(anchor="w", pady=(6, 0))

        # --- Account at bottom ---
        account = tk.Frame(self.sidebar_content, bg=SB_BG, cursor="hand2")
        account.pack(side="bottom", fill="x", padx=20, pady=(0, 22))

        sep = tk.Frame(self.sidebar_content, bg="#252525", height=1)
        sep.pack(side="bottom", fill="x", padx=20, pady=(0, 16))

        email_str = auth.email() or "not signed in"
        initial = (email_str[:1] or "?").upper()
        self.sb_avatar_label = tk.Label(
            account, text=initial, bg=ACCENT, fg="#1a1a1a",
            font=("Segoe UI", 11, "bold"),
            width=2, padx=4, pady=2, cursor="hand2",
        )
        self.sb_avatar_label.pack(side="left", padx=(0, 12))

        info = tk.Frame(account, bg=SB_BG, cursor="hand2")
        info.pack(side="left", fill="x", expand=True)
        display_email = email_str if len(email_str) <= 22 else email_str[:20] + "…"
        self.sb_email_label = tk.Label(
            info, text=display_email, bg=SB_BG, fg=TEXT_PRIMARY,
            font=("Segoe UI", 10, "bold"), anchor="w", cursor="hand2",
        )
        self.sb_email_label.pack(anchor="w")
        tk.Label(info, text="Beta", bg=SB_BG, fg=TEXT_TERTIARY,
                 font=("Segoe UI", 9), anchor="w", cursor="hand2").pack(anchor="w")

        # Bind click on every widget in the account row to show the sign-out menu
        def _show_account_menu(e):
            m = tk.Menu(self.root, tearoff=0, bg=SURFACE, fg=TEXT_PRIMARY,
                        activebackground=SURFACE_HOVER, activeforeground=TEXT_PRIMARY,
                        font=("Segoe UI", 10), bd=0, relief="flat")
            m.add_command(label="Sign out", command=self._sign_out)
            try:
                m.tk_popup(e.x_root, e.y_root)
            finally:
                m.grab_release()

        for w in (account, self.sb_avatar_label, info, self.sb_email_label):
            w.bind("<Button-1>", _show_account_menu)

    def _refresh_sidebar_account(self):
        if not hasattr(self, "sb_email_label"):
            return
        email_str = auth.email() or "not signed in"
        initial = (email_str[:1] or "?").upper()
        display_email = email_str if len(email_str) <= 22 else email_str[:20] + "…"
        self.sb_avatar_label.config(text=initial)
        self.sb_email_label.config(text=display_email)

    def _sign_out(self):
        if state["active"]:
            messagebox.showwarning("45", "Stop the current session before signing out.")
            return
        if not messagebox.askyesno("Sign out", "Sign out of 45?"):
            return
        auth.clear()
        messagebox.showinfo("45", "Signed out. 45 will now close — reopen it to sign back in.")
        self.on_close()

    def _toggle_sidebar(self):
        # Ignore clicks while a previous animation is still running.
        if self._sb_animating:
            return
        self.sidebar_expanded = not self.sidebar_expanded
        target = self.SB_W_EXPANDED if self.sidebar_expanded else self.SB_W_COLLAPSED
        if not self.sidebar_expanded:
            # On collapse, hide content + brand immediately so the slide-in is empty.
            self.sb_brand.pack_forget()
            self.sidebar_content.pack_forget()
        current = self.sidebar.winfo_width() or self.SB_W_COLLAPSED
        self._animate_sidebar(current, target)

    def _animate_sidebar(self, current, target, step=28):
        self._sb_animating = True
        if abs(current - target) <= step:
            self.sidebar.config(width=target)
            if self.sidebar_expanded:
                self.sb_brand.pack(side="left", padx=(14, 0))
                self.sidebar_content.pack(fill="both", expand=True)
            self._sb_animating = False
            return
        new_w = current + step if target > current else current - step
        self.sidebar.config(width=new_w)
        # Snappier: ~8 frames at 10ms = ~80ms total.
        self.root.after(10, lambda: self._animate_sidebar(new_w, target, step))

    def _force_collapse_sidebar(self):
        """Instant collapse — used when starting a sprint."""
        if not self.sidebar_expanded:
            return
        self.sidebar_expanded = False
        try:
            self.sb_brand.pack_forget()
            self.sidebar_content.pack_forget()
            self.sidebar.config(width=self.SB_W_COLLAPSED)
        except tk.TclError:
            pass

    def _panic_restore(self):
        # Manual emergency restore from sidebar.
        safe_restore()
        self._cancel_restore_task()
        messagebox.showinfo("45", "Proxy restored. If a session was running, it's been forfeited.")
        state["active"] = False
        self._route()

    # ---------- layouts ----------

    def _build_idle(self):
        self.idle = tk.Frame(self.container, bg=BG)

        # Counter above the brand: just text, no dots.
        self.idle_counter_top = tk.Label(self.idle, text="0 / 4 done today",
                                          bg=BG, fg=TEXT_TERTIARY,
                                          font=("Segoe UI", 11))
        self.idle_counter_top.pack(pady=(0, 16))

        # Header: 45 mark + Georgia tagline. Tagline rotates per idle visit.
        header = tk.Frame(self.idle, bg=BG)
        header.pack(pady=(0, 56))
        self._idle_mark = make_logo_photo(56)
        tk.Label(header, image=self._idle_mark, bg=BG,
                 borderwidth=0, highlightthickness=0).pack(side="left", padx=(0, 20))
        self.tagline_label = tk.Label(header, text=random.choice(TAGLINES),
                                       bg=BG, fg=TEXT_PRIMARY,
                                       font=("Georgia", 38))
        self.tagline_label.pack(side="left", anchor="s", pady=(0, 6))

        # Input card — wider, taller, BORDERLESS.
        INPUT_W = 760
        INPUT_H = 140
        INPUT_R = 18
        PAD_X = 26
        PAD_Y = 22
        MIC_W, MIC_H = 40, 36
        MIC_MARGIN = 12  # extra breathing room from the input edges

        self.input_canvas = tk.Canvas(self.idle, width=INPUT_W, height=INPUT_H,
                                       bg=BG, highlightthickness=0)
        self.input_canvas.pack()

        self.task_text = tk.Text(self.input_canvas, height=2,
                                 font=("Segoe UI", 13),
                                 bg=SURFACE, fg=TEXT_TERTIARY,
                                 insertbackground=TEXT_PRIMARY,
                                 selectbackground=ACCENT_DIM,
                                 selectforeground=TEXT_PRIMARY,
                                 relief="flat", borderwidth=0,
                                 padx=0, pady=0, wrap="word",
                                 highlightthickness=0)
        self._is_placeholder = False
        self._show_placeholder()
        self.task_text.bind("<FocusIn>", self._on_focus_in)
        self.task_text.bind("<FocusOut>", self._on_focus_out)

        # PIL-rendered mic icon: always renders, regardless of system fonts.
        self._mic_idle = make_mic_photo(24, TEXT_SECONDARY)
        self._mic_hover = make_mic_photo(24, ACCENT)
        self._mic_rec = make_mic_photo(24, "#ffffff")
        self.mic_btn = tk.Button(self.input_canvas,
                                 image=self._mic_idle,
                                 bg=SURFACE,
                                 activebackground=SURFACE_HOVER,
                                 relief="flat", borderwidth=0,
                                 cursor="hand2",
                                 command=self.toggle_recording)
        self.mic_btn.bind("<Enter>", lambda _: self._mic_hover_state(True))
        self.mic_btn.bind("<Leave>", lambda _: self._mic_hover_state(False))

        def _layout_input(event):
            w, h = event.width, event.height
            self.input_canvas.delete("all")
            # Borderless rounded surface — no outline.
            self._draw_rounded_rect(self.input_canvas, 0, 0, w - 1, h - 1, INPUT_R,
                                    fill=SURFACE)
            self.task_text.place(
                in_=self.input_canvas,
                x=PAD_X, y=PAD_Y,
                width=w - 2 * PAD_X,
                height=h - 2 * PAD_Y - MIC_H - 8,
            )
            # ~16px from right and bottom edges — clearly inside the card but
            # not crammed in the corner.
            self.mic_btn.place(
                in_=self.input_canvas,
                x=w - 16 - MIC_W,
                y=h - 16 - MIC_H,
                width=MIC_W, height=MIC_H,
            )

        self.input_canvas.bind("<Configure>", _layout_input)

        self.mic_status = tk.Label(self.idle, text="",
                                   bg=BG, fg=TEXT_TERTIARY,
                                   font=("Segoe UI", 9, "italic"))
        self.mic_status.pack(pady=(6, 0))

        # Capsule START SPRINT button — PIL-rendered, anti-aliased.
        self.start_btn = RoundedButton(self.idle, text="Start session",
                                        command=self.start_sprint,
                                        pad_x=52, pad_y=16,
                                        font=("Segoe UI", 13, "bold"))
        self.start_btn.pack(pady=(32, 0))
        # Recents live in the sidebar now — see _render_sidebar_recents().

    def _build_active(self):
        self.active = tk.Frame(self.container, bg=BG)

        # Sprint label at top.
        # Dashboard layout — content nudged down so it sits visually centred.
        wrap = tk.Frame(self.active, bg=BG)
        wrap.pack(fill="both", expand=True, padx=72, pady=(120, 32))

        # --- LEFT: sprint label, task, timer ---
        left = tk.Frame(wrap, bg=BG)
        left.pack(side="left", padx=(0, 64), anchor="n", fill="both", expand=True)

        self.sprint_label = tk.Label(left, text="SESSION 01 / 04",
                                      bg=BG, fg=ACCENT,
                                      font=("Segoe UI", 11, "bold"))
        self.sprint_label.pack(anchor="w", pady=(0, 16))

        self.task_label = tk.Label(left, text="",
                                   bg=BG, fg=TEXT_PRIMARY,
                                   font=("Georgia", 24),
                                   wraplength=480, justify="left",
                                   anchor="w")
        self.task_label.pack(anchor="w", pady=(0, 56))

        # Bold amber timer — the focal element.
        self.timer_label = tk.Label(left, text="00:00",
                                    bg=BG, fg=ACCENT,
                                    font=("Segoe UI", 124, "bold"),
                                    pady=14)
        self.timer_label.pack(anchor="w")

        # Progress bar removed. Presence indicator removed too — the camera
        # check still fires and forfeits the sprint if you walk away; we just
        # don't surface a passive "● watching" label.
        self.progress_fg = None
        self.presence_label = tk.Label(left, bg=BG)  # placeholder so existing
                                                       # code that touches it
                                                       # doesn't crash.

        # --- RIGHT: decisions feed (aligned with sprint label at top) ---
        right = tk.Frame(wrap, bg=BG)
        right.pack(side="left", anchor="n", fill="both", expand=True)

        self.decisions_header = tk.Label(right, text="DECISIONS",
                                          bg=BG, fg=ACCENT,
                                          font=("Segoe UI", 10, "bold"))
        self.decisions_header.pack(anchor="w")

        divider = tk.Frame(right, bg=BORDER, height=1)
        divider.pack(fill="x", pady=(8, 16))

        self.decisions_text = tk.Text(right, height=20, width=58,
                                       font=("Consolas", 11),
                                       bg=BG, fg=TEXT_PRIMARY,
                                       relief="flat", borderwidth=0,
                                       highlightthickness=0,
                                       padx=0, pady=2, wrap="word",
                                       cursor="arrow")
        self.decisions_text.pack(fill="both", expand=True)
        self.decisions_text.tag_configure("allowed", foreground=ACCENT,
                                          font=("Segoe UI", 11, "bold"))
        self.decisions_text.tag_configure("blocked", foreground=WARN,
                                          font=("Segoe UI", 11, "bold"))
        self.decisions_text.tag_configure("domain", foreground=TEXT_PRIMARY)
        self.decisions_text.tag_configure("reason", foreground=TEXT_TERTIARY)
        self.decisions_text.config(state="disabled")

    def _build_done(self):
        self.done = tk.Frame(self.container, bg=BG)
        # Four big filled dots — the visual reward for completing the day.
        self._done_dots = make_dots_photo(4, total=4, dot_size=24, gap=18)
        tk.Label(self.done, image=self._done_dots, bg=BG,
                 borderwidth=0, highlightthickness=0).pack(pady=(0, 36))
        tk.Label(self.done, text="DONE FOR TODAY",
                 bg=BG, fg=TEXT_PRIMARY,
                 font=("Georgia", 44, "bold")).pack()
        tk.Label(self.done, text="4 × 45 — your day is done.",
                 bg=BG, fg=TEXT_SECONDARY,
                 font=("Segoe UI", 13)).pack(pady=(16, 4))
        tk.Label(self.done, text="Back tomorrow.",
                 bg=BG, fg=TEXT_TERTIARY,
                 font=("Georgia", 12, "italic")).pack(pady=(0, 0))

    def _build_warning_overlay(self):
        self.warn_overlay = tk.Frame(self.root, bg=WARN)
        tk.Label(self.warn_overlay, text="BACK TO YOUR DESK",
                 bg=WARN, fg="#ffffff",
                 font=("Arial Black", 48)).pack(expand=True)
        tk.Label(self.warn_overlay, text="Session forfeits in a few seconds.",
                 bg=WARN, fg="#ffe5e5",
                 font=("Segoe UI", 14)).pack(pady=(0, 60))

    @staticmethod
    def _draw_rounded_rect(canvas, x, y, w, h, r, fill, outline=None):
        canvas.create_rectangle(x + r, y, x + w - r, y + h + 1, fill=fill, outline="")
        canvas.create_rectangle(x, y + r, x + w + 1, y + h - r, fill=fill, outline="")
        for cx, cy, a in [(x, y, 90), (x + w - 2 * r, y, 0),
                          (x + w - 2 * r, y + h - 2 * r, 270), (x, y + h - 2 * r, 180)]:
            canvas.create_arc(cx, cy, cx + 2 * r, cy + 2 * r,
                              start=a, extent=90, fill=fill, outline="")
        if outline:
            canvas.create_line(x + r, y, x + w - r, y, fill=outline)
            canvas.create_line(x + w, y + r, x + w, y + h - r, fill=outline)
            canvas.create_line(x + r, y + h, x + w - r, y + h, fill=outline)
            canvas.create_line(x, y + r, x, y + h - r, fill=outline)
            for cx, cy, a in [(x, y, 90), (x + w - 2 * r, y, 0),
                              (x + w - 2 * r, y + h - 2 * r, 270), (x, y + h - 2 * r, 180)]:
                canvas.create_arc(cx, cy, cx + 2 * r, cy + 2 * r,
                                  start=a, extent=90, outline=outline, style="arc")

    # ---------- routing ----------

    def _update_fix_btn(self):
        if state["active"]:
            self.fix_btn.set_text("⏹  Stop session")
            self.fix_label.pack_forget()
        else:
            self.fix_btn.set_text("↺  Fix browser")
            self.fix_label.pack(anchor="w", pady=(6, 0))

    def _route(self):
        for frame in (self.idle, self.active, self.done):
            try:
                frame.place_forget()
            except tk.TclError:
                pass
        if state["active"]:
            self.active.place(relx=0, rely=0, relwidth=1, relheight=1)
        elif not daily_state.can_start():
            self.done.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.idle.place(relx=0.5, rely=0.5, anchor="center")
            self.tagline_label.config(text=random.choice(TAGLINES))
            self._refresh_idle_counter()
        self._update_fix_btn()

    def _post_sprint_to_server(self, task, sprint_index):
        """Fire-and-forget POST /sprints to record a completed sprint."""
        if not auth.signed_in():
            return
        base = get_backend_base_url()
        if not base:
            return
        def _post():
            try:
                import httpx
                client = httpx.Client(trust_env=False, timeout=10.0)
                client.post(
                    f"{base}/sprints",
                    json={"task": task, "sprint_index": sprint_index},
                    headers={"X-User-Token": auth.token(),
                             "Content-Type": "application/json"},
                )
            except Exception:
                pass
        threading.Thread(target=_post, daemon=True).start()

    def _maybe_show_signup_then_onboarding(self):
        """First-launch flow: onboarding cards first, then signup."""
        if not settings.get("onboarded"):
            self._show_onboarding(after=self._show_signup_if_needed)
        else:
            self._show_signup_if_needed()

    def _show_signup_if_needed(self):
        if not auth.signed_in():
            self._show_signup()

    def _show_signup(self):
        """Modal shown on first launch: Sign in / Create account tabs."""
        W, H = 520, 580
        win = tk.Toplevel(self.root)
        win.title("Welcome to 45")
        win.geometry(f"{W}x{H}")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        win.update_idletasks()
        sx = (win.winfo_screenwidth() - W) // 2
        sy = (win.winfo_screenheight() - H) // 2
        win.geometry(f"{W}x{H}+{sx}+{sy}")
        win.protocol("WM_DELETE_WINDOW", lambda: None)

        # Brand
        signup_logo = make_logo_photo(72)
        signup_brand = tk.Label(win, image=signup_logo, bg=BG,
                                borderwidth=0, highlightthickness=0)
        signup_brand.image = signup_logo  # keep ref
        signup_brand.pack(pady=(28, 4))

        # Mode tabs
        mode = ["signup"]   # mutable container so closures can update it
        tabs = tk.Frame(win, bg=BG)
        tabs.pack(pady=(16, 24))

        create_tab = tk.Button(tabs, text="Create account",
                                bg=BG, fg=ACCENT,
                                activebackground=BG, activeforeground=ACCENT,
                                relief="flat", borderwidth=0,
                                font=("Segoe UI", 12, "bold"),
                                cursor="hand2")
        signin_tab = tk.Button(tabs, text="Sign in",
                                bg=BG, fg=TEXT_TERTIARY,
                                activebackground=BG, activeforeground=TEXT_PRIMARY,
                                relief="flat", borderwidth=0,
                                font=("Segoe UI", 12),
                                cursor="hand2")
        create_tab.pack(side="left", padx=14)
        signin_tab.pack(side="left", padx=14)

        # Email
        email_entry = tk.Entry(win, font=("Segoe UI", 13),
                                bg=SURFACE, fg=TEXT_PRIMARY,
                                insertbackground=TEXT_PRIMARY,
                                relief="flat", borderwidth=0,
                                justify="center", width=32)
        email_entry.pack(ipady=10, padx=60, fill="x")
        tk.Label(win, text="email",
                 bg=BG, fg=TEXT_TERTIARY,
                 font=("Segoe UI", 9, "italic")).pack(pady=(4, 14))

        # Password
        pw_entry = tk.Entry(win, font=("Segoe UI", 13),
                             bg=SURFACE, fg=TEXT_PRIMARY,
                             insertbackground=TEXT_PRIMARY,
                             relief="flat", borderwidth=0,
                             justify="center", width=32,
                             show="•")
        pw_entry.pack(ipady=10, padx=60, fill="x")
        tk.Label(win, text="password (min 6 chars)",
                 bg=BG, fg=TEXT_TERTIARY,
                 font=("Segoe UI", 9, "italic")).pack(pady=(4, 0))

        status = tk.Label(win, text="", bg=BG, fg=WARN,
                          font=("Segoe UI", 10), wraplength=420, justify="center")
        status.pack(pady=(14, 4))

        def set_mode(m):
            mode[0] = m
            if m == "signup":
                create_tab.config(fg=ACCENT, font=("Segoe UI", 12, "bold"))
                signin_tab.config(fg=TEXT_TERTIARY, font=("Segoe UI", 12))
                action_btn.set_text("Create account")
                helper.config(text="Passwords are hashed. No marketing emails — your\n"
                                   "email is only used to log you back in.")
            else:
                create_tab.config(fg=TEXT_TERTIARY, font=("Segoe UI", 12))
                signin_tab.config(fg=ACCENT, font=("Segoe UI", 12, "bold"))
                action_btn.set_text("Sign in")
                helper.config(text="Welcome back.")
            status.config(text="")

        create_tab.config(command=lambda: set_mode("signup"))
        signin_tab.config(command=lambda: set_mode("login"))

        def submit():
            email_str = email_entry.get().strip().lower()
            pw_str = pw_entry.get()
            if "@" not in email_str or len(email_str) < 4:
                status.config(text="That doesn't look like an email.", fg=WARN)
                return
            if len(pw_str) < 6:
                status.config(text="Password must be at least 6 characters.", fg=WARN)
                return

            action_name = "Creating your account…" if mode[0] == "signup" else "Signing in…"
            status.config(text=action_name, fg=TEXT_TERTIARY)
            win.update_idletasks()

            try:
                import httpx
                base = get_backend_base_url()
                client = httpx.Client(trust_env=False, timeout=20.0)
                path = "/signup" if mode[0] == "signup" else "/login"
                r = client.post(f"{base}{path}",
                                json={"email": email_str, "password": pw_str},
                                headers={"Content-Type": "application/json"})
                if r.status_code in (200, 201):
                    data = r.json()
                    auth.save(data["token"], data["user_id"], data["email"])
                    win.grab_release()
                    win.destroy()
                    self._refresh_sidebar_account()
                    return
                # Show server's error message if it sent one
                try:
                    err = (r.json() or {}).get("error") or r.text
                except Exception:
                    err = r.text or f"Error {r.status_code}"
                status.config(text=err, fg=WARN)
            except Exception as e:
                status.config(text=f"Couldn't reach the server: {type(e).__name__}",
                              fg=WARN)

        # RoundedButton needs the text item id exposed so we can change its label
        # when the user toggles between tabs.
        action_btn = RoundedButton(win, text="Create account", command=submit,
                                    pad_x=44, pad_y=12,
                                    font=("Segoe UI", 12, "bold"))
        action_btn.pack(pady=(8, 8))

        email_entry.bind("<Return>", lambda _: pw_entry.focus_set())
        pw_entry.bind("<Return>", lambda _: submit())
        email_entry.focus_set()

        helper = tk.Label(win,
                           text="Passwords are hashed. No marketing emails — your\n"
                                "email is only used to log you back in.",
                           bg=BG, fg=TEXT_TERTIARY,
                           font=("Segoe UI", 9), justify="center",
                           wraplength=440)
        helper.pack(pady=(10, 0))

    def _maybe_show_onboarding(self):
        # Legacy entry point — now goes through signup-then-onboarding.
        self._maybe_show_signup_then_onboarding()

    def _show_onboarding(self, after=None):
        W, H = 720, 540
        win = tk.Toplevel(self.root)
        win.title("Welcome to 45")
        win.geometry(f"{W}x{H}")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        win.update_idletasks()
        x = (win.winfo_screenwidth() - W) // 2
        y = (win.winfo_screenheight() - H) // 2
        win.geometry(f"{W}x{H}+{x}+{y}")

        cards = [
            ("Welcome to 45.",
             "Four 45-minute sprints.\nOne deep work day.\n\n"
             "Locked structure, on purpose."),
            ("Type what you're on.",
             "Type or speak your task before each sprint.\n"
             "An AI uses it to decide what's relevant for the next 45 minutes."),
            ("We block the rest.",
             "News, social, video, off-task apps — all get blocked\n"
             "for the sprint, then go back to normal.\n\n"
             "No predefined lists. Per-task judgement."),
            ("Optional: camera presence.",
             "Optional. Off any time from the sidebar Settings.\n\n"
             "When on: your camera watches that you're at your desk while\n"
             "a sprint is active. Walk away for 20 seconds → sprint forfeits.\n\n"
             "All processing is local. No images leave your machine."),
            ("Ready?",
             "Pick what you'll work on. Hit Start.\nLock in for 45 minutes.\n\n"
             "Do this four times. That's the day."),
        ]

        idx = [0]
        content = tk.Frame(win, bg=BG)
        content.pack(fill="both", expand=True, padx=40, pady=(36, 16))

        title_label = tk.Label(content, text="", bg=BG, fg=TEXT_PRIMARY,
                                font=("Georgia", 28, "bold"), justify="left")
        title_label.pack(anchor="w")

        body_label = tk.Label(content, text="", bg=BG, fg=TEXT_SECONDARY,
                               font=("Segoe UI", 12), justify="left", wraplength=480)
        body_label.pack(anchor="w", pady=(16, 0))

        nav = tk.Frame(win, bg=BG)
        nav.pack(side="bottom", fill="x", padx=40, pady=(0, 24))

        # Progress dots in the middle
        dots_holder = tk.Label(nav, bg=BG, borderwidth=0, highlightthickness=0)
        dots_holder.pack(side="top", pady=(0, 16))

        # Buttons row
        btn_row = tk.Frame(nav, bg=BG)
        btn_row.pack(side="top", fill="x")

        back_btn = tk.Button(btn_row, text="Back", bg=BG, fg=TEXT_TERTIARY,
                              activebackground=BG, activeforeground=TEXT_PRIMARY,
                              relief="flat", borderwidth=0,
                              font=("Segoe UI", 11), cursor="hand2",
                              command=lambda: change(-1))
        back_btn.pack(side="left")

        next_btn = RoundedButton(btn_row, text="Next →",
                                  command=lambda: change(1),
                                  pad_x=28, pad_y=10,
                                  font=("Segoe UI", 11, "bold"))
        next_btn.pack(side="right")

        win._onboarding_imgs = []

        def render():
            i = idx[0]
            title, body = cards[i]
            title_label.config(text=title)
            body_label.config(text=body)
            # Progress dots
            dot_img = make_dots_photo(i + 1, total=len(cards),
                                       dot_size=8, gap=6,
                                       filled_color=ACCENT, empty_color=BORDER)
            win._onboarding_imgs.append(dot_img)
            dots_holder.config(image=dot_img)
            # Back button visibility
            back_btn.pack_forget()
            if i > 0:
                back_btn.pack(side="left")
            # Last card → "Get started"
            is_last = (i == len(cards) - 1)
            next_btn.destroy_all = None  # placeholder
            # Re-create next button with new label/command on last card
            for w in btn_row.pack_slaves():
                if w is back_btn:
                    continue
                w.destroy()
            label = "Get started" if is_last else "Next →"
            command = finish if is_last else (lambda: change(1))
            new_next = RoundedButton(btn_row, text=label, command=command,
                                      pad_x=28, pad_y=10,
                                      font=("Segoe UI", 11, "bold"))
            new_next.pack(side="right")

        def change(delta):
            idx[0] = max(0, min(len(cards) - 1, idx[0] + delta))
            render()

        def finish():
            settings.set_value("onboarded", True)
            win.destroy()
            self._offer_desktop_shortcut()
            if after:
                after()

    def _offer_desktop_shortcut(self):
        """Offer to create a Desktop shortcut on first run (frozen exe only)."""
        if not getattr(sys, "frozen", False):
            return
        if not messagebox.askyesno("45", "Add a shortcut to your Desktop?", parent=self.root):
            return
        try:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            if _IS_MAC:
                # On Mac: create a symlink on the Desktop pointing to the .app bundle
                import subprocess as _sp
                app_path = os.path.dirname(os.path.dirname(sys.executable))  # .app bundle
                link = os.path.join(desktop, "45.app")
                if not os.path.exists(link):
                    os.symlink(app_path, link)
            else:
                import win32com.client
                lnk = os.path.join(desktop, "45.lnk")
                shell = win32com.client.Dispatch("WScript.Shell")
                sc = shell.CreateShortCut(lnk)
                sc.Targetpath = sys.executable
                sc.WorkingDirectory = os.path.dirname(sys.executable)
                sc.IconLocation = sys.executable
                sc.save()
        except Exception:
            pass

        render()

    def _open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.geometry("440x300")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)

        tk.Label(win, text="Settings", bg=BG, fg=TEXT_PRIMARY,
                 font=("Georgia", 24, "bold")).pack(pady=(28, 24))

        # Camera toggle
        row = tk.Frame(win, bg=BG)
        row.pack(fill="x", padx=40, pady=(0, 4))

        presence_var = tk.BooleanVar(value=settings.get("presence_enabled"))

        def on_toggle():
            settings.set_value("presence_enabled", presence_var.get())

        cb = tk.Checkbutton(row, text="  Camera presence detection",
                            variable=presence_var, command=on_toggle,
                            bg=BG, fg=TEXT_PRIMARY, selectcolor=SURFACE,
                            activebackground=BG, activeforeground=TEXT_PRIMARY,
                            font=("Segoe UI", 12), borderwidth=0,
                            highlightthickness=0)
        cb.pack(anchor="w")
        tk.Label(win, bg=BG, fg=TEXT_TERTIARY,
                 text="When on, your sprint forfeits if you step away from\n"
                      "your laptop for more than 20 seconds. The camera light\n"
                      "stays on for the full 45 minutes. All processing is local.",
                 font=("Segoe UI", 9), justify="left",
                 wraplength=360).pack(anchor="w", padx=64, pady=(4, 0))

        # Close button
        close_btn = RoundedButton(win, text="Close",
                                   command=win.destroy,
                                   pad_x=36, pad_y=10,
                                   font=("Segoe UI", 11, "bold"))
        close_btn.pack(pady=(36, 24))

    def _refresh_idle_counter(self):
        done = daily_state.completed_today()
        remaining = daily_state.DAILY_LIMIT - done
        if done == 0:
            text = "0 / 4 done today"
        elif remaining == 0:
            text = "4 / 4 done today"
        elif remaining == 1:
            text = f"{done} / 4 done today — 1 to go"
        else:
            text = f"{done} / 4 done today — {remaining} to go"
        self.idle_counter_top.config(text=text)
        self._render_sidebar_recents()

    def _render_sidebar_recents(self):
        if not hasattr(self, "sb_recents_holder"):
            return
        for w in self.sb_recents_holder.winfo_children():
            w.destroy()
        recents = history.recent(7)
        if not recents:
            tk.Label(self.sb_recents_holder, text="  (none yet)",
                     bg=self.SB_BG, fg=TEXT_TERTIARY,
                     font=("Segoe UI", 9, "italic"),
                     anchor="w").pack(fill="x", padx=10, pady=4, anchor="w")
            return
        SB = self.SB_BG
        for task in recents:
            label = task if len(task) <= 22 else task[:20].rstrip() + "…"

            row = tk.Frame(self.sb_recents_holder, bg=SB)
            row.pack(fill="x")

            task_btn = tk.Button(row, text=label,
                                 bg=SB, fg=TEXT_SECONDARY,
                                 activebackground=SURFACE,
                                 activeforeground=TEXT_PRIMARY,
                                 relief="flat", borderwidth=0,
                                 font=("Segoe UI", 10), cursor="hand2",
                                 anchor="w", padx=10, pady=6,
                                 command=lambda t=task: self._confirm_reuse_task(t))
            task_btn.pack(side="left", fill="x", expand=True)

            # Three-dot button: invisible (fg == bg) until hover.
            dots_btn = tk.Button(row, text="⋯",
                                 bg=SB, fg=SB,
                                 activebackground=SURFACE,
                                 activeforeground=TEXT_PRIMARY,
                                 relief="flat", borderwidth=0,
                                 font=("Segoe UI", 11), cursor="hand2",
                                 padx=8, pady=6)
            dots_btn.pack(side="right")

            def _enter(e, r=row, tb=task_btn, db=dots_btn):
                r.config(bg=SURFACE)
                tb.config(bg=SURFACE)
                db.config(bg=SURFACE, fg=TEXT_TERTIARY)

            def _leave(e, r=row, tb=task_btn, db=dots_btn):
                r.config(bg=SB)
                tb.config(bg=SB)
                db.config(bg=SB, fg=SB)

            for w in (row, task_btn, dots_btn):
                w.bind("<Enter>", _enter)
                w.bind("<Leave>", _leave)

            def _show_menu(t=task, db=dots_btn):
                menu = tk.Menu(self.root, tearoff=0,
                               bg=SURFACE, fg=TEXT_PRIMARY,
                               activebackground=ACCENT_DIM,
                               activeforeground=TEXT_PRIMARY,
                               borderwidth=0, relief="flat",
                               font=("Segoe UI", 10))
                menu.add_command(
                    label="Archive",
                    command=lambda: self._archive_task(t),
                )
                try:
                    menu.tk_popup(
                        db.winfo_rootx(),
                        db.winfo_rooty() + db.winfo_height(),
                    )
                finally:
                    menu.grab_release()

            dots_btn.config(command=_show_menu)

    def _confirm_reuse_task(self, task):
        if state["active"]:
            return  # ignore during an active sprint
        answer = messagebox.askyesno(
            "Start session?",
            f"Start a new sprint with this task?\n\n  {task}",
        )
        if not answer:
            return
        # Prefill the input visually, then start.
        self._clear_placeholder()
        self.task_text.delete("1.0", "end")
        self.task_text.insert("1.0", task)
        self.task_text.config(fg=TEXT_PRIMARY)
        self.start_sprint()

    def _archive_task(self, task):
        history.remove(task)
        self._render_sidebar_recents()

    # ---------- placeholder ----------

    def _show_placeholder(self):
        self.task_text.delete("1.0", "end")
        self.task_text.insert("1.0", PLACEHOLDER)
        self.task_text.config(fg=TEXT_TERTIARY)
        self._is_placeholder = True

    def _clear_placeholder(self):
        if self._is_placeholder:
            self.task_text.delete("1.0", "end")
            self.task_text.config(fg=TEXT_PRIMARY)
            self._is_placeholder = False

    def _on_focus_in(self, _):
        self._clear_placeholder()

    def _on_focus_out(self, _):
        if not self.task_text.get("1.0", "end").strip():
            self._show_placeholder()

    def _real_task(self):
        if self._is_placeholder:
            return ""
        return self.task_text.get("1.0", "end").strip()

    # ---------- recording / transcription ----------

    def _mic_hover_state(self, hovering):
        if self.recorder.active:
            return
        self.mic_btn.config(image=self._mic_hover if hovering else self._mic_idle)

    def toggle_recording(self):
        if self.recorder.active:
            audio = self.recorder.stop()
            self.mic_btn.config(image=self._mic_idle, bg=SURFACE,
                                state="disabled")
            if audio is None or len(audio) < SAMPLE_RATE * 0.3:
                self._reset_mic()
                self.mic_status.config(text="too short.", fg=TEXT_TERTIARY)
                return
            self.mic_status.config(text="transcribing…", fg=TEXT_SECONDARY)
            threading.Thread(target=self._do_transcribe,
                             args=(audio,), daemon=True).start()
            return
        try:
            self.recorder.start()
        except Exception as e:
            messagebox.showerror("45", f"Couldn't access microphone:\n{e}")
            return
        self.mic_btn.config(image=self._mic_rec, bg=WARN, activebackground=WARN)
        self.mic_status.config(text="recording — click again to stop.", fg=WARN)

    def _do_transcribe(self, audio):
        try:
            text = transcribe(audio)
            self.root.after(0, lambda: self._fill_transcription(text))
        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda: self._transcription_error(msg))

    def _fill_transcription(self, text):
        text = (text or "").strip()
        if text:
            self._clear_placeholder()
            existing = self.task_text.get("1.0", "end").strip()
            if existing:
                self.task_text.insert("end", " " + text)
            else:
                self.task_text.insert("1.0", text)
            self.task_text.config(fg=TEXT_PRIMARY)
        self._reset_mic()
        self.mic_status.config(text="")

    def _transcription_error(self, msg):
        self._reset_mic()
        self.mic_status.config(text="")
        messagebox.showerror("45", f"Transcription failed:\n{msg}")

    def _reset_mic(self):
        self.mic_btn.config(image=self._mic_idle, bg=SURFACE,
                            activebackground=SURFACE_HOVER, state="normal")

    # ---------- window mode (fullscreen lock during sprint) ----------

    def _enter_sprint_window_mode(self):
        # Stay maximized so user can alt-tab to their editor / browser as needed.
        # The X intercept already blocks close attempts; we don't lock the window.
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass

    def _exit_sprint_window_mode(self):
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass

    # --- Scheduled-task safety net (Windows Task Scheduler) ---
    # Survives Task Manager kill, crashes, BSOD. Registered at sprint start,
    # cancelled on clean end. If everything dies, the task still fires.

    TASK_NAME = "Fortyfive-AutoRestore"

    def _schedule_restore_task(self, fire_at):
        """Register a one-time Windows task that runs `Fortyfive.exe --restore`
        at the given datetime. Overwrites any existing task with /F."""
        import datetime as _dt
        try:
            cmd = [
                "schtasks", "/Create",
                "/SC", "ONCE",
                "/TN", self.TASK_NAME,
                "/TR", f'"{sys.executable}" --restore',
                "/ST", fire_at.strftime("%H:%M"),
                "/SD", fire_at.strftime("%m/%d/%Y"),
                "/F",
            ]
            subprocess.run(
                cmd, capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass

    def _cancel_restore_task(self):
        try:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", self.TASK_NAME, "/F"],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass

    def _on_close_request(self):
        # Hard-block close attempts during a sprint. Only escape is Task Manager.
        if state["active"]:
            return
        self.on_close()

    # ---------- sprint control ----------

    def start_sprint(self):
        if not daily_state.can_start():
            messagebox.showinfo("45", "Done for today. Come back tomorrow.")
            self._route()
            return

        task = self._real_task()
        if not task:
            messagebox.showwarning("45", "Type or speak what you're on first.")
            return

        _, base_url, _ = get_openai_config()
        if not base_url:
            messagebox.showerror("45",
                "No backend configured.\n\n"
                "This build needs DEFAULT_BACKEND_URL set in klar_config.py "
                "(or the KLAR_BACKEND_URL env var) — that's how 45 reaches "
                "the AI judge.")
            return

        if not auth.signed_in():
            messagebox.showwarning("45",
                "You're not signed in.\n\nThe app needs an account to track "
                "your sessions. Close and reopen 45 to go through the signup.")
            return

        # Visual feedback during camera init — even fast, can be ~1s.
        try:
            self.root.config(cursor="wait")
            self.root.update_idletasks()
        except tk.TclError:
            pass

        def _reset_cursor():
            try:
                self.root.config(cursor="")
            except tk.TclError:
                pass

        # Camera presence — controlled by Settings. Off by default for those
        # without a camera or who don't want it.
        if settings.get("presence_enabled"):
            self.presence = PresenceMonitor(on_status=self._on_presence_status)
            if not self.presence.start():
                self.presence = None
                _reset_cursor()
                if messagebox.askyesno("45",
                    "Couldn't access the camera.\n\n"
                    "Continue without presence detection?\n"
                    "(You can permanently disable the camera in Settings.)"):
                    pass  # proceed without presence
                else:
                    return
        else:
            self.presence = None

        now = time.time()
        state["active"] = True
        state["task"] = task
        state["start_time"] = now
        state["end_time"] = now + SPRINT_MINUTES * 60
        state["current_sprint_index"] = daily_state.completed_today() + 1

        # Record the task in local history immediately — sidebar Recent should
        # show this sprint's task without waiting for completion.
        history.add(task)
        self._render_sidebar_recents()

        # Auto-collapse the sidebar so it's out of the way during a sprint.
        self._force_collapse_sidebar()

        proxy.start_sprint(task)
        try:
            enable_proxy(f"127.0.0.1:{PROXY_PORT}")
        except Exception as e:
            messagebox.showerror("45", f"Couldn't set Windows proxy:\n{e}")
            state["active"] = False
            proxy.end_sprint()
            if self.presence:
                self.presence.stop()
                self.presence = None
            _reset_cursor()
            return

        # Schedule a Windows Task to auto-restore proxy at end_time + 5 minutes,
        # in case we get killed via Task Manager and the clean-exit hooks don't fire.
        from datetime import datetime, timedelta
        fire_at = datetime.fromtimestamp(state["end_time"]) + timedelta(minutes=5)
        self._schedule_restore_task(fire_at)

        _reset_cursor()

        self.notified_complete = False
        if self.presence is not None:
            self.presence_label.config(text="● watching", fg=TEXT_TERTIARY)
        else:
            self.presence_label.config(text="○ camera off", fg=TEXT_TERTIARY)
        # Update sprint label ("SPRINT 02 / 04" etc.)
        idx = state["current_sprint_index"]
        self.sprint_label.config(text=f"SESSION {idx:02d} / 04")
        self._enter_sprint_window_mode()
        self._route()

    def end_sprint(self, completed=True, forfeit_reason=None):
        state["active"] = False
        proxy.end_sprint()
        safe_restore()
        if self.presence is not None:
            self.presence.stop()
            self.presence = None
        self._cancel_restore_task()
        self._hide_warning()
        self._exit_sprint_window_mode()
        if completed:
            daily_state.mark_completed()
            # history.add() now fires at sprint START — don't double-add here.
            self._post_sprint_to_server(state["task"], state.get("current_sprint_index", 0))
            self.notified_complete = True
            self._route()
            done = daily_state.completed_today()
            if done >= daily_state.DAILY_LIMIT:
                messagebox.showinfo("45", "DONE.\n\n04 / 04 sprints completed today.")
            else:
                messagebox.showinfo("45", f"Sprint complete.\n\n{done:02d} / 04 done today.")
        else:
            self._route()
            if forfeit_reason:
                messagebox.showwarning("45",
                    f"SPRINT FORFEITED\n\n{forfeit_reason}\n\nDid not count toward today's 04.")

    def _on_presence_status(self, absent_seconds, failed):
        self.root.after(0, self._handle_presence, absent_seconds, failed)

    def _handle_presence(self, absent_seconds, failed):
        if not state["active"]:
            return
        if failed:
            self.end_sprint(completed=False,
                            forfeit_reason="No presence detected at the camera.")
            return
        warn_at = PresenceMonitor.WARN_THRESHOLD
        if absent_seconds >= warn_at:
            self._show_warning()
            self.presence_label.config(
                text=f"● away ({int(absent_seconds)}s)", fg=WARN)
        else:
            self._hide_warning()
            if absent_seconds == 0:
                self.presence_label.config(text="● checked — you're here",
                                           fg=TEXT_TERTIARY)
            else:
                self.presence_label.config(
                    text=f"● didn't see you ({int(absent_seconds)}s)",
                    fg=WARN)

    def _show_warning(self):
        try:
            self.warn_overlay.place(in_=self.root, x=0, y=0, relwidth=1, relheight=1)
            self.warn_overlay.lift()
        except tk.TclError:
            pass

    def _hide_warning(self):
        try:
            self.warn_overlay.place_forget()
        except tk.TclError:
            pass

    def update_loop(self):
        if state["active"]:
            now = time.time()
            remaining = max(0, int(state["end_time"] - now))
            elapsed = max(0, now - state["start_time"])
            total = max(1.0, state["end_time"] - state["start_time"])
            mm, ss = divmod(remaining, 60)
            self.timer_label.config(text=f"{mm:02d}:{ss:02d}")
            self.task_label.config(text=state["task"][:160])
            # Progress bar removed by design — timer counting down is enough.
            self._render_decisions(proxy.recent_decisions(8))
            if remaining <= 0:
                self.end_sprint(completed=True)
        self.root.after(500, self.update_loop)

    def _render_decisions(self, decisions):
        used = proxy._override_count
        remaining = OVERRIDE_LIMIT - used
        if remaining > 0:
            s = "s" if remaining != 1 else ""
            self.decisions_header.config(
                text=f"DECISIONS  ·  {remaining} override{s} left"
            )
        else:
            self.decisions_header.config(text="DECISIONS  ·  no overrides left")

        self.decisions_text.config(state="normal")
        self.decisions_text.delete("1.0", "end")
        # Remove stale per-domain override link tags so bindings don't pile up.
        for tag in list(self.decisions_text.tag_names()):
            if tag.startswith("override_"):
                self.decisions_text.tag_delete(tag)
        if not decisions:
            self.decisions_text.insert("end",
                                       "Waiting for the first request…",
                                       "reason")
        else:
            for d in reversed(decisions):
                tag = "allowed" if d["allowed"] else "blocked"
                kind = d.get("kind", "web")
                label = d["domain"]
                if kind == "app":
                    label = f"{label} (app)"
                self.decisions_text.insert("end", "●  ", tag)
                self.decisions_text.insert("end", f"{label}\n", "domain")
                if not d["allowed"] and kind == "web" and remaining > 0:
                    # Blocked web domain + overrides still available → show link.
                    self.decisions_text.insert("end", f"    {d['reason']}  ", "reason")
                    link_tag = "override_" + "".join(
                        c if c.isalnum() else "_" for c in d["domain"]
                    )
                    self.decisions_text.tag_configure(
                        link_tag,
                        foreground=ACCENT,
                        font=("Segoe UI", 10),
                    )
                    self.decisions_text.insert("end", "↻ allow", link_tag)
                    self.decisions_text.tag_bind(
                        link_tag, "<Button-1>",
                        lambda e, dom=d["domain"]: self._override_domain(dom),
                    )
                    self.decisions_text.tag_bind(
                        link_tag, "<Enter>",
                        lambda e: self.decisions_text.config(cursor="hand2"),
                    )
                    self.decisions_text.tag_bind(
                        link_tag, "<Leave>",
                        lambda e: self.decisions_text.config(cursor="arrow"),
                    )
                    self.decisions_text.insert("end", "\n\n", "reason")
                else:
                    self.decisions_text.insert("end", f"    {d['reason']}\n\n", "reason")
        self.decisions_text.config(state="disabled")

    def _override_domain(self, domain):
        """Show a reason dialog; if confirmed, add domain to session allowlist."""
        used = proxy._override_count
        remaining = OVERRIDE_LIMIT - used
        if remaining <= 0:
            return

        W, H = 400, 240
        win = tk.Toplevel(self.root)
        win.title("Allow site?")
        win.geometry(f"{W}x{H}")
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        win.update_idletasks()
        sx = (win.winfo_screenwidth() - W) // 2
        sy = (win.winfo_screenheight() - H) // 2
        win.geometry(f"{W}x{H}+{sx}+{sy}")

        s = "s" if remaining != 1 else ""
        tk.Label(win, text=f"Allow {domain}?",
                 bg=BG, fg=TEXT_PRIMARY,
                 font=("Georgia", 15, "bold")).pack(pady=(26, 2))
        tk.Label(win, text=f"Uses 1 of your {remaining} remaining override{s}.",
                 bg=BG, fg=TEXT_TERTIARY,
                 font=("Segoe UI", 10)).pack(pady=(0, 18))

        reason_entry = tk.Entry(win, font=("Segoe UI", 12),
                                bg=SURFACE, fg=TEXT_PRIMARY,
                                insertbackground=TEXT_PRIMARY,
                                relief="flat", borderwidth=0,
                                justify="center", width=34)
        reason_entry.pack(ipady=10, padx=40, fill="x")
        tk.Label(win, text="why do you need it?",
                 bg=BG, fg=TEXT_TERTIARY,
                 font=("Segoe UI", 9, "italic")).pack(pady=(4, 0))

        btn_row = tk.Frame(win, bg=BG)
        btn_row.pack(pady=(20, 0))

        def confirm():
            if not reason_entry.get().strip():
                return
            win.destroy()
            self.decisions_text.config(cursor="arrow")
            proxy.override_domain(domain)

        tk.Button(btn_row, text="Cancel",
                  bg=BG, fg=TEXT_TERTIARY,
                  activebackground=BG, activeforeground=TEXT_PRIMARY,
                  relief="flat", borderwidth=0,
                  font=("Segoe UI", 11), cursor="hand2",
                  command=win.destroy).pack(side="left", padx=(0, 20))

        RoundedButton(btn_row, text="Allow →",
                      command=confirm,
                      pad_x=28, pad_y=10,
                      font=("Segoe UI", 11, "bold")).pack(side="left")

        reason_entry.bind("<Return>", lambda _: confirm())
        reason_entry.focus_set()

    def on_close(self):
        if self.recorder.active:
            try:
                self.recorder.stop()
            except Exception:
                pass
        if self.presence is not None:
            try:
                self.presence.stop()
            except Exception:
                pass
            self.presence = None
        self._cancel_restore_task()
        if state["active"]:
            proxy.end_sprint()
        safe_restore()
        self.root.destroy()


def main():
    load_user_env_from_registry()

    if "--restore" in sys.argv:
        restore_proxy()
        print("Proxy restored.")
        return

    from windows_proxy import BACKUP_FILE
    if os.path.exists(BACKUP_FILE):
        safe_restore()

    proxy.start()
    # Proxy reads this on every AI request so the current user token is sent
    # even though the proxy was created before sign-in.
    proxy._headers_getter = lambda: get_openai_config()[2]
    atexit.register(safe_restore)
    try:
        signal.signal(signal.SIGINT, lambda *_: (safe_restore(), sys.exit(0)))
        signal.signal(signal.SIGTERM, lambda *_: (safe_restore(), sys.exit(0)))
    except Exception:
        pass

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
