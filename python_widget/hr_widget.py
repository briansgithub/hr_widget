import asyncio
import threading
import tkinter as tk
import math
import time
import os
import sys
import signal
import ctypes
import json
import queue
import socket
import cProfile, pstats, atexit
import tempfile
import wave
from array import array
from pathlib import Path

try:
    import winsound
except ImportError:  # Allows timing tests to import this module off Windows.
    winsound = None

from bleak import BleakClient, BleakScanner
import discord
from discord.ext import tasks
from dotenv import load_dotenv
import pystray
from PIL import Image, ImageDraw
from tkinter import simpledialog, messagebox

from sync_link import SyncLink, load_or_create_token, save_token, ROLE_HOST, ROLE_MIRROR, ROLE_UNLINKED
from pulse_clock import BeatGate, PulseClock
from keyboard_sync import ClevoIpcBackend, KeyboardSyncController, KeyboardSyncState, StaticColor

# Load token from .env file
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ICON_PATH = r"H:\Desktop\widgets\@icons\ico_desktop\mobile_BPMsworth_semibot.ico"
HEARTBEAT_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
DEFAULT_HEARTBEAT_SOUND_PRESET = "Clear Stethoscope"
HEARTBEAT_SOUND_PRESETS = {
    "Natural Lub-Dub": "heartbeat_lub_dub.wav",
    "Deep Chest": "heartbeat_deep_chest.wav",
    "Soft Resting": "heartbeat_soft_resting.wav",
    DEFAULT_HEARTBEAT_SOUND_PRESET: "heartbeat_clear_stethoscope.wav",
    "Strong Pulse": "heartbeat_strong_pulse.wav",
}
# Retained as the canonical/default path for compatibility with existing
# packaging and tests; other presets are resolved from HEARTBEAT_ASSETS_DIR.
HEARTBEAT_SOUND_PATH = HEARTBEAT_ASSETS_DIR / HEARTBEAT_SOUND_PRESETS[DEFAULT_HEARTBEAT_SOUND_PRESET]
BPM_STALE_TIMEOUT_SECONDS = 180.0
KEYBOARD_BRIGHTNESS_MIN = 1
KEYBOARD_BRIGHTNESS_MAX = 4
KEYBOARD_BRIGHTNESS_UPDATE_SECONDS = 0.100
KEYBOARD_BRIGHTNESS_LATENCY_LEAD_SECONDS = 0.025

# Standard Bluetooth LE Heart Rate UUID
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

_AGENT_LOG_PATH = r"h:\Desktop\widgets\hr_widget\.cursor\debug-e0ee23.log"
_agent_log_lock = threading.Lock()
_agent_last_hr_monotonic = None
_agent_mirror_takeover = False


def _agent_log(location, message, data, hypothesis_id):
    payload = {
        "sessionId": "e0ee23",
        "runId": "pre-fix",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        line = json.dumps(payload, default=str) + "\n"
        with _agent_log_lock:
            with open(_AGENT_LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:
        pass

# --- PROFILING SETUP ---
profiler = cProfile.Profile()
_profiling_enabled = False

def setup_profiling():
    global _profiling_enabled
    _profiling_enabled = True
    print("--- PROFILING ENABLED ---")
    profiler.enable()
    atexit.register(stop_profiling)

def stop_profiling():
    if _profiling_enabled:
        profiler.disable()
        print("\n\n--- PROFILING STATS ---")
        stats = pstats.Stats(profiler).sort_stats('cumulative')
        stats.print_stats(30)
        print("--- END OF STATS ---")
# --- END PROFILING SETUP ---

def minimize_console():
    """Minimizes the console window running this script."""
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        SW_MINIMIZE = 6
        ctypes.windll.user32.ShowWindow(hwnd, SW_MINIMIZE)

# --- DISCORD BOT CLASS ---
class HeartRateBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_bpm = 0
        self.last_sent_bpm = -1

    async def setup_hook(self):
        self.update_nickname.start()

    @tasks.loop(seconds=15)
    async def update_nickname(self):
        """Updates the bot's per-server nickname to the current BPM every 15
        seconds (no status/activity text -- just the name).

        Skipped entirely while MIRRORing a peer's Fitbit over the sync link:
        both this widget and the phone app share one Discord bot token, and
        if both independently PATCH the same guild nickname from their own
        (possibly lagging) local bpm, they race and flicker. Only the sync
        link's HOST -- the device with the real, authoritative BLE connection
        -- writes to Discord; a MIRROR leaves it alone regardless of whether
        its own Discord toggle is on.
        """
        if sync_link_ref is not None and sync_link_ref.role == ROLE_MIRROR:
            return
        bpm = self.current_bpm
        if bpm == self.last_sent_bpm:
            return
        nickname = f"{bpm} BPM" if bpm > 0 else "BPMsworth"
        for guild in self.guilds:
            try:
                await guild.me.edit(nick=nickname)
            except Exception as e:
                print(f"Failed to update nickname in {guild.name}: {e}")
        self.last_sent_bpm = bpm

    @update_nickname.before_loop
    async def before_update(self):
        await self.wait_until_ready()
        try:
            await self.change_presence(status=discord.Status.online, activity=None)
        except Exception:
            pass

class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tipwindow = None
        self.id = None
        self.widget.bind("<Enter>", self.enter)
        self.widget.bind("<Leave>", self.leave)
        self.widget.bind("<ButtonPress>", self.leave)

    def enter(self, event=None):
        self.schedule()
        
    def leave(self, event=None):
        self.unschedule()
        self.hidetip()
        
    def schedule(self):
        self.unschedule()
        self.id = self.widget.after(500, self.showtip)
        
    def unschedule(self):
        id_ = self.id
        self.id = None
        if id_:
            self.widget.after_cancel(id_)
            
    def showtip(self, event=None):
        if self.tipwindow or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=self.text, justify=tk.LEFT,
                         background="#202225", fg="#F2F4F7", relief=tk.FLAT, borderwidth=0,
                         font=("Segoe UI", 9, "normal"), highlightbackground="#3A3E44", highlightthickness=1)
        label.pack(ipadx=6, ipady=3)
        
    def hidetip(self):
        tw = self.tipwindow
        self.tipwindow = None
        if tw:
            tw.destroy()

class HeartRateWidget:
    CONTROL_REVEAL_DELAY_SECONDS = 1.5
    CONTROL_HIDE_DELAY_SECONDS = 1.0
    CONTROL_MIN_WIDTH = 650
    CONTROL_MIN_HEIGHT = 400

    CONTROL_COLORS = {
        "backdrop": "#202225",
        "surface": "#2A2D31",
        "surface_hover": "#34383E",
        "border": "#3A3E44",
        "text": "#F2F4F7",
        "secondary": "#A7ADB5",
        "muted": "#747B85",
        "selected": "#DDE1E6",
        "selected_text": "#191B1E",
        "trough": "#111315",
        "exit": "#563235",
        "exit_hover": "#704046",
        "exit_text": "#F2D7D9",
    }

    def __init__(
        self,
        root,
        *,
        enable_experimental_clevo_ipc=False,
        keyboard_sync_default_on=False,
    ):
        self.root = root
        self.root.title("Live HR")
        self.root.attributes("-topmost", True)
        self.root.geometry("650x400+-180+-100")
        
        # --- ALLOW RESIZING ---
        self.root.resizable(True, True)
        self.root.minsize(self.CONTROL_MIN_WIDTH, self.CONTROL_MIN_HEIGHT)
        
        # --- TRANSPARENCY SETUP ---
        self.bg_color = "#000001" 
        self.root.configure(bg=self.bg_color)
        self.root.attributes("-transparentcolor", self.bg_color)
        
        # Start borderless
        self.root.overrideredirect(True)
        # Force taskbar icon after the window is mapped
        self.root.after(50, self._force_taskbar_icon)
        
        # The drawing canvas
        self.canvas = tk.Canvas(root, bg=self.bg_color, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        # --- STATE VARIABLES ---
        self.current_bpm = 0
        self.status_text = "Starting..."
        self.base_font_size = 32
        self.pulse_phase = 0.0
        pulse_now = time.perf_counter()
        self._pulse_clock = PulseClock(now=pulse_now)
        self._heartbeat_gate = BeatGate(max_lateness_seconds=0.100)
        self._heartbeat_job = None
        self._updating_heartbeat_sound_var = False
        self._heartbeat_temp_dir = None
        self._heartbeat_scaled_paths = {}
        self.start_time = time.time()
        self.last_received_bpm = 0
        self.last_bpm_received_time = time.time()
        self.last_bpm_received_monotonic = pulse_now
        
        # Hover state logic
        self.controls_visible = False
        self.last_hover_time = time.time()
        
        self._updating_sync_var = False
        self._updating_keyboard_sync_var = False
        self._keyboard_sync_opt_in = bool(enable_experimental_clevo_ipc)
        self._keyboard_sync_auto_start_pending = bool(
            self._keyboard_sync_opt_in and keyboard_sync_default_on
        )
        self._keyboard_sync_desired = False
        self._keyboard_sync_generation = 0
        self._keyboard_sync_future = None
        self._keyboard_color_request_future = None
        self._keyboard_brightness_request_future = None
        self._last_requested_keyboard_color = None
        self._last_requested_keyboard_brightness = None
        self._last_keyboard_brightness_request_time = float("-inf")
        self._keyboard_sync_completions = queue.SimpleQueue()
        self._keyboard_sync_poll_job = None
        self._screen_off_enabled = False
        self._hibernate_enabled = False
        
        # --- HOVER CONTROLS ---
        self.pulse_var = tk.BooleanVar(value=True)
        self.pulse_var.trace_add("write", self._on_pulse_toggle)
        self.checkbox = self._create_toggle(self.pulse_var, "Pulse animation")
        ToolTip(self.checkbox, "Toggle visual pulse animation")

        self.heartbeat_sound_var = tk.BooleanVar(value=True)
        self.heartbeat_sound_var.trace_add("write", self._on_heartbeat_sound_toggle)
        self.heartbeat_sound_checkbox = self._create_toggle(
            self.heartbeat_sound_var,
            "Heartbeat sound",
        )
        ToolTip(self.heartbeat_sound_checkbox, "Play a BPM-synced heartbeat through system media volume")

        self.heartbeat_sound_preset_var = tk.StringVar(value=DEFAULT_HEARTBEAT_SOUND_PRESET)
        self.heartbeat_sound_preset_var.trace_add(
            "write",
            self._on_heartbeat_sound_preset_change,
        )
        self.heartbeat_sound_preset_label = tk.Label(
            self.root,
            text="Sound effect",
            bg=self.CONTROL_COLORS["surface"],
            fg=self.CONTROL_COLORS["muted"],
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.heartbeat_sound_preset_dropdown = self._create_dropdown(
            self.heartbeat_sound_preset_var,
            tuple(HEARTBEAT_SOUND_PRESETS),
        )
        ToolTip(
            self.heartbeat_sound_preset_dropdown,
            "Choose the heartbeat sound played at each pulse crest",
        )

        self.topmost_var = tk.BooleanVar(value=True)
        self.topmost_var.trace_add("write", self._on_topmost_toggle)
        self.topmost_checkbox = self._create_toggle(self.topmost_var, "Always on top")
        ToolTip(self.topmost_checkbox, "Keep the widget above other windows")

        self.hibernate_var = tk.BooleanVar(value=False)
        self.hibernate_var.trace_add("write", self._on_hibernate_toggle)
        self.hibernate_checkbox = self._create_toggle(self.hibernate_var, "Hibernate mode")
        ToolTip(self.hibernate_checkbox, "If toggled on, hibernation is used instead of screen off")

        self.screen_off_var = tk.BooleanVar(value=True)
        self.screen_off_var.trace_add("write", self._on_screen_off_toggle)
        self.screen_off_checkbox = self._create_toggle(self.screen_off_var, "Screen off below 65")
        ToolTip(self.screen_off_checkbox, "Enable/disable actions when BPM drops below 65 for 1 minute")

        self.discord_var = tk.BooleanVar(value=False)
        self.discord_var.trace_add("write", self._on_discord_toggle)
        self.discord_checkbox = self._create_toggle(self.discord_var, "Discord bot")
        ToolTip(self.discord_checkbox, "Share your heart rate to Discord")

        self.sync_link_var = tk.BooleanVar(value=False)
        self.sync_link_var.trace_add("write", self._on_sync_link_toggle)
        self.sync_link_checkbox = self._create_toggle(self.sync_link_var, "Link device")
        ToolTip(self.sync_link_checkbox, "Link with the phone app to share this Fitbit's heart rate over your local network")

        self.pair_code_button = self._create_action_button(
            "Pair code",
            self._on_pair_code_click,
        )
        ToolTip(self.pair_code_button, "View or enter the pairing code shared with the phone app")

        self.keyboard_sync_var = tk.BooleanVar(value=False)
        self.keyboard_sync_var.trace_add("write", self._on_keyboard_sync_toggle)
        self.keyboard_sync_checkbox = self._create_toggle(
            self.keyboard_sync_var,
            "Keyboard sync",
        )
        if self._keyboard_sync_opt_in:
            keyboard_sync_tip = (
                "Heart-rate color and pulse-brightness sync. Activation remains blocked unless "
                "a qualified, reversible OEM lighting-only IPC is available."
            )
        else:
            keyboard_sync_tip = (
                "Keyboard sync was disabled for this session. Relaunch without "
                "--disable-experimental-clevo-ipc to make it available."
            )
        # The background event loop publishes a readiness handshake only after
        # its shutdown event exists. No keyboard work is accepted before then.
        self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")
        ToolTip(self.keyboard_sync_checkbox, keyboard_sync_tip)

        # Construction stays inert. Default-on activation occurs only after the
        # background loop is ready and the signed broker completes its safety probe.
        self.keyboard_sync = KeyboardSyncController(
            ClevoIpcBackend(enable_real_broker=self._keyboard_sync_opt_in)
        )

        self.new_threshold_var = tk.BooleanVar(value=True)
        self.new_threshold_var.trace_add("write", self._on_threshold_toggle)
        self.threshold_checkbox = self._create_toggle(self.new_threshold_var, "Sitting thresholds")
        ToolTip(self.threshold_checkbox, "Toggle between new and old color thresholds")

        self.heartbeat_volume_var = tk.IntVar(value=100)

        self.heartbeat_volume_var.trace_add("write", self._on_heartbeat_volume_change)

        self.heartbeat_volume_scale = self._create_scale(
            self.heartbeat_volume_var,
            "Heartbeat volume %",
            0,
            100,
        )

        self._updating_brightness_sliders = False
        self.min_brightness_var = tk.IntVar(value=KEYBOARD_BRIGHTNESS_MIN)
        self.max_brightness_var = tk.IntVar(value=KEYBOARD_BRIGHTNESS_MAX)
        self.min_brightness_var.trace_add("write", self._on_min_brightness_change)
        self.max_brightness_var.trace_add("write", self._on_max_brightness_change)
        self.min_brightness_scale = self._create_scale(
            self.min_brightness_var,
            "Minimum brightness",
            0,
            4,
        )
        self.max_brightness_scale = self._create_scale(
            self.max_brightness_var,
            "Maximum brightness",
            0,
            4,
        )

        self.exit_button = self._create_action_button(
            "× Exit",
            shutdown_bot,
            destructive=True,
        )
        ToolTip(self.exit_button, "Exit the application safely")

        self._hover_controls = (
            self.checkbox,
            self.heartbeat_sound_checkbox,
            self.topmost_checkbox,
            self.screen_off_checkbox,
            self.hibernate_checkbox,
            self.threshold_checkbox,
            self.discord_checkbox,
            self.sync_link_checkbox,
            self.pair_code_button,
            self.keyboard_sync_checkbox,
            self.heartbeat_sound_preset_label,
            self.heartbeat_sound_preset_dropdown,
            self.min_brightness_scale,
            self.max_brightness_scale,
            self.heartbeat_volume_scale,
            self.exit_button,
        )
        
        # --- DRAG HANDLE (visible box for moving the window) ---
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._dragging = False
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        
        # Track when cursor left the window for delayed hide
        self._cursor_left_time = None
        self._cursor_entered_time = None
        
        self.canvas.bind("<Enter>", self.on_enter)
        self.canvas.bind("<Leave>", self.on_leave)
        self.canvas.bind("<Motion>", self.on_motion)
        
        # --- EVENT CACHING ---
        self._cached_window_size = (self.CONTROL_MIN_WIDTH, self.CONTROL_MIN_HEIGHT)
        self.root.bind("<Configure>", self._on_configure)
        self._last_hover_poll = 0

        # --- PERSISTENT CANVAS ITEMS (created once, updated in-place via itemconfigure) ---
        hover_tags = ("hover_decor",)
        self._bg_rect = self.canvas.create_polygon(
            *self._rounded_rect_points(8, 8, 592, 342, 16),
            fill=self.CONTROL_COLORS["backdrop"],
            outline=self.CONTROL_COLORS["border"],
            width=2,
            smooth=True,
            splinesteps=24,
            state="hidden",
            tags=hover_tags,
        )
        self._left_panel = self.canvas.create_polygon(
            *self._rounded_rect_points(14, 44, 206, 278, 10),
            fill=self.CONTROL_COLORS["surface"],
            outline=self.CONTROL_COLORS["border"],
            width=1,
            smooth=True,
            splinesteps=20,
            state="hidden",
            tags=hover_tags,
        )
        self._right_panel = self.canvas.create_polygon(
            *self._rounded_rect_points(394, 44, 586, 278, 10),
            fill=self.CONTROL_COLORS["surface"],
            outline=self.CONTROL_COLORS["border"],
            width=1,
            smooth=True,
            splinesteps=20,
            state="hidden",
            tags=hover_tags,
        )
        self._levels_panel = self.canvas.create_polygon(
            *self._rounded_rect_points(14, 284, 586, 342, 10),
            fill=self.CONTROL_COLORS["surface"],
            outline=self.CONTROL_COLORS["border"],
            width=1,
            smooth=True,
            splinesteps=20,
            state="hidden",
            tags=hover_tags,
        )
        self._header_divider = self.canvas.create_line(
            18,
            38,
            582,
            38,
            fill=self.CONTROL_COLORS["border"],
            width=1,
            state="hidden",
            tags=hover_tags,
        )
        self._left_divider = self.canvas.create_line(
            22,
            130,
            198,
            130,
            fill=self.CONTROL_COLORS["border"],
            width=1,
            state="hidden",
            tags=hover_tags,
        )
        self._right_divider = self.canvas.create_line(
            402,
            160,
            578,
            160,
            fill=self.CONTROL_COLORS["border"],
            width=1,
            state="hidden",
            tags=hover_tags,
        )
        group_text = {
            "font": ("Segoe UI", 8, "bold"),
            "fill": self.CONTROL_COLORS["secondary"],
            "anchor": "nw",
            "state": "hidden",
            "tags": hover_tags,
        }
        self._experience_title = self.canvas.create_text(22, 50, text="EXPERIENCE", **group_text)
        self._system_title = self.canvas.create_text(22, 138, text="SYSTEM", **group_text)
        self._connections_title = self.canvas.create_text(402, 50, text="CONNECTIONS", **group_text)
        self._hardware_title = self.canvas.create_text(402, 168, text="HARDWARE", **group_text)
        self._drag_text = self.canvas.create_text(
            300,
            23,
            text="···  DRAG EMPTY SPACE TO MOVE  ···",
            font=("Segoe UI", 9),
            fill=self.CONTROL_COLORS["secondary"],
            state="hidden",
            tags=hover_tags,
        )
        self._bpm_outlines = [self.canvas.create_text(0, 0, text="", font=("Helvetica", 32, "bold"), fill="black") for _ in range(4)]
        self._bpm_text = self.canvas.create_text(0, 0, text="", font=("Helvetica", 32, "bold"), fill="white")
        self._time_outlines = [self.canvas.create_text(0, 0, text="", font=("Helvetica", 14, "bold"), fill="black") for _ in range(4)]
        self._time_text = self.canvas.create_text(0, 0, text="", font=("Helvetica", 14, "bold"), fill="white")
        self._uptime_outlines = [self.canvas.create_text(0, 0, text="", font=("Helvetica", 14, "bold"), fill="black") for _ in range(4)]
        self._uptime_text = self.canvas.create_text(0, 0, text="", font=("Helvetica", 14, "bold"), fill="white")
        self._idle_hover_items = (
            self._bpm_text,
            *self._bpm_outlines,
            self._time_text,
            *self._time_outlines,
            self._uptime_text,
            *self._uptime_outlines,
        )

        # --- CHANGE DETECTION CACHE ---
        self._cached_time_str = ""
        self._last_drawn_bpm = -1
        self._last_drawn_status = ""
        self._last_controls_visible = None
        self._last_uptime_minutes = -1
        self._cached_color_bpm = -1
        self._cached_color = "white"
        self._last_topmost_enforce = 0
        self._last_window_size = (0, 0)
        self._last_drawn_size = -1
        self._animate_job = None

        # Ensure styling matches initial toggle states
        self._on_pulse_toggle()
        self._on_heartbeat_sound_toggle()
        self._on_topmost_toggle()
        self._style_toggle(self.sync_link_checkbox, False, "Link device")
        self._on_discord_toggle()
        self._on_hibernate_toggle()
        self._on_screen_off_toggle()
        self._style_toggle(
            self.keyboard_sync_checkbox,
            False,
            "Keyboard sync (experimental)",
        )
        self._on_threshold_toggle()
        self._keyboard_sync_poll_job = self.root.after(
            50,
            self._drain_keyboard_sync_completions,
        )

        self.animate()

    @staticmethod
    def _rounded_rect_points(x1, y1, x2, y2, radius):
        radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
        return (
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        )

    def _create_toggle(self, variable, label):
        colors = self.CONTROL_COLORS
        return tk.Checkbutton(
            self.root,
            text=f"○ {label}",
            variable=variable,
            indicatoron=False,
            bg=colors["surface"],
            fg=colors["secondary"],
            selectcolor=colors["surface"],
            activebackground=colors["surface_hover"],
            activeforeground=colors["text"],
            disabledforeground=colors["muted"],
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            padx=8,
            pady=2,
            cursor="hand2",
            bd=0,
            relief="flat",
            overrelief="flat",
            highlightthickness=1,
            highlightbackground=colors["border"],
            highlightcolor=colors["text"],
            takefocus=True,
        )

    def _create_action_button(self, label, command, destructive=False):
        colors = self.CONTROL_COLORS
        if destructive:
            background = colors["exit"]
            foreground = colors["exit_text"]
            active_background = colors["exit_hover"]
        else:
            background = colors["surface"]
            foreground = colors["secondary"]
            active_background = colors["surface_hover"]

        return tk.Button(
            self.root,
            text=label,
            command=command,
            bg=background,
            fg=foreground,
            activebackground=active_background,
            activeforeground=colors["text"],
            disabledforeground=colors["muted"],
            font=("Segoe UI", 9),
            padx=8,
            pady=2,
            cursor="hand2",
            bd=0,
            relief="flat",
            overrelief="flat",
            highlightthickness=1,
            highlightbackground=colors["border"],
            highlightcolor=colors["text"],
            takefocus=True,
        )

    def _create_scale(self, variable, label, minimum, maximum):
        colors = self.CONTROL_COLORS
        return tk.Scale(
            self.root,
            from_=minimum,
            to=maximum,
            orient=tk.HORIZONTAL,
            variable=variable,
            label=label,
            bg=colors["surface"],
            fg=colors["secondary"],
            activebackground=colors["selected"],
            troughcolor=colors["trough"],
            highlightthickness=0,
            bd=0,
            font=("Segoe UI", 8),
            width=6,
            sliderlength=14,
            sliderrelief="flat",
            showvalue=True,
            resolution=1,
            cursor="hand2",
            takefocus=True,
        )

    def _create_dropdown(self, variable, options):
        colors = self.CONTROL_COLORS
        dropdown = tk.OptionMenu(self.root, variable, *options)
        dropdown.config(
            bg=colors["surface"],
            fg=colors["secondary"],
            activebackground=colors["surface_hover"],
            activeforeground=colors["text"],
            disabledforeground=colors["muted"],
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            padx=8,
            pady=2,
            cursor="hand2",
            bd=0,
            relief="flat",
            highlightthickness=1,
            highlightbackground=colors["border"],
            highlightcolor=colors["text"],
            takefocus=True,
        )
        dropdown["menu"].config(
            bg=colors["surface"],
            fg=colors["secondary"],
            activebackground=colors["selected"],
            activeforeground=colors["selected_text"],
            font=("Segoe UI", 9),
            bd=0,
        )
        return dropdown

    def _hover_control_placements(self, width, height):
        left_x = 22
        right_x = width - 198
        control_width = 176
        control_height = 25

        levels_x = 22
        levels_gap = 6
        levels_inner_width = width - 44
        scale_width = (levels_inner_width - (2 * levels_gap)) // 3
        # Keep the sliders above the dedicated exit-button row.
        scale_y = height - 101

        placements = [
            (self.checkbox, {"x": left_x, "y": 67, "width": control_width, "height": control_height}),
            (self.heartbeat_sound_checkbox, {"x": left_x, "y": 97, "width": control_width, "height": control_height}),
            (self.topmost_checkbox, {"x": left_x, "y": 155, "width": control_width, "height": control_height}),
            (self.screen_off_checkbox, {"x": left_x, "y": 183, "width": control_width, "height": control_height}),
            (self.hibernate_checkbox, {"x": left_x, "y": 211, "width": control_width, "height": control_height}),
            (self.threshold_checkbox, {"x": left_x, "y": 239, "width": control_width, "height": control_height}),
            (self.discord_checkbox, {"x": right_x, "y": 67, "width": control_width, "height": control_height}),
            (self.sync_link_checkbox, {"x": right_x, "y": 97, "width": control_width, "height": control_height}),
            (self.pair_code_button, {"x": right_x, "y": 127, "width": control_width, "height": control_height}),
            (self.keyboard_sync_checkbox, {"x": right_x, "y": 185, "width": control_width, "height": control_height}),
        ]

        preset_label = getattr(self, "heartbeat_sound_preset_label", None)
        preset_dropdown = getattr(self, "heartbeat_sound_preset_dropdown", None)
        if preset_label is not None and preset_dropdown is not None:
            placements.extend((
                (preset_label, {"x": right_x, "y": 215, "width": control_width, "height": 16}),
                (preset_dropdown, {"x": right_x, "y": 233, "width": control_width, "height": 27}),
            ))

        placements.extend((
            (
                self.min_brightness_scale,
                {
                    "x": levels_x,
                    "y": scale_y,
                    "width": scale_width,
                    "height": 54,
                },
            ),
            (
                self.max_brightness_scale,
                {
                    "x": levels_x + scale_width + levels_gap,
                    "y": scale_y,
                    "width": scale_width,
                    "height": 54,
                },
            ),
            (
                self.heartbeat_volume_scale,
                {
                    "x": levels_x + (2 * (scale_width + levels_gap)),
                    "y": scale_y,
                    "width": scale_width,
                    "height": 54,
                },
            ),
            (self.exit_button, {"x": width - 76, "y": height - 40, "width": 60, "height": 24}),
        ))
        return tuple(placements)

    def _layout_hover_panel(self, width=None, height=None):
        if width is None or height is None:
            width, height = self._cached_window_size

        width = max(self.CONTROL_MIN_WIDTH, int(width))
        height = max(self.CONTROL_MIN_HEIGHT, int(height))
        right_panel_x = width - 206
        panel_bottom = height - 110
        levels_top = height - 104

        self.canvas.coords(
            self._bg_rect,
            *self._rounded_rect_points(8, 8, width - 8, height - 8, 16),
        )
        self.canvas.coords(
            self._left_panel,
            *self._rounded_rect_points(14, 44, 206, panel_bottom, 10),
        )
        self.canvas.coords(
            self._right_panel,
            *self._rounded_rect_points(right_panel_x, 44, width - 14, panel_bottom, 10),
        )
        self.canvas.coords(
            self._levels_panel,
            *self._rounded_rect_points(14, levels_top, width - 14, height - 8, 10),
        )
        self.canvas.coords(self._header_divider, 18, 38, width - 18, 38)
        self.canvas.coords(self._left_divider, 22, 130, 198, 130)
        self.canvas.coords(self._right_divider, width - 198, 160, width - 22, 160)
        self.canvas.coords(self._experience_title, 22, 50)
        self.canvas.coords(self._system_title, 22, 138)
        self.canvas.coords(self._connections_title, width - 198, 50)
        self.canvas.coords(self._hardware_title, width - 198, 168)
        self.canvas.coords(self._drag_text, width // 2, 23)

        placements = self._hover_control_placements(width, height)
        self._current_hover_placements = placements
        if self.controls_visible:
            for control, options in placements:
                control.place(**options)

    def _set_controls_visible(self, visible):
        visible = bool(visible)
        self.controls_visible = visible

        if visible:
            self._layout_hover_panel()
            self.canvas.itemconfigure("hover_decor", state="normal")
            self.canvas.config(cursor="fleur")
        else:
            for control in self._hover_controls:
                control.place_forget()
            self.canvas.itemconfigure("hover_decor", state="hidden")
            self.canvas.config(cursor="")

    def _show_controls(self):
        self._set_controls_visible(True)

    def _hide_controls(self):
        self._set_controls_visible(False)

    def _point_over_idle_text(self, x, y):
        if x is None or y is None:
            return False

        for item in self._idle_hover_items:
            bounds = self.canvas.bbox(item)
            if bounds and bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]:
                return True
        return False

    def _update_control_visibility(self, current_time, cursor_over_activation_target):
        if self.controls_visible:
            if cursor_over_activation_target:
                self._cursor_left_time = None
                return

            if self._cursor_left_time is None:
                self._cursor_left_time = current_time
            elif current_time - self._cursor_left_time >= self.CONTROL_HIDE_DELAY_SECONDS:
                self._hide_controls()
                self._cursor_left_time = None
            return

        if not cursor_over_activation_target:
            self._cursor_entered_time = None
            return

        if self._cursor_entered_time is None:
            self._cursor_entered_time = current_time
        elif current_time - self._cursor_entered_time >= self.CONTROL_REVEAL_DELAY_SECONDS:
            self._show_controls()
            self._cursor_entered_time = None

    def _force_taskbar_icon(self):
        """Force the borderless window to appear on the taskbar and disable minimize."""
        GWL_EXSTYLE = -20
        GWL_STYLE = -16
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080
        WS_MINIMIZEBOX = 0x00020000
        SWP_FRAMECHANGED = 0x0020
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        # Force taskbar icon
        ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ex_style = (ex_style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)
        # Remove minimize button
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
        style = style & ~WS_MINIMIZEBOX
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style)
        # Apply style changes without withdraw/deiconify (avoids popping back up)
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
        )

    def _style_toggle(self, button, is_on, label):
        """Update a toggle button's appearance based on its on/off state."""
        colors = self.CONTROL_COLORS
        if is_on:
            button.config(
                text=f"● {label}",
                bg=colors["selected"],
                fg=colors["selected_text"],
                selectcolor=colors["selected"],
                activebackground=colors["selected"],
                activeforeground=colors["selected_text"],
            )
        else:
            button.config(
                text=f"○ {label}",
                bg=colors["surface"],
                fg=colors["secondary"],
                selectcolor=colors["surface"],
                activebackground=colors["surface_hover"],
                activeforeground=colors["text"],
            )

    def _on_pulse_toggle(self, *args):
        self._style_toggle(self.checkbox, self.pulse_var.get(), "Pulse animation")

    def _on_heartbeat_sound_toggle(self, *args):
        is_on = self.heartbeat_sound_var.get()
        self._style_toggle(self.heartbeat_sound_checkbox, is_on, "Heartbeat sound")
        if self._updating_heartbeat_sound_var:
            return

        if not is_on:
            self._halt_heartbeat_audio()
            return

        if winsound is None:
            self._heartbeat_audio_failure("winsound is unavailable on this platform")
            return
        try:
            sound_path = self._selected_heartbeat_sound_path()
        except Exception as exc:
            self._heartbeat_audio_failure(str(exc))
            return
        if not sound_path.is_file():
            self._heartbeat_audio_failure(f"sound asset is missing: {sound_path}")
            return

        now = time.perf_counter()
        if self.current_bpm > 0 and not self._bpm_data_is_stale(now):
            self._heartbeat_gate.arm(self._pulse_clock, now)
            self._schedule_heartbeat_tick(now)

    def _on_heartbeat_sound_preset_change(self, *args):
        # Preserve the current clock, beat gate, and scheduled callback. Only
        # stop an in-flight waveform; the selected sound begins at the next
        # crest that was already scheduled.
        self._stop_heartbeat_playback()
        if not self.heartbeat_sound_var.get():
            return

        try:
            sound_path = self._selected_heartbeat_sound_path()
        except Exception as exc:
            self._heartbeat_audio_failure(str(exc))
            return
        if not sound_path.is_file():
            self._heartbeat_audio_failure(f"sound asset is missing: {sound_path}")

    def _on_heartbeat_volume_change(self, *args):
        # winsound exposes no per-play gain. Stop the current async waveform so
        # a slider change takes effect cleanly on the next scheduled crest.
        self._stop_heartbeat_playback()

    def _heartbeat_volume_percent(self):
        volume_var = getattr(self, "heartbeat_volume_var", None)
        if volume_var is None:
            return 100
        return max(0, min(100, int(volume_var.get())))

    def _heartbeat_sound_preset(self):
        preset_var = getattr(self, "heartbeat_sound_preset_var", None)
        preset = DEFAULT_HEARTBEAT_SOUND_PRESET if preset_var is None else str(preset_var.get())
        if preset not in HEARTBEAT_SOUND_PRESETS:
            raise ValueError(f"unknown heartbeat sound preset: {preset}")
        return preset

    def _selected_heartbeat_sound_path(self):
        preset = self._heartbeat_sound_preset()
        if preset == DEFAULT_HEARTBEAT_SOUND_PRESET:
            return HEARTBEAT_SOUND_PATH
        return HEARTBEAT_ASSETS_DIR / HEARTBEAT_SOUND_PRESETS[preset]

    def _heartbeat_playback_path(self):
        preset = self._heartbeat_sound_preset()
        source = self._selected_heartbeat_sound_path()
        if not source.is_file():
            raise FileNotFoundError(f"sound asset is missing: {source}")

        volume = self._heartbeat_volume_percent()
        if volume == 0:
            return None
        if volume == 100:
            return source

        cache_key = (preset, volume)
        cached = self._heartbeat_scaled_paths.get(cache_key)
        if cached is not None and cached.is_file():
            return cached

        if self._heartbeat_temp_dir is None:
            self._heartbeat_temp_dir = tempfile.TemporaryDirectory(
                prefix=f"hr_widget_heartbeat_{os.getpid()}_"
            )

        target = Path(self._heartbeat_temp_dir.name) / f"{source.stem}_{volume:03d}.wav"
        try:
            self._write_scaled_heartbeat_wav(source, target, volume)
        except Exception:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        self._heartbeat_scaled_paths[cache_key] = target
        return target

    @staticmethod
    def _write_scaled_heartbeat_wav(source, target, volume_percent):
        """Write a gain-scaled copy of an uncompressed 16-bit PCM WAV."""
        with wave.open(str(source), "rb") as reader:
            if reader.getsampwidth() != 2 or reader.getcomptype() != "NONE":
                raise ValueError("heartbeat asset must be uncompressed 16-bit PCM")
            params = reader.getparams()
            frames = reader.readframes(reader.getnframes())

        if len(frames) % 2:
            raise ValueError("heartbeat PCM data has an incomplete 16-bit sample")
        samples = array("h")
        samples.frombytes(frames)
        if sys.byteorder != "little":
            samples.byteswap()

        gain = max(0, min(100, int(volume_percent))) / 100.0
        for index, sample in enumerate(samples):
            samples[index] = max(-32768, min(32767, round(sample * gain)))

        if sys.byteorder != "little":
            samples.byteswap()
        with wave.open(str(target), "wb") as writer:
            writer.setparams(params)
            writer.writeframes(samples.tobytes())

    def _cleanup_heartbeat_temp_audio(self):
        self._heartbeat_scaled_paths.clear()
        temp_dir = self._heartbeat_temp_dir
        self._heartbeat_temp_dir = None
        if temp_dir is not None:
            try:
                temp_dir.cleanup()
            except Exception as exc:
                print(f"[heartbeat sound] temporary audio cleanup failed: {exc}")

    def _heartbeat_audio_failure(self, message):
        print(f"[heartbeat sound] {message}; disabling heartbeat sound")
        self._updating_heartbeat_sound_var = True
        try:
            self.heartbeat_sound_var.set(False)
            self._style_toggle(self.heartbeat_sound_checkbox, False, "Heartbeat sound")
        finally:
            self._updating_heartbeat_sound_var = False
        self._halt_heartbeat_audio()
        self._cleanup_heartbeat_temp_audio()

    def _cancel_heartbeat_job(self):
        job = self._heartbeat_job
        self._heartbeat_job = None
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass

    def _stop_heartbeat_playback(self):
        if winsound is None:
            return
        try:
            # A null sound stops a waveform started with SND_ASYNC immediately.
            winsound.PlaySound(None, 0)
        except Exception:
            pass

    def _halt_heartbeat_audio(self):
        self._cancel_heartbeat_job()
        self._heartbeat_gate.disarm()
        self._stop_heartbeat_playback()

    def shutdown_heartbeat_audio(self):
        """Stop scheduled/current audio without persisting the toggle state."""
        self._halt_heartbeat_audio()
        self._cleanup_heartbeat_temp_audio()

    def _play_heartbeat_sound(self):
        try:
            playback_path = self._heartbeat_playback_path()
        except Exception as exc:
            self._heartbeat_audio_failure(
                f"sound preparation failed ({type(exc).__name__}: {exc})"
            )
            return False
        if playback_path is None:
            return True

        try:
            winsound.PlaySound(
                str(playback_path),
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
            return True
        except Exception as exc:
            self._heartbeat_audio_failure(f"playback failed ({type(exc).__name__}: {exc})")
            return False

    def _bpm_data_is_stale(self, monotonic_now=None):
        if self.last_received_bpm <= 0:
            return False
        if monotonic_now is None:
            monotonic_now = time.perf_counter()
        last_monotonic = getattr(self, "last_bpm_received_monotonic", None)
        if last_monotonic is not None:
            return monotonic_now - last_monotonic > BPM_STALE_TIMEOUT_SECONDS
        return time.time() - self.last_bpm_received_time > BPM_STALE_TIMEOUT_SECONDS

    def _expire_stale_bpm(self, monotonic_now=None):
        if not self._bpm_data_is_stale(monotonic_now):
            return False

        # #region agent log
        now_m = time.perf_counter() if monotonic_now is None else monotonic_now
        last_m = getattr(self, "last_bpm_received_monotonic", None)
        _agent_log(
            "hr_widget.py:_expire_stale_bpm",
            "stale bpm expired",
            {
                "bpm": self.current_bpm,
                "age_s": None if last_m is None else round(now_m - last_m, 1),
                "status": self.status_text,
                "role": sync_link_ref.role if sync_link_ref else None,
            },
            "C",
        )
        # #endregion
        self.current_bpm = 0
        self.last_received_bpm = 0
        self.status_text = "-- BPM"
        now = time.perf_counter() if monotonic_now is None else monotonic_now
        self.pulse_phase = self._pulse_clock.update(0, now)
        self._halt_heartbeat_audio()

        global _bot_ref
        if _bot_ref:
            _bot_ref.current_bpm = 0
        return True

    def _schedule_heartbeat_tick(self, monotonic_now=None):
        self._cancel_heartbeat_job()
        if (
            _shutdown_initiated
            or not self.heartbeat_sound_var.get()
            or self.current_bpm <= 0
            or not self._heartbeat_gate.armed
        ):
            return

        if monotonic_now is None:
            monotonic_now = time.perf_counter()
        delay_seconds = self._heartbeat_gate.seconds_until_due(monotonic_now)
        if delay_seconds is None:
            return
        delay_ms = max(1, math.ceil(delay_seconds * 1000.0))
        self._heartbeat_job = self.root.after(delay_ms, self._heartbeat_tick)

    def _heartbeat_tick(self):
        self._heartbeat_job = None
        if _shutdown_initiated or not self.heartbeat_sound_var.get():
            self._halt_heartbeat_audio()
            return

        now = time.perf_counter()
        if self._expire_stale_bpm(now) or self.current_bpm <= 0:
            self._halt_heartbeat_audio()
            return

        if self._heartbeat_gate.poll(self._pulse_clock, now):
            if not self._play_heartbeat_sound():
                return
        self._schedule_heartbeat_tick(now)

    def current_pulse_phase(self, monotonic_now=None):
        if monotonic_now is None:
            monotonic_now = time.perf_counter()
        clock = getattr(self, "_pulse_clock", None)
        if clock is None:
            clock = PulseClock(
                bpm=getattr(self, "current_bpm", 0),
                phase=getattr(self, "pulse_phase", 0.0),
                now=monotonic_now,
            )
            self._pulse_clock = clock
        self.pulse_phase = clock.phase_at(monotonic_now)
        return self.pulse_phase

    def _on_topmost_toggle(self, *args):
        self.root.attributes("-topmost", self.topmost_var.get())
        self._style_toggle(self.topmost_checkbox, self.topmost_var.get(), "Always on top")

    def _on_screen_off_toggle(self, *args):
        self._screen_off_enabled = bool(self.screen_off_var.get())
        label = "Hibernate below 65" if hasattr(self, 'hibernate_var') and self.hibernate_var.get() else "Screen off below 65"
        self._style_toggle(self.screen_off_checkbox, self._screen_off_enabled, label)

    def _on_hibernate_toggle(self, *args):
        is_on = self.hibernate_var.get()
        self._hibernate_enabled = bool(is_on)
        self._style_toggle(self.hibernate_checkbox, is_on, "Hibernate mode")
        if hasattr(self, 'screen_off_checkbox'):
            label = "Hibernate below 65" if is_on else "Screen off below 65"
            self._style_toggle(self.screen_off_checkbox, self.screen_off_var.get(), label)

    def _on_discord_toggle(self, *args):
        is_on = self.discord_var.get()
        if sync_link_ref and _loop_ref and not getattr(self, "_syncing_discord", False):
            _loop_ref.call_soon_threadsafe(sync_link_ref.set_discord_enabled, is_on)
        self._style_toggle(self.discord_checkbox, is_on, "Discord bot")
        if is_on and not (sync_link_ref and sync_link_ref.role == ROLE_MIRROR):
            if _loop_ref:
                asyncio.run_coroutine_threadsafe(start_discord_bot(), _loop_ref)
        else:
            if _loop_ref:
                asyncio.run_coroutine_threadsafe(_close_discord(), _loop_ref)

    def _on_sync_link_toggle(self, *args):
        if getattr(self, '_updating_sync_var', False): return
        is_on = self.sync_link_var.get()
        if is_on:
            if sync_link_ref and _loop_ref:
                asyncio.run_coroutine_threadsafe(sync_link_ref.request_link(), _loop_ref)
        else:
            if sync_link_ref and _loop_ref:
                asyncio.run_coroutine_threadsafe(sync_link_ref.disconnect(), _loop_ref)
            self._style_toggle(self.sync_link_checkbox, False, "Link device")

    def _on_pair_code_click(self):
        current = load_or_create_token()
        new_value = simpledialog.askstring(
            "Pairing Code",
            "This code must match on both devices.\n"
            "Share it with the phone app, or paste the phone's code here to match it:",
            initialvalue=current,
            parent=self.root,
        )
        if new_value and new_value.strip() and new_value.strip() != current:
            updated = save_token(new_value)
            if sync_link_ref:
                sync_link_ref.token = updated
            messagebox.showinfo("Pairing Code", f"Pairing code set to: {updated}", parent=self.root)

    def on_sync_role_change(self, role, peer_name):
        # #region agent log
        _agent_log(
            "hr_widget.py:on_sync_role_change",
            "sync role changed",
            {
                "role": role,
                "has_peer": bool(peer_name),
                "bpm": self.current_bpm,
                "local_fitbit": bool(sync_link_ref and sync_link_ref._fitbit_connected),
                "peer_fitbit": bool(sync_link_ref and sync_link_ref._peer_fitbit_connected),
            },
            "B",
        )
        # #endregion
        # A stale last_sent_bpm cache could make update_nickname() wrongly
        # believe Discord already shows the right value after regaining HOST
        # (if the current bpm happens to match whatever we last wrote before
        # we started MIRRORing). Force the next write unconditionally.
        if _bot_ref:
            _bot_ref.last_sent_bpm = -1
        # The setting is shared, but only the host owns the Discord session.
        self._syncing_discord = True
        try:
            self._on_discord_toggle()
        finally:
            self._syncing_discord = False
        if role == ROLE_HOST:
            label = f"Sharing → {peer_name}"
        elif role == ROLE_MIRROR:
            label = f"Mirroring ← {peer_name}"
        elif peer_name:
            label = f"Linked: {peer_name}"
        else:
            label = "Link device"
            self._updating_sync_var = True
            self.sync_link_var.set(False)
            self._updating_sync_var = False
        is_on = role != ROLE_UNLINKED
        self._style_toggle(self.sync_link_checkbox, is_on, label)

    def on_sync_status(self, text):
        print(f"[sync] {text}")
        # Show network status only if we don't have a live heart rate.
        self.update_status_if_idle(text)

    def _submit_keyboard_sync(self, coroutine_factory, on_done=None):
        """Run keyboard-sync work on the app's asyncio thread.

        The Tk thread never blocks on IPC, and completion handlers are always
        marshalled back through ``root.after`` before touching UI state.
        """
        loop = globals().get("_loop_ref")
        ready = globals().get("_background_ready")
        if (
            loop is None
            or not loop.is_running()
            or ready is None
            or not ready.is_set()
        ):
            return None

        try:
            future = asyncio.run_coroutine_threadsafe(coroutine_factory(), loop)
        except Exception as exc:
            print(f"[keyboard sync] Could not schedule operation: {exc}")
            return None

        if on_done is not None:
            def dispatch(completed):
                self._keyboard_sync_completions.put((on_done, completed))

            future.add_done_callback(dispatch)
        return future

    def _drain_keyboard_sync_completions(self):
        """Run worker completions only from Tk's owning thread."""
        self._keyboard_sync_poll_job = None
        while True:
            try:
                callback, completed = self._keyboard_sync_completions.get_nowait()
            except queue.Empty:
                break
            if globals().get("_shutdown_initiated", False):
                continue
            try:
                callback(completed)
            except Exception as exc:
                print(
                    "[keyboard sync] Completion callback failed: "
                    f"{type(exc).__name__}: {exc}"
                )

        self._refresh_keyboard_sync_availability()

        if not globals().get("_shutdown_initiated", False):
            self._keyboard_sync_poll_job = self.root.after(
                50,
                self._drain_keyboard_sync_completions,
            )

    def _refresh_keyboard_sync_availability(self):
        if not self._keyboard_sync_opt_in:
            self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")
            return
        ready = globals().get("_background_ready")
        if ready is None or not ready.is_set():
            if (
                not globals().get("_shutdown_initiated", False)
                and (self._keyboard_sync_desired or self.keyboard_sync.enabled)
            ):
                self._keyboard_sync_desired = False
                self._keyboard_sync_generation += 1
                self._render_keyboard_sync_off("Keyboard sync blocked")
            self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")
            return
        if (
            globals().get("_shutdown_initiated", False)
            or self._keyboard_sync_desired
            or self._keyboard_sync_future is not None
            or self.keyboard_sync.state is not KeyboardSyncState.OEM_ONLY
        ):
            return
        if ready.is_set() and self._keyboard_sync_auto_start_pending:
            self._keyboard_sync_auto_start_pending = False
            self._keyboard_sync_generation += 1
            generation = self._keyboard_sync_generation
            self._updating_keyboard_sync_var = True
            try:
                self.keyboard_sync_var.set(True)
            finally:
                self._updating_keyboard_sync_var = False
            self._begin_keyboard_sync_enable(generation)
            return
        if ready.is_set():
            self.keyboard_sync_checkbox.config(state=tk.NORMAL, cursor="hand2")

    def _render_keyboard_sync_off(self, label="Keyboard sync"):
        self._updating_keyboard_sync_var = True
        try:
            self.keyboard_sync_var.set(False)
            self._style_toggle(self.keyboard_sync_checkbox, False, label)
        finally:
            self._updating_keyboard_sync_var = False
        self._last_requested_keyboard_color = None
        self._last_requested_keyboard_brightness = None
        self._last_keyboard_brightness_request_time = float("-inf")

    def _keyboard_sync_failure_message(self):
        fault = self.keyboard_sync.fault
        return getattr(fault, "message", None) or (
            "No qualified, reversible Clevo lighting-only IPC backend is available."
        )

    def _complete_keyboard_sync_enable(self, completed, generation):
        try:
            enabled = bool(completed.result())
        except Exception as exc:
            enabled = False
            print(f"[keyboard sync] Enable failed: {type(exc).__name__}: {exc}")

        stale = (
            generation != self._keyboard_sync_generation
            or not self._keyboard_sync_desired
        )
        if stale:
            if enabled and not self._keyboard_sync_desired:
                cleanup = self._submit_keyboard_sync(self.keyboard_sync.disable)
                if cleanup is None and not globals().get("_shutdown_initiated", False):
                    self._render_keyboard_sync_off("Keyboard sync blocked")
                    self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")
            return

        self._keyboard_sync_future = None
        if enabled and self.keyboard_sync.enabled:
            self._style_toggle(
                self.keyboard_sync_checkbox,
                True,
                "Keyboard sync (on)",
            )
            self.keyboard_sync_checkbox.config(state=tk.NORMAL, cursor="hand2")
            self._last_requested_keyboard_color = None
            self._last_requested_keyboard_brightness = None
            self._last_keyboard_brightness_request_time = float("-inf")
            return

        self._keyboard_sync_desired = False
        quarantined = self.keyboard_sync.state is KeyboardSyncState.QUARANTINED
        self._render_keyboard_sync_off(
            "Keyboard sync blocked"
        )
        if self.keyboard_sync.state in {
            KeyboardSyncState.BLOCKED,
            KeyboardSyncState.QUARANTINED,
        }:
            self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")
        message = self._keyboard_sync_failure_message()
        print(f"[keyboard sync] {message}")
        try:
            messagebox.showwarning(
                "Keyboard sync quarantined" if quarantined else "Keyboard sync unavailable",
                message,
                parent=self.root,
            )
        except Exception:
            pass

    def _complete_keyboard_sync_disable(self, completed, generation):
        try:
            stopped = bool(completed.result())
        except Exception as exc:
            stopped = False
            print(f"[keyboard sync] Disable note: {type(exc).__name__}: {exc}")
        if generation != self._keyboard_sync_generation:
            return
        self._keyboard_sync_future = None
        if not stopped or self.keyboard_sync.state == KeyboardSyncState.QUARANTINED:
            self._render_keyboard_sync_off("Keyboard sync blocked")
            self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")
        elif self.keyboard_sync.state == KeyboardSyncState.BLOCKED:
            self._render_keyboard_sync_off("Keyboard sync blocked")
            self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")
        else:
            self._render_keyboard_sync_off()
            if self._keyboard_sync_opt_in and not globals().get("_shutdown_initiated", False):
                self.keyboard_sync_checkbox.config(state=tk.NORMAL, cursor="hand2")

    def _on_keyboard_sync_toggle(self, *args):
        if self._updating_keyboard_sync_var:
            return

        requested_on = bool(self.keyboard_sync_var.get())
        self._keyboard_sync_generation += 1
        generation = self._keyboard_sync_generation

        if not requested_on:
            self._keyboard_sync_desired = False
            self._style_toggle(
                self.keyboard_sync_checkbox,
                True,
                "Keyboard sync stopping…",
            )
            self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")
            self._keyboard_sync_future = self._submit_keyboard_sync(
                self.keyboard_sync.disable,
                lambda completed: self._complete_keyboard_sync_disable(
                    completed,
                    generation,
                ),
            )
            if self._keyboard_sync_future is None:
                self._render_keyboard_sync_off("Keyboard sync blocked")
                self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")
            return

        if not self._keyboard_sync_opt_in:
            self._keyboard_sync_desired = False
            self._render_keyboard_sync_off("Keyboard sync blocked")
            return

        confirmed = messagebox.askyesno(
            "Enable experimental keyboard sync?",
            "This feature sends only acknowledged color and pulse-brightness commands "
            "through a qualified OEM lighting-only route. It will remain off if that route "
            "cannot be proven. Clevo FnKey and CPU-profile control must remain active.",
            parent=self.root,
        )
        if not confirmed:
            self._keyboard_sync_desired = False
            self._render_keyboard_sync_off()
            return

        self._begin_keyboard_sync_enable(generation)

    def _begin_keyboard_sync_enable(self, generation):
        """Start after either explicit confirmation or the saved default-on choice."""
        self._keyboard_sync_desired = True
        self._style_toggle(
            self.keyboard_sync_checkbox,
            True,
            "Keyboard sync starting…",
        )
        self._keyboard_sync_future = self._submit_keyboard_sync(
            self.keyboard_sync.enable,
            lambda completed: self._complete_keyboard_sync_enable(
                completed,
                generation,
            ),
        )
        if self._keyboard_sync_future is None:
            self._keyboard_sync_desired = False
            self._render_keyboard_sync_off("Keyboard sync blocked")
            try:
                messagebox.showwarning(
                    "Keyboard sync unavailable",
                    "The widget background worker is not ready. Keyboard sync stayed off.",
                    parent=self.root,
                )
            except Exception:
                pass

    def _complete_keyboard_color_request(self, completed, requested_color, generation):
        if self._keyboard_color_request_future is completed:
            self._keyboard_color_request_future = None
        try:
            accepted = bool(completed.result())
        except Exception as exc:
            accepted = False
            print(f"[keyboard sync] Static-color request failed: {type(exc).__name__}: {exc}")
        if (
            accepted
            and generation == self._keyboard_sync_generation
            and self._keyboard_sync_desired
        ):
            # This is a request cache only. The controller advances applied_color
            # exclusively after the vendor synchronously accepts the exact request.
            self._last_requested_keyboard_color = requested_color

    def _request_current_keyboard_color(self):
        if (
            self.current_bpm <= 0
            or not self._keyboard_sync_desired
            or self._keyboard_sync_future is not None
            or not self.keyboard_sync.enabled
        ):
            return
        if self._keyboard_color_request_future is not None:
            return

        color_text = self._cached_color
        if not color_text.startswith("#") or len(color_text) != 7:
            return
        requested_color = StaticColor(
            int(color_text[1:3], 16),
            int(color_text[3:5], 16),
            int(color_text[5:7], 16),
        )
        if requested_color == self._last_requested_keyboard_color:
            return

        generation = self._keyboard_sync_generation
        self._keyboard_color_request_future = self._submit_keyboard_sync(
            lambda: self.keyboard_sync.request_static_color(requested_color),
            lambda completed: self._complete_keyboard_color_request(
                completed,
                requested_color,
                generation,
            ),
        )

    @staticmethod
    def _keyboard_brightness_for_phase(
        phase,
        minimum=KEYBOARD_BRIGHTNESS_MIN,
        maximum=KEYBOARD_BRIGHTNESS_MAX,
    ):
        """Map the shared heartbeat phase to the keyboard's five-step scale."""
        minimum = max(0, min(4, int(minimum)))
        maximum = max(minimum, min(4, int(maximum)))
        normalized = (math.sin(phase) + 1.0) / 2.0
        span = maximum - minimum
        return max(
            minimum,
            min(
                maximum,
                minimum + int((normalized * span) + 0.5),
            ),
        )

    def _complete_keyboard_brightness_request(self, completed, requested_level, generation):
        if self._keyboard_brightness_request_future is completed:
            self._keyboard_brightness_request_future = None
        error = None
        try:
            accepted = bool(completed.result())
        except Exception as exc:
            accepted = False
            error = f"{type(exc).__name__}: {exc}"
            print(f"[keyboard sync] Brightness request failed: {error}")
        # #region agent log
        sent_at = getattr(self, "_agent_brightness_sent_at", None)
        _agent_log(
            "hr_widget.py:_complete_keyboard_brightness_request",
            "brightness request finished",
            {
                "accepted": accepted,
                "level": requested_level,
                "error": error,
                "age_s": None if sent_at is None else round(time.perf_counter() - sent_at, 3),
                "state": str(self.keyboard_sync.state),
                "bpm": self.current_bpm,
            },
            "D",
        )
        # #endregion
        if (
            accepted
            and generation == self._keyboard_sync_generation
            and self._keyboard_sync_desired
        ):
            self._last_requested_keyboard_brightness = requested_level

    def _request_current_keyboard_brightness(self, monotonic_now):
        # #region agent log
        diag = None
        if (
            self._keyboard_brightness_request_future is not None
            and self._keyboard_sync_desired
            and self.keyboard_sync.enabled
        ):
            pending_for = monotonic_now - getattr(self, "_agent_brightness_sent_at", monotonic_now)
            if pending_for >= 0.4 and monotonic_now - getattr(self, "_agent_last_stall_log", 0.0) >= 1.0:
                self._agent_last_stall_log = monotonic_now
                diag = {
                    "kind": "stall",
                    "pending_s": round(pending_for, 3),
                    "bpm": self.current_bpm,
                    "phase": round(self.pulse_phase, 3),
                    "last_level": self._last_requested_keyboard_brightness,
                    "state": str(self.keyboard_sync.state),
                    "status": self.status_text,
                }
        elif (
            self._keyboard_sync_desired
            and not self.keyboard_sync.enabled
            and monotonic_now - getattr(self, "_agent_last_kb_off_log", 0.0) >= 5.0
        ):
            self._agent_last_kb_off_log = monotonic_now
            diag = {
                "kind": "not_enabled",
                "state": str(self.keyboard_sync.state),
                "bpm": self.current_bpm,
                "status": self.status_text,
            }
        if diag:
            _agent_log(
                "hr_widget.py:_request_current_keyboard_brightness",
                "brightness path blocked",
                diag,
                "D",
            )
        # #endregion
        if (
            not self._keyboard_sync_desired
            or self._keyboard_sync_future is not None
            or not self.keyboard_sync.enabled
            or self._keyboard_brightness_request_future is not None
        ):
            return
        if (
            monotonic_now - self._last_keyboard_brightness_request_time
            < KEYBOARD_BRIGHTNESS_UPDATE_SECONDS
        ):
            return

        minimum_var = getattr(self, "min_brightness_var", None)
        maximum_var = getattr(self, "max_brightness_var", None)
        minimum = (
            minimum_var.get()
            if minimum_var is not None
            else KEYBOARD_BRIGHTNESS_MIN
        )
        maximum = (
            maximum_var.get()
            if maximum_var is not None
            else KEYBOARD_BRIGHTNESS_MAX
        )

        if self.current_bpm > 0:
            lead = (
                KEYBOARD_BRIGHTNESS_LATENCY_LEAD_SECONDS
                * (self.current_bpm / 60.0)
                * math.tau
            )
            requested_level = self._keyboard_brightness_for_phase(
                self.pulse_phase + lead,
                minimum,
                maximum,
            )
        else:
            requested_level = maximum

        if requested_level == self._last_requested_keyboard_brightness:
            return

        generation = self._keyboard_sync_generation
        self._last_keyboard_brightness_request_time = monotonic_now
        self._agent_brightness_sent_at = monotonic_now
        # #region agent log
        _agent_log(
            "hr_widget.py:_request_current_keyboard_brightness",
            "brightness queued",
            {
                "level": requested_level,
                "bpm": self.current_bpm,
                "phase": round(self.pulse_phase, 3),
                "sin": round(math.sin(self.pulse_phase), 3),
                "status": self.status_text,
                "state": str(self.keyboard_sync.state),
            },
            "E",
        )
        # #endregion
        self._keyboard_brightness_request_future = self._submit_keyboard_sync(
            lambda: self.keyboard_sync.request_brightness(requested_level),
            lambda completed: self._complete_keyboard_brightness_request(
                completed,
                requested_level,
                generation,
            ),
        )
        if self._keyboard_brightness_request_future is None:
            self._last_keyboard_brightness_request_time = float("-inf")

    def _reconcile_keyboard_sync_state(self):
        if self.keyboard_sync.state not in {
            KeyboardSyncState.BLOCKED,
            KeyboardSyncState.QUARANTINED,
        }:
            return
        if self.keyboard_sync_var.get() or self._keyboard_sync_desired:
            self._keyboard_sync_desired = False
            self._keyboard_sync_generation += 1
            label = (
                "Keyboard sync blocked"
                if self.keyboard_sync.state == KeyboardSyncState.QUARANTINED
                else "Keyboard sync blocked"
            )
            self._render_keyboard_sync_off(label)
            self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")

    def _prepare_keyboard_sync_for_suspend(self):
        """Make resume default to OEM-only control before hibernation begins."""
        self._keyboard_sync_desired = False
        self._keyboard_sync_generation += 1
        self._style_toggle(
            self.keyboard_sync_checkbox,
            True,
            "Keyboard sync stopping…",
        )
        self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")

    def _finish_keyboard_sync_suspend(self, stopped):
        if stopped and self.keyboard_sync.state == KeyboardSyncState.OEM_ONLY:
            self._render_keyboard_sync_off()
            if self._keyboard_sync_opt_in:
                self.keyboard_sync_checkbox.config(state=tk.NORMAL, cursor="hand2")
            return
        self._render_keyboard_sync_off("Keyboard sync blocked")
        self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")

    def _begin_keyboard_safe_hibernate(self, _completed=None):
        """Run suspend preparation on Tk's thread, then start async drain."""
        if globals().get("_shutdown_initiated", False):
            return
        self._prepare_keyboard_sync_for_suspend()
        scheduled = self._submit_keyboard_sync(
            lambda: _hibernate_after_keyboard_sync(self),
        )
        if scheduled is None:
            self._render_keyboard_sync_off("Keyboard sync blocked")
            self.keyboard_sync_checkbox.config(state=tk.DISABLED, cursor="arrow")

    def _on_threshold_toggle(self, *args):
        self._style_toggle(self.threshold_checkbox, self.new_threshold_var.get(), "Sitting thresholds")
        self._cached_color_bpm = -1 # Force color re-evaluation
        self.animate() # Trigger immediate redraw

    def _on_min_brightness_change(self, *args):
        if self._updating_brightness_sliders:
            return
        minimum = self.min_brightness_var.get()
        maximum = self.max_brightness_var.get()
        if minimum > maximum:
            self._updating_brightness_sliders = True
            try:
                self.max_brightness_var.set(minimum)
            finally:
                self._updating_brightness_sliders = False
        self._last_requested_keyboard_brightness = None
        self._last_keyboard_brightness_request_time = float("-inf")

    def _on_max_brightness_change(self, *args):
        if self._updating_brightness_sliders:
            return
        minimum = self.min_brightness_var.get()
        maximum = self.max_brightness_var.get()
        if maximum < minimum:
            self._updating_brightness_sliders = True
            try:
                self.min_brightness_var.set(maximum)
            finally:
                self._updating_brightness_sliders = False
        self._last_requested_keyboard_brightness = None
        self._last_keyboard_brightness_request_time = float("-inf")

    def _on_drag_start(self, event):
        if not self.controls_visible or event.widget is not self.canvas:
            return
            
        self._drag_start_x = event.x_root
        self._drag_start_y = event.y_root
        self._win_x = self.root.winfo_x()
        self._win_y = self.root.winfo_y()
        self._dragging = True
        self._drag_job = None

    def _on_drag_motion(self, event):
        if self._dragging:
            self._drag_target_x = self._win_x + (event.x_root - self._drag_start_x)
            self._drag_target_y = self._win_y + (event.y_root - self._drag_start_y)
            if not getattr(self, '_drag_job', None):
                self._drag_job = self.root.after(16, self._apply_drag)

    def _apply_drag(self):
        self._drag_job = None
        self.root.geometry(f"+{self._drag_target_x}+{self._drag_target_y}")

    def _on_drag_end(self, event):
        self._dragging = False
        if getattr(self, '_drag_job', None):
            self.root.after_cancel(self._drag_job)
            self._drag_job = None
            self.root.geometry(f"+{self._drag_target_x}+{self._drag_target_y}")

    def on_enter(self, event):
        if not self.controls_visible:
            self._cursor_entered_time = (
                time.time() if self._point_over_idle_text(event.x, event.y) else None
            )

    def on_leave(self, event):
        if not self.controls_visible:
            self._cursor_entered_time = None

    def on_motion(self, event):
        if self._dragging:
            return
        if not self.controls_visible:
            if self._point_over_idle_text(event.x, event.y):
                if self._cursor_entered_time is None:
                    self._cursor_entered_time = time.time()
            else:
                self._cursor_entered_time = None

    def _on_configure(self, event):
        if event.widget == self.root:
            self._cached_window_size = (event.width, event.height)
            if self.controls_visible:
                self._layout_hover_panel(event.width, event.height)
    '''
    stops = [
            (0,   (0, 0, 255)),     # Pure Blue
            (60,  (0, 0, 255)),     # Pure Blue until 60
            (100,  (0, 255, 0)),     # Green Midpoint shifted down from 115 to 100
            (155, (255, 0, 0)),     # Pure Red starts at 155 instead of 170
            (250, (255, 0, 0))      
        ]
    '''
    def get_color(self, bpm):
        if self.new_threshold_var.get():
            # Sitting thresholds
            stops = [
                (0,   (0, 0, 255)),     
                (58,  (0, 0, 255)),     
                (70, (0, 255, 0)),     
                (100, (255, 0, 0)),     
                (250, (255, 0, 0))      
            ]
        else:
            # Original thresholds
            stops = [
                (0,   (0, 0, 255)),     
                (60,  (0, 0, 255)),     
                (100, (0, 255, 0)),     
                (155, (255, 0, 0)),     
                (250, (255, 0, 0))      
            ]
        
        bpm = max(0, min(bpm, 250)) 
        for i in range(len(stops) - 1):
            if stops[i][0] <= bpm <= stops[i+1][0]:
                t = (bpm - stops[i][0]) / (stops[i+1][0] - stops[i][0])
                r = int(stops[i][1][0] + t * (stops[i+1][1][0] - stops[i][1][0]))
                g = int(stops[i][1][1] + t * (stops[i+1][1][1] - stops[i][1][1]))
                b = int(stops[i][1][2] + t * (stops[i+1][1][2] - stops[i][1][2]))
                return f"#{r:02x}{g:02x}{b:02x}"
        return "#ff0000" 

    def animate(self):
        # --- OPTIMIZATION: HALT WHEN HIDDEN ---
        # If the window is withdrawn (in tray), stop the loop entirely to save all idle resources.
        if self.root.state() == "withdrawn":
            self._animate_job = None
            return

        # Ensure we don't have multiple loops running
        if self._animate_job:
            self.root.after_cancel(self._animate_job)
            self._animate_job = None

        current_time = time.time()
        monotonic_now = time.perf_counter()
        
        needs_redraw = False
        
        needs_redraw = False
        
        # --- BPM TIMEOUT CHECK ---
        # Receipt time is updated for every packet, including an unchanged BPM.
        # A steady heart rate must not be mistaken for a dead data stream.
        if self._expire_stale_bpm(monotonic_now):
            needs_redraw = True
        
        # --- FIX #2: THROTTLED TOPMOST ENFORCEMENT ---
        if self.topmost_var.get() and (current_time - self._last_topmost_enforce >= 5.0):
            self._last_topmost_enforce = current_time
            try:
                HWND = ctypes.windll.user32.GetParent(self.root.winfo_id())
                ctypes.windll.user32.SetWindowPos(HWND, -1, 0, 0, 0, 0, 0x0013)
            except Exception:
                pass

        # --- SELECTIVE HOVER POLLING ---
        # Throttled polling (every 100ms instead of 33ms) to save CPU
        if current_time - getattr(self, '_last_hover_poll', 0) >= 0.1:
            self._last_hover_poll = current_time
            # Only poll mouse position if window is actually viewable/mapped
            if self.root.winfo_viewable():
                mouse_x = self.root.winfo_pointerx()
                mouse_y = self.root.winfo_pointery()
                win_x = self.root.winfo_rootx()
                win_y = self.root.winfo_rooty()
                win_w, win_h = self._cached_window_size
                cursor_in_window = (win_x <= mouse_x <= win_x + win_w and
                                    win_y <= mouse_y <= win_y + win_h)
                if self.controls_visible:
                    cursor_over_activation_target = cursor_in_window
                elif cursor_in_window:
                    cursor_over_activation_target = self._point_over_idle_text(
                        mouse_x - win_x,
                        mouse_y - win_y,
                    )
                else:
                    cursor_over_activation_target = False
                self._update_control_visibility(current_time, cursor_over_activation_target)

        # --- CHANGE DETECTION ---
        is_animating = self.pulse_var.get() and self.current_bpm > 0
        self.pulse_phase = self._pulse_clock.phase_at(monotonic_now)

        if is_animating:
            pulse_amplitude = (self.current_bpm / 60.0) * 1.2
            sine_val = math.sin(self.pulse_phase)
            current_size = self.base_font_size + (sine_val * pulse_amplitude)
        else:
            current_size = self.base_font_size

        current_int_size = int(current_size)
        font_changed = current_int_size != self._last_drawn_size

        if font_changed:
            needs_redraw = True

        new_time_str = time.strftime("%#I:%M %p")
        time_changed = new_time_str != self._cached_time_str
        if time_changed:
            self._cached_time_str = new_time_str
            needs_redraw = True

        bpm_changed = self.current_bpm != self._last_drawn_bpm
        if bpm_changed:
            self._last_drawn_bpm = self.current_bpm
            needs_redraw = True

        status_changed = self.status_text != self._last_drawn_status
        if status_changed:
            self._last_drawn_status = self.status_text
            needs_redraw = True

        controls_changed = self.controls_visible != self._last_controls_visible
        if controls_changed:
            self._last_controls_visible = self.controls_visible
            needs_redraw = True

        uptime_total_seconds = int(current_time - self.start_time)
        uptime_minutes = uptime_total_seconds // 60
        uptime_changed = uptime_minutes != self._last_uptime_minutes
        if uptime_changed:
            self._last_uptime_minutes = uptime_minutes
            needs_redraw = True

        current_window_size = self._cached_window_size
        window_changed = current_window_size != self._last_window_size
        if window_changed:
            self._last_window_size = current_window_size
            needs_redraw = True

        color_changed = False
        if self.current_bpm != self._cached_color_bpm:
            self._cached_color_bpm = self.current_bpm
            new_color = self.get_color(self.current_bpm) if self.current_bpm > 0 else "white"
            if new_color != self._cached_color:
                self._cached_color = new_color
                color_changed = True
                needs_redraw = True

        # Color and pulse-brightness requests share one serialized, acknowledged
        # lighting queue. Color is sent only when its HR zone changes; brightness
        # is coalesced to the newest heartbeat phase and capped at 10 Hz.
        self._reconcile_keyboard_sync_state()
        self._request_current_keyboard_color()
        self._request_current_keyboard_brightness(monotonic_now)

        if needs_redraw:
            # --- OPTIMIZATION: LAYOUT & FONT CACHING ---
            self._last_drawn_size = current_int_size
            
            font_tuple = ("Helvetica", current_int_size, "bold")

            if self.controls_visible and window_changed:
                self._layout_hover_panel(*current_window_size)

            text_str = f"{self.current_bpm} BPM" if self.current_bpm > 0 else self.status_text
            color = self._cached_color

            cx, cy = current_window_size[0] // 2, current_window_size[1] // 2
            if cx <= 1: cx, cy = 300, 150 

            # Update BPM item coords/font only if necessary
            if window_changed or font_changed:
                thickness = 2
                offsets = [(-thickness, 0), (thickness, 0), (0, -thickness), (0, thickness)]
                for i, (dx, dy) in enumerate(offsets):
                    self.canvas.coords(self._bpm_outlines[i], cx + dx, cy + dy)
                    self.canvas.itemconfigure(self._bpm_outlines[i], font=font_tuple)
                self.canvas.coords(self._bpm_text, cx, cy)
                self.canvas.itemconfigure(self._bpm_text, font=font_tuple)
            
            # Update BPM text/color if string/color changed
            if bpm_changed or status_changed:
                for i in range(4):
                    self.canvas.itemconfigure(self._bpm_outlines[i], text=text_str)
                self.canvas.itemconfigure(self._bpm_text, text=text_str)
            if color_changed:
                self.canvas.itemconfigure(self._bpm_text, fill=color)

            # --- DRAW CURRENT TIME ---
            time_str = self._cached_time_str
            time_y = cy - (self.base_font_size * 0.7) - 15
            if window_changed:
                for i, (dx, dy) in enumerate([(-1, 0), (1, 0), (0, -1), (0, 1)]):
                    self.canvas.coords(self._time_outlines[i], cx + dx, time_y + dy)
                self.canvas.coords(self._time_text, cx, time_y)
            
            if time_changed:
                for i in range(4):
                    self.canvas.itemconfigure(self._time_outlines[i], text=time_str)
                self.canvas.itemconfigure(self._time_text, text=time_str)

            # --- DRAW UPTIME ---
            hours = uptime_total_seconds // 3600
            minutes = (uptime_total_seconds % 3600) // 60
            uptime_str = f"\u0394 {hours}:{minutes:02d}"
            uptime_y = cy + (self.base_font_size * 0.7) + 15
            if window_changed:
                for i, (dx, dy) in enumerate([(-1, 0), (1, 0), (0, -1), (0, 1)]):
                    self.canvas.coords(self._uptime_outlines[i], cx + dx, uptime_y + dy)
                self.canvas.coords(self._uptime_text, cx, uptime_y)
            
            if uptime_changed:
                for i in range(4):
                    self.canvas.itemconfigure(self._uptime_outlines[i], text=uptime_str)
                self.canvas.itemconfigure(self._uptime_text, text=uptime_str)

        keyboard_is_animating = (
            self.current_bpm > 0
            and self._keyboard_sync_desired
            and self.keyboard_sync.enabled
        )
        interval = 33 if is_animating else (100 if keyboard_is_animating else 500)
        self._animate_job = self.root.after(interval, self.animate)
        
    def update_data(self, status, bpm=0, phase=None):
        self.status_text = status
        monotonic_now = time.perf_counter()
        pulse_clock = getattr(self, "_pulse_clock", None)
        if pulse_clock is None:
            pulse_clock = PulseClock(
                bpm=getattr(self, "current_bpm", 0),
                phase=getattr(self, "pulse_phase", 0.0),
                now=monotonic_now,
            )
            self._pulse_clock = pulse_clock

        if bpm > 0:
            self.last_bpm_received_time = time.time()
            self.last_bpm_received_monotonic = monotonic_now
            self.last_received_bpm = bpm
            self.current_bpm = bpm
            self.pulse_phase = pulse_clock.update(bpm, monotonic_now, phase)

            heartbeat_var = getattr(self, "heartbeat_sound_var", None)
            if heartbeat_var is not None and heartbeat_var.get():
                heartbeat_gate = getattr(self, "_heartbeat_gate", None)
                if heartbeat_gate is not None:
                    if heartbeat_gate.armed:
                        heartbeat_gate.reschedule(pulse_clock, monotonic_now)
                    else:
                        heartbeat_gate.arm(pulse_clock, monotonic_now)
                    self._schedule_heartbeat_tick(monotonic_now)
        else:
            self.current_bpm = 0
            self.last_received_bpm = 0
            self.pulse_phase = pulse_clock.update(0, monotonic_now)
            if hasattr(self, "_heartbeat_gate"):
                self._halt_heartbeat_audio()

        # Sync Discord bot if active
        global _bot_ref
        if _bot_ref:
            _bot_ref.current_bpm = self.current_bpm

        # --- OPTIMIZATION: REACTIVE WAKE-UP ---
        # If the animation loop is idling or stopped, wake it up immediately for a snappy update.
        if self.root.state() != "withdrawn":
            self.animate()

    def update_status_if_idle(self, status):
        """Set the caption only while no heart rate is on screen.

        update_data() defaults to bpm=0, which is a real "no reading" event:
        it clears current_bpm, resets the pulse clock, halts heartbeat audio
        and zeroes the Discord bot's cached bpm. Callers that merely want to
        describe link state must not do that to a live reading -- a status
        loop and an incoming-bpm callback that both write unconditionally
        just overwrite each other, and the display flips between the two.
        """
        if self.current_bpm <= 0:
            self.update_data(status)

# --- BLUETOOTH STABILITY LOGIC ---

_screen_off_triggered = False
_last_screen_off = 0            # secondary debounce: min 60s between triggers
_below_threshold_start_time = None

async def _hibernate_after_keyboard_sync(app):
    """Drain keyboard sync before the widget initiates hibernation."""
    try:
        # The controller owns bounded phase deadlines and finishes cleanup
        # before honoring cancellation; callers must not cut that transaction short.
        stopped = await app.keyboard_sync.disable()
    except Exception as exc:
        # A failed/uncertain stop must never trigger an automatic OEM repair.
        # The controller is responsible for terminating only its own broker.
        print(f"[keyboard sync] Pre-hibernate stop note: {type(exc).__name__}: {exc}")
        app._keyboard_sync_completions.put((app._finish_keyboard_sync_suspend, False))
        return
    if not stopped:
        print("[keyboard sync] Hibernation cancelled because sync did not stop safely.")
        app._keyboard_sync_completions.put((app._finish_keyboard_sync_suspend, False))
        return
    app._keyboard_sync_completions.put((app._finish_keyboard_sync_suspend, True))
    ctypes.windll.powrprof.SetSuspendState(1, 1, 0)

def hr_handler(sender, data, app):
    global _screen_off_triggered, _last_screen_off, _below_threshold_start_time, _bot_ref
    global _agent_last_hr_monotonic

    now_m = time.perf_counter()
    gap_s = None if _agent_last_hr_monotonic is None else now_m - _agent_last_hr_monotonic
    _agent_last_hr_monotonic = now_m
    ignored_mirror = bool(sync_link_ref and sync_link_ref.role == ROLE_MIRROR)
    # #region agent log
    if gap_s is None or gap_s >= 2.0 or ignored_mirror:
        _agent_log(
            "hr_widget.py:hr_handler",
            "hr notification",
            {
                "bpm": data[1] if len(data) > 1 else None,
                "gap_s": None if gap_s is None else round(gap_s, 3),
                "ignored_mirror": ignored_mirror,
                "role": sync_link_ref.role if sync_link_ref else None,
            },
            "B" if ignored_mirror else "C",
        )
    # #endregion

    # CRITICAL: If we are in MIRROR mode, ignore local BLE data.
    # The LAN relay is our primary source of truth now.
    if ignored_mirror:
        return

    bpm = data[1]
    app.root.after(0, app.update_data, "", bpm)
    if sync_link_ref:
        phase = app.current_pulse_phase() if hasattr(app, "current_pulse_phase") else app.pulse_phase
        sync_link_ref.notify_bpm(bpm, phase)

    threshold = 65
    now = time.time()
        
    if 0 < bpm < threshold and getattr(app, "_screen_off_enabled", False):
        if _below_threshold_start_time is None:
            _below_threshold_start_time = now
            print(f"BPM dropped below {threshold} ({bpm}). Timer started...")
        
        # Check if 1 minute (60s) has passed
        if (now - _below_threshold_start_time >= 60):
            if not _screen_off_triggered and (now - _last_screen_off >= 60):
                _screen_off_triggered = True
                _last_screen_off = now
                
                if getattr(app, "_hibernate_enabled", False):
                    print(f"BPM below {threshold} for 1 minute. Hibernating PC safely...")
                    # The BLE callback runs on the asyncio thread. Queue the
                    # entire prepare-then-drain sequence for Tk so widget state
                    # is never read or mutated cross-thread and ordering is FIFO.
                    app._keyboard_sync_completions.put(
                        (app._begin_keyboard_safe_hibernate, None)
                    )
                else:
                    print(f"BPM below {threshold} for 1 minute. Turning off monitor safely...")
                    HWND = app.root.winfo_id()
                    WM_SYSCOMMAND = 0x0112
                    SC_MONITORPOWER = 0xF170
                    ctypes.windll.user32.PostMessageW(HWND, WM_SYSCOMMAND, SC_MONITORPOWER, 2)
    elif bpm >= threshold:
        # Only reset when heart rate actually recovers to 65+
        if _below_threshold_start_time is not None or _screen_off_triggered:
            print(f"BPM recovered to {bpm}. Resetting trigger and timer.")
        _below_threshold_start_time = None
        _screen_off_triggered = False
    # If bpm is 0, we do nothing (preserve the current timer/trigger state)

async def _find_fitbit_fast(app, timeout=10.0):
    """Stream-based scan that returns the instant a Fitbit/Charge is detected,
    instead of waiting the full timeout like BleakScanner.discover() does."""
    found_device = asyncio.get_event_loop().create_future()

    def _on_detection(device, adv_data):
        if found_device.done():
            return
        # Stop scanning if we started mirroring in the middle of a scan
        if sync_link_ref and sync_link_ref.role == ROLE_MIRROR:
            if not found_device.done():
                found_device.set_result(None)
            return

        name = device.name or adv_data.local_name or ""
        if "Charge" in name or "Fitbit" in name:
            found_device.set_result(device)

    scanner = BleakScanner(detection_callback=_on_detection)
    await scanner.start()
    try:
        device = await asyncio.wait_for(found_device, timeout=timeout)
        return device
    except asyncio.TimeoutError:
        return None
    finally:
        await scanner.stop()

async def scan_and_connect(app):
    target_device = None   # BLEDevice object (not just address string)

    while True:
        # While mirroring another device's Fitbit connection over the sync
        # link, don't compete for the Fitbit's single BLE connection slot.
        # EXCEPT: If we haven't received a heart rate packet in 30 seconds,
        # the peer might have stalled, so we attempt to take over.
        if sync_link_ref and sync_link_ref.role == ROLE_MIRROR:
            if not sync_link_ref.mirror_is_stale():
                # Caption only; the relayed readings own the BPM display.
                app.root.after(
                    0, app.update_status_if_idle, f"Mirroring {sync_link_ref.peer_name}"
                )
                await asyncio.sleep(1.0)
                continue
            else:
                print("Mirror data stale (>30s). Attempting to take over HOST role...")
                global _agent_mirror_takeover
                _agent_mirror_takeover = True
                # We don't force-change the role here, we just fall through
                # to the scanning phase. If we find the Fitbit, notify_fitbit_state(True)
                # will trigger a role negotiation that makes us the HOST.

        # --- 1. SCANNING PHASE ---
        if target_device is None:
            try:
                app.root.after(0, app.update_data, "Scanning...")
                print("Scanning for Charge 6...")
                
                # Stream-based scan: returns the INSTANT the Fitbit is seen,
                # rather than waiting the full timeout like discover() does.
                device = await _find_fitbit_fast(app, timeout=10.0)
                if device:
                    target_device = device
                    print(f"Found Fitbit at {device.address}")
                        
                if not target_device:
                    if sync_link_ref and sync_link_ref.role == ROLE_MIRROR:
                        continue
                    app.root.after(0, app.update_data, "Not Found...")
                    await asyncio.sleep(0.3) 
                    continue 
                    
            except Exception as e:
                print(f"Scan error: {e}")
                await asyncio.sleep(0.3)
                continue

        # Check again before connecting
        if sync_link_ref and sync_link_ref.role == ROLE_MIRROR:
            target_device = None
            continue

        # --- 2. CONNECTING PHASE ---
        drop_reason = "context-exit"
        disconnect_seen = False
        session_started = False
        notify_error = None
        session_ended_at = None
        connected_at = time.perf_counter()
        address_tail = (getattr(target_device, "address", "") or "")[-4:]
        try:
            app.root.after(0, app.update_data, "Connecting...")
            print(f"Attempting connection to {target_device.address}...")

            def _on_ble_disconnect(_client):
                nonlocal disconnect_seen
                disconnect_seen = True
            
            async with BleakClient(
                target_device,
                timeout=5.0,
                disconnected_callback=_on_ble_disconnect,
            ) as client:
                session_started = True
                # Check again once connected but before subscribing
                if sync_link_ref and sync_link_ref.role == ROLE_MIRROR:
                    drop_reason = "role-mirror"
                    print("Role changed to MIRROR during connection; dropping BLE.")
                    break

                # --- AUTOMATED BACKGROUND PAIRING (WINRT) ---
                # Windows 11 normally pops up an "Add a device -> Allow" notification when
                # pairing implicitly. To automate this and suppress the UI, we intercept the
                # WinRT custom pairing events and automatically accept them in the background.
                try:
                    from bleak.backends.winrt.client import BleakClientWinRT
                    from winrt.windows.devices.enumeration import DeviceInformation, DevicePairingKinds

                    if isinstance(client._backend, BleakClientWinRT):
                        # Get fresh device info
                        dev_info = await DeviceInformation.create_from_id_async(
                            client._backend._requester.device_information.id
                        )

                        if not dev_info.pairing.is_paired:
                            app.root.after(0, app.update_data, "Auto-Pairing...")
                            print("Auto-accepting pairing request in background...")

                            custom_pairing = dev_info.pairing.custom

                            # Automatically accept any pairing prompt without UI
                            def pairing_handler(sender, args):
                                print(f"Intercepted pairing prompt ({args.pairing_kind.name}), auto-accepting...")
                                args.accept()

                            token = custom_pairing.add_pairing_requested(pairing_handler)
                            try:
                                # Trigger Just Works pairing (CONFIRM_ONLY)
                                await custom_pairing.pair_async(DevicePairingKinds.CONFIRM_ONLY)
                            finally:
                                custom_pairing.remove_pairing_requested(token)

                            print("Background pairing complete.")
                except Exception as e:
                    print(f"Auto-pairing note (non-fatal): {e}")

                try:
                    await client.start_notify(HR_MEASUREMENT_UUID, lambda s, d: hr_handler(s, d, app))

                    print("HR Stream Authorized & Active!")
                    app.root.after(0, app.update_data, "-- BPM")
                    minimize_console()
                    if sync_link_ref:
                        sync_link_ref.notify_fitbit_state(True)

                    connected_at = time.perf_counter()
                    next_watch = connected_at + 15.0
                    while client.is_connected:
                        await asyncio.sleep(1)
                        if sync_link_ref and sync_link_ref.role == ROLE_MIRROR:
                            # Release the old local GATT when the peer wins host
                            # ownership; ignoring its readings alone retains BLE.
                            drop_reason = "role-mirror"
                            break
                        # #region agent log
                        now_m = time.perf_counter()
                        if now_m >= next_watch:
                            next_watch = now_m + 15.0
                            since_hr = (
                                None
                                if _agent_last_hr_monotonic is None
                                else round(now_m - _agent_last_hr_monotonic, 3)
                            )
                            _agent_log(
                                "hr_widget.py:scan_and_connect",
                                "ble link watch",
                                {
                                    "connected_s": round(now_m - connected_at, 1),
                                    "since_hr_s": since_hr,
                                    "is_connected": bool(client.is_connected),
                                    "disconnect_callback": disconnect_seen,
                                    "bpm": app.current_bpm,
                                    "role": sync_link_ref.role if sync_link_ref else None,
                                    "kb_state": str(app.keyboard_sync.state),
                                    "kb_enabled": bool(app.keyboard_sync.enabled),
                                    "brightness_in_flight": app._keyboard_brightness_request_future is not None,
                                    "last_level": app._last_requested_keyboard_brightness,
                                    "status": app.status_text,
                                },
                                "A",
                            )
                        # #endregion

                except Exception as e:
                    notify_error = f"{type(e).__name__}: {e}"
                    print(f"Request rejected: {e}")
                    pass

            # Once the connection drops:
            session_ended_at = time.perf_counter()
            print("Connection lost. Resetting target and retrying in 3s...")
            target_device = None
            if _bot_ref:
                _bot_ref.current_bpm = 0
            if sync_link_ref:
                sync_link_ref.notify_fitbit_state(False)

            await asyncio.sleep(3.0)

        except Exception as e:
            drop_reason = f"{type(e).__name__}: {e}"
            session_ended_at = time.perf_counter()
            print(f"Connection failed: {e}. Retrying in 3s...")
            if _bot_ref:
                _bot_ref.current_bpm = 0
            if sync_link_ref:
                sync_link_ref.notify_fitbit_state(False)
            # Re-scan if the device can't be found (address may have changed)
            target_device = None
            await asyncio.sleep(3.0)
        finally:
            # #region agent log
            if session_started or drop_reason != "context-exit":
                now_m = session_ended_at if session_ended_at is not None else time.perf_counter()
                since_hr = (
                    None
                    if _agent_last_hr_monotonic is None
                    else round(now_m - _agent_last_hr_monotonic, 3)
                )
                _agent_log(
                    "hr_widget.py:scan_and_connect",
                    "ble session ended",
                    {
                        "reason": drop_reason,
                        "disconnect_callback": disconnect_seen,
                        "notify_error": notify_error,
                        "connected_s": round(now_m - connected_at, 1),
                        "since_hr_s": since_hr,
                        "bpm": getattr(app, "current_bpm", None),
                        "role": sync_link_ref.role if sync_link_ref else None,
                        "mirror_takeover": _agent_mirror_takeover,
                        "address_tail": address_tail,
                    },
                    "B" if drop_reason == "role-mirror" or _agent_mirror_takeover else "A",
                )
                _agent_mirror_takeover = False
            # #endregion

# --- MAIN ASYNC LOOP (runs BLE + Discord together) ---

_bot_ref = None
_loop_ref = None
_shutdown_initiated = False
_shutdown_event = None
_shutdown_requested = threading.Event()
_ble_task = None
_background_stopped = threading.Event()
_background_ready = threading.Event()
_keyboard_sync_shutdown_result = None
sync_link_ref = None

async def start_discord_bot():
    global _bot_ref
    if sync_link_ref and sync_link_ref.role == ROLE_MIRROR: return
    if _bot_ref and not _bot_ref.is_closed(): return

    if DISCORD_TOKEN and DISCORD_TOKEN != "YOUR_BOT_TOKEN_HERE":
        intents = discord.Intents.default()
        bot = HeartRateBot(intents=intents)
        _bot_ref = bot
        print("Starting Discord bot...")
        try:
            await bot.start(DISCORD_TOKEN)
        except Exception as e:
            print(f"Discord bot disconnected: {e}")
        finally:
            _bot_ref = None

def _on_sync_role_change(role, peer_name):
    if _shutdown_initiated:
        return
    app.root.after(0, app.on_sync_role_change, role, peer_name)

def _on_sync_discord(enabled):
    if _shutdown_initiated:
        return
    def apply():
        app._syncing_discord = True
        try:
            app.discord_var.set(enabled)
        finally:
            app._syncing_discord = False
    app.root.after(0, apply)

def _on_sync_status(text):
    if _shutdown_initiated:
        return
    app.root.after(0, app.on_sync_status, text)

def _on_sync_remote_bpm(bpm, phase):
    if _shutdown_initiated:
        return
    app.root.after(0, app.update_data, "", bpm, phase)

async def main_async(app):
    global sync_link_ref, _shutdown_event, _ble_task, _keyboard_sync_shutdown_result
    _shutdown_event = asyncio.Event()
    if _shutdown_requested.is_set():
        _shutdown_event.set()
    sync_link_ref = SyncLink(
        socket.gethostname(),
        load_or_create_token(),
        on_role_change=_on_sync_role_change,
        on_status=_on_sync_status,
        on_remote_bpm=_on_sync_remote_bpm,
        on_discord_change=_on_sync_discord,
    )
    try:
        await asyncio.wait_for(sync_link_ref.start(), timeout=5.0)
    except asyncio.TimeoutError:
        print("[sync_link] Startup timed out. Continuing without phone sync.")
    except OSError as e:
        print(f"[sync_link] Could not start (port {sync_link_ref.port} busy?): {e}. "
              f"Continuing without phone sync.")

    # Publish keyboard-work readiness only after startup has reached the
    # cancellable main wait and the shutdown event is guaranteed to exist.
    _background_ready.set()

    # Keep ownership of the BLE task here so shutdown can cancel and await it.
    # That lets BleakClient.__aexit__ release the Windows GATT session before
    # the process exits; os._exit() alone skips every async context cleanup.
    _ble_task = asyncio.create_task(scan_and_connect(app), name="fitbit-ble")
    shutdown_waiter = asyncio.create_task(_shutdown_event.wait(), name="widget-shutdown")
    try:
        done, _pending = await asyncio.wait(
            {_ble_task, shutdown_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if _ble_task in done and not _shutdown_event.is_set():
            try:
                _ble_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"BLE worker stopped unexpectedly: {type(e).__name__}: {e}")
    finally:
        shutdown_waiter.cancel()
        try:
            await shutdown_waiter
        except asyncio.CancelledError:
            pass

        keyboard_sync = getattr(app, "keyboard_sync", None)
        if keyboard_sync is not None:
            try:
                _keyboard_sync_shutdown_result = await keyboard_sync.shutdown()
                if not _keyboard_sync_shutdown_result:
                    print(
                        "Keyboard-sync shutdown completed in quarantine; "
                        "broker-stop confirmation will be checked before process exit."
                    )
            except Exception as e:
                _keyboard_sync_shutdown_result = False
                # The controller may terminate only its own future broker. OEM
                # processes, services, packages, and CPU profiles remain untouched.
                print(f"Keyboard-sync shutdown note: {type(e).__name__}: {e}")

        ble_task = _ble_task
        if ble_task is not None and not ble_task.done():
            print("Disconnecting Fitbit cleanly...")
            ble_task.cancel()
        if ble_task is not None:
            try:
                await ble_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"BLE shutdown note: {type(e).__name__}: {e}")
        _ble_task = None

        try:
            await asyncio.wait_for(_close_discord(), timeout=4.0)
        except Exception as e:
            print(f"Discord shutdown note: {type(e).__name__}: {e}")

        if sync_link_ref:
            try:
                await asyncio.wait_for(sync_link_ref.stop(), timeout=3.0)
            except Exception as e:
                print(f"Sync-link shutdown note: {type(e).__name__}: {e}")

def start_background_thread(app):
    global _loop_ref
    _background_ready.clear()
    _background_stopped.clear()
    loop = asyncio.new_event_loop()
    _loop_ref = loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main_async(app))
    finally:
        _background_ready.clear()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        _loop_ref = None
        _background_stopped.set()

# --- TRAY ICON LOGIC ---
_tray_icon_ref = None

def create_tray_image():
    try:
        return Image.open(ICON_PATH)
    except Exception:
        image = Image.new('RGB', (64, 64), color=(0, 0, 0))
        dc = ImageDraw.Draw(image)
        dc.ellipse((8, 8, 56, 56), fill=(255, 0, 0))
        return image

def setup_tray_icon(app):
    global _tray_icon_ref
    def on_toggle(icon, item):
        # Use state() to check if window is hidden (withdrawn) or visible
        state = app.root.state()
        if state == "withdrawn" or not app.root.winfo_viewable():
            app.root.after(0, app.root.deiconify)
            app.root.after(0, app.root.lift)
            app.root.after(0, app.root.focus_force)
            app.root.after(0, lambda: app.root.attributes("-topmost", app.topmost_var.get()))
            # WAKE UP THE LOOP
            app.root.after(100, app.animate)
        else:
            app.root.after(0, app.root.withdraw)

    def on_exit(icon, item):
        icon.stop()
        app.root.after(0, shutdown_bot)

    image = create_tray_image()
    menu = pystray.Menu(
        pystray.MenuItem("Toggle Widget", on_toggle, default=True),
        pystray.MenuItem("Exit", on_exit)
    )
    
    icon = pystray.Icon("HR Widget", image, "Live HR Widget", menu, action=on_toggle)
    _tray_icon_ref = icon
    
    tray_thread = threading.Thread(target=icon.run, daemon=True)
    tray_thread.start()

def shutdown_bot():
    global _shutdown_initiated
    if _shutdown_initiated: return
    _shutdown_initiated = True
    _shutdown_requested.set()

    widget = globals().get("app")
    if widget is not None and hasattr(widget, "shutdown_heartbeat_audio"):
        widget.shutdown_heartbeat_audio()
    if widget is not None and hasattr(widget, "_prepare_keyboard_sync_for_suspend"):
        widget._prepare_keyboard_sync_for_suspend()

    # Instantly hide the window to feel responsive
    try:
        root.withdraw()
    except Exception:
        pass
        
    if _tray_icon_ref:
        try:
            _tray_icon_ref.stop()
        except Exception:
            pass

    # Ask the asyncio owner to cancel and await BLE before terminating the
    # process. This is what releases the old Windows GATT session so a newly
    # launched widget can attach to the same HR-on-equipment session.
    loop = _loop_ref
    background_quiescent = True
    if loop and loop.is_running():
        if _shutdown_event is not None:
            loop.call_soon_threadsafe(_shutdown_event.set)
        background_quiescent = _background_stopped.wait(timeout=20.0)
        if not background_quiescent:
            print("Async shutdown timed out; evaluating the keyboard broker safety gate.")

    keyboard_sync = getattr(widget, "keyboard_sync", None)
    broker_stopped = (
        keyboard_sync is None or keyboard_sync.broker_stop_confirmed
    )
    if not background_quiescent or not broker_stopped:
        reason = (
            "the background worker is still active"
            if not background_quiescent
            else "isolated keyboard broker termination was not confirmed"
        )
        print(
            f"Process exit withheld: {reason}. "
            "No Clevo process or service will be restarted automatically."
        )
        try:
            root.deiconify()
            messagebox.showerror(
                "Keyboard sync cleanup unconfirmed",
                f"The widget will remain open because {reason}. It will not force an "
                "unsafe exit. Clevo software was not changed or restarted; you may retry "
                "Exit after the background worker settles.",
                parent=root,
            )
        except Exception:
            pass
        _shutdown_initiated = False
        return

    # The async owner has now drained keyboard sync (or bounded its shutdown)
    # before the hard process exit used by this long-running tray application.
    os._exit(0)

async def _close_discord():
    if _bot_ref and not _bot_ref.is_closed():
        async def _reset_nick(guild):
            try:
                await guild.me.edit(nick="BPMsworth")
            except Exception:
                pass
        # If we're MIRRORing, we never wrote the nickname in the first place
        # (see HeartRateBot.update_nickname) -- resetting it here would stomp
        # on the HOST's live value instead of leaving it alone.
        was_mirror = sync_link_ref is not None and sync_link_ref.role == ROLE_MIRROR
        try:
            if not was_mirror:
                await asyncio.gather(*(_reset_nick(g) for g in _bot_ref.guilds))
            await _bot_ref.change_presence(status=discord.Status.offline)
            # Give Discord's gateway a moment to process the changes before we drop the websocket
            await asyncio.sleep(0.5)
        except Exception:
            pass
        await _bot_ref.close()

def signal_handler(sig, frame):
    print("\nInterrupt received, shutting down...")
    shutdown_bot()

if __name__ == "__main__":
    experimental_clevo_ipc = "--disable-experimental-clevo-ipc" not in sys.argv[1:]
    keyboard_sync_default_on = (
        experimental_clevo_ipc and "--keyboard-sync-off" not in sys.argv[1:]
    )
    setup_profiling()
    signal.signal(signal.SIGINT, signal_handler)
    
    # Tell Windows this is a distinct app to ensure the taskbar uses the custom icon instead of python.exe
    if os.name == 'nt':
        myappid = 'bellsworth.hr_widget.1.0'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        
    root = tk.Tk()
    
    # Set the window and taskbar icon
    try:
        root.iconbitmap(ICON_PATH)
    except Exception as e:
        print(f"Failed to load icon: {e}")
        
    app = HeartRateWidget(
        root,
        enable_experimental_clevo_ipc=experimental_clevo_ipc,
        keyboard_sync_default_on=keyboard_sync_default_on,
    )
    root.protocol("WM_DELETE_WINDOW", shutdown_bot)
    
    setup_tray_icon(app)
    
    bg_thread = threading.Thread(target=start_background_thread, args=(app,), daemon=True)
    bg_thread.start()
    
    try:
        root.mainloop()
    except KeyboardInterrupt:
        shutdown_bot()
        
    shutdown_bot()
