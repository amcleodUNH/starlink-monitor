"""
Starlink Dish Monitoring Dashboard
Connects to the dish gRPC API at 192.168.100.1:9200 (standard gRPC/HTTP2).
Requires: pip install grpcio grpcio-tools pyserial
"""

import os
import sys
import csv
import math
import time
import datetime
import threading
import subprocess
import tempfile
import struct
import tkinter as tk
from tkinter import ttk, font as tkfont
from collections import deque
from pathlib import Path

DISH_HOST = "192.168.100.1:9200"

# Firmware the protobuf field numbers were reverse-engineered/verified against.
# On a mismatch the dashboard keeps running but flags it in the Dish Info panel,
# since a different firmware could shift field numbers and skew readings.
# Field mappings confirmed unchanged on 2026.06.15.mr81291 (re-verified by wire-decode).
KNOWN_FIRMWARE = "2026.06.15.mr81291"

POLL_INTERVAL = 2    # seconds between live polls
HISTORY_LEN  = 600   # sparkline sample buffer; 600 pts × 2 s = 20 min
HIST_POINTS  = 600   # throughput history deque; 600 pts × 2 s = 20 min
BOXCAR_N     = 100   # throughput mean = moving boxcar over the last 100 samples

GPS_PORT = "COM10"
GPS_BAUD = 9600

# Optional "Likely satellite" TLE matching (detail window). Requires sgp4 + numpy.
SAT_MATCH_INTERVAL = 15   # seconds between TLE look-angle matches (handoffs are ~15 s)

# ---------------------------------------------------------------------------
# Proto generation (embedded .proto, compiled at first run)
# ---------------------------------------------------------------------------

PROTO_DIR = Path(tempfile.gettempdir()) / "starlink_proto"
PROTO_FILE = PROTO_DIR / "starlink.proto"

PROTO_SRC = r"""
syntax = "proto3";
package SpaceX.API.Device;

service Device {
    rpc Handle(Request) returns (Response) {}
}

message DeviceInfo {
    string id = 1;
    string hardware_version = 2;
    string software_version = 3;
    string country_code = 4;
    bool software_partitions_equal = 8;
}

message DeviceState {
    uint64 uptime_s = 1;
}

message DishSignalStats {
    uint32 index = 1;
    float snr_db = 3;
    float elevation_deg = 4;
    float azimuth_deg = 5;
    uint32 rx_beam_state = 6;
    // f7 was mapped to "obstruction_score" but it is an alignment/uncertainty
    // metric, not an obstruction fraction: it swings (e.g. 0.62 -> 0.44 between
    // polls) and reads high even with a verified clear sky. Kept for raw logging
    // only; not surfaced in the UI as obstruction.
    float align_metric = 7;
    float secondary_elevation_deg = 8;
    float secondary_azimuth_deg = 9;
}

message DishObstructionStats {
    bool currently_obstructed = 1;
    uint32 obstruction_duration_s = 2;
    uint32 obstruction_event_count = 5;
}

// Per-sector (wedge) signal quality, 10 sectors
message DishSectorSignal {
    uint32 s1 = 1;
    uint32 s2 = 2;
    uint32 s3 = 3;
    uint32 s4 = 4;
    uint32 s5 = 5;
    uint32 s6 = 6;
    uint32 s7 = 7;
    uint32 s8 = 8;
    uint32 s9 = 9;
    uint32 s10 = 10;
}

// 5 readiness flags (all 1 = fully operational)
message DishReadyStates {
    bool cady = 2;
    bool scp = 3;
    bool l1l2 = 4;
    bool xphy = 5;
    bool aap = 6;
}

// GPS / IMU status
message DishGpsStatus {
    bool valid = 1;
    float accuracy = 2;
}

// Dish orientation quaternion (x,w,y,z ordering confirmed by wire decode)
message DishTilt {
    float x = 1;
    float w = 2;
    float y = 3;
    float z = 4;
}

// Actual field numbers confirmed by raw wire-decoding against firmware 2026.05.26
message DishGetStatusResponse {
    DeviceInfo device_info = 1;
    DeviceState device_state = 2;

    float pop_ping_drop_rate = 1006;
    float downlink_throughput_bps = 1007;
    float uplink_throughput_bps = 1008;
    float pop_ping_latency_ms = 1009;

    float boresight_elevation_deg = 1011;
    float boresight_azimuth_deg = 1012;

    DishObstructionStats obstruction_stats = 1015;
    uint32 eth_speed_mbps = 1016;

    DishReadyStates ready_states = 1019;

    DishGpsStatus gps_status = 1026;
    DishSignalStats signal_stats = 1027;
    DishSectorSignal sector_signal = 1028;

    DishTilt tilt_quaternion = 1049;

    // Additional fields confirmed by wire-decode (fw 2026.05.26)
    string router_id = 1040;        // e.g. "Router-01000000000000000092F196"
    float  dish_timestamp = 1002;   // Unix timestamp from dish clock
}

message DishGetHistoryResponse {
    uint64 current = 1;
    // Packed float arrays — 900 seconds of 1Hz history
    // Field numbers confirmed by wire-decoding against firmware 2026.05.26
    repeated float pop_ping_drop_rate = 1001 [packed=true];
    repeated float pop_ping_latency_ms = 1002 [packed=true];
    repeated float downlink_throughput_bps = 1003 [packed=true];
    repeated float uplink_throughput_bps = 1004 [packed=true];
    // NOTE: field 1010 was previously mapped to snr_db, but wire-decoding showed
    // it ranges ~16-89 (mean ~32) and does not track the live signal_stats.snr_db
    // (~16 dB). It is NOT SNR, so it is intentionally not parsed/seeded.
}

message GetStatusRequest {}
message DishGetHistoryRequest {}

message Request {
    uint64 id = 1;
    oneof request {
        GetStatusRequest get_status = 1004;
        DishGetHistoryRequest get_history = 1007;
    }
}

message Response {
    uint64 id = 1;
    oneof response {
        DishGetStatusResponse dish_get_status = 2004;
        DishGetHistoryResponse dish_get_history = 2006;
    }
}
"""


def ensure_proto_compiled():
    PROTO_DIR.mkdir(exist_ok=True)
    pb2_file = PROTO_DIR / "starlink_pb2.py"
    existing = PROTO_FILE.read_text() if PROTO_FILE.exists() else ""
    needs_compile = not pb2_file.exists() or existing.strip() != PROTO_SRC.strip()
    PROTO_FILE.write_text(PROTO_SRC)
    if needs_compile:
        subprocess.check_call([
            sys.executable, "-m", "grpc_tools.protoc",
            f"--proto_path={PROTO_DIR}",
            f"--python_out={PROTO_DIR}",
            f"--grpc_python_out={PROTO_DIR}",
            str(PROTO_FILE),
        ])
    sys.path.insert(0, str(PROTO_DIR))


# ---------------------------------------------------------------------------
# Starlink client  (gRPC-Web over HTTP/1.1)
# ---------------------------------------------------------------------------

class StarlinkClient:
    """Standard gRPC (HTTP/2) client on port 9200 — no auth required."""

    def __init__(self):
        import grpc, importlib
        ensure_proto_compiled()
        self.pb2 = importlib.import_module("starlink_pb2")
        self.pb2_grpc = importlib.import_module("starlink_pb2_grpc")
        channel = grpc.insecure_channel(DISH_HOST)
        self.stub = self.pb2_grpc.DeviceStub(channel)

    def get_status(self):
        req = self.pb2.Request()
        req.get_status.CopyFrom(self.pb2.GetStatusRequest())
        return self.stub.Handle(req, timeout=5).dish_get_status

    def get_history(self):
        req = self.pb2.Request()
        req.get_history.CopyFrom(self.pb2.DishGetHistoryRequest())
        return self.stub.Handle(req, timeout=10).dish_get_history


# ---------------------------------------------------------------------------
# Colour palette + font scale
# ---------------------------------------------------------------------------

BG = "#0d1117"
CARD = "#161b22"
BORDER = "#30363d"
TEXT = "#e6edf3"
DIM = "#8b949e"
GREEN = "#3fb950"
YELLOW = "#d29922"
RED = "#f85149"
BLUE = "#58a6ff"
TEAL = "#39d353"
ORANGE = "#db6d28"
PURPLE = "#bc8cff"

# ---------------------------------------------------------------------------
# Fonts — named tkfont.Font objects so they can be rescaled live with the window.
# Until init_fonts() runs (needs a Tk root) these are plain tuples, which are
# still valid anywhere a font is accepted; init_fonts() swaps in Font objects and
# attach_font_scaling() resizes them on <Configure>. Updating one Font object
# restyles every widget that uses it, so the whole UI scales together.
# ---------------------------------------------------------------------------
_FONT_SPECS = {              # name: (family, base_size, weight)
    "F_CARD":       ("Consolas", 12, "bold"),    # card title
    "F_KEY":        ("Consolas", 11, "normal"),  # row key labels (dim)
    "F_VAL":        ("Consolas", 12, "normal"),  # row value text
    "F_SMALL":      ("Consolas", 10, "normal"),  # secondary labels
    "F_TINY":       ("Consolas",  8, "normal"),  # axis tick labels
    "F_BIG":        ("Consolas", 28, "bold"),    # metric card main number
    "F_SMALL_BOLD": ("Consolas", 10, "bold"),    # emphasised secondary labels
    "F_TITLE":      ("Consolas", 14, "bold"),    # window header banners
}
_FONT_OBJS = {}   # name -> tkfont.Font (populated by init_fonts)
_FONT_BASE = {}   # name -> base point size

# Tuple placeholders so references resolve before init_fonts() swaps in Font objects.
F_CARD       = _FONT_SPECS["F_CARD"]
F_KEY        = _FONT_SPECS["F_KEY"]
F_VAL        = _FONT_SPECS["F_VAL"]
F_SMALL      = _FONT_SPECS["F_SMALL"]
F_TINY       = _FONT_SPECS["F_TINY"]
F_BIG        = _FONT_SPECS["F_BIG"]
F_SMALL_BOLD = _FONT_SPECS["F_SMALL_BOLD"]
F_TITLE      = _FONT_SPECS["F_TITLE"]


def init_fonts(root):
    """Create the named Font objects once a Tk root exists, replacing the
    tuple placeholders in module globals."""
    g = globals()
    for name, (family, size, weight) in _FONT_SPECS.items():
        f = tkfont.Font(root=root, family=family, size=size, weight=weight)
        _FONT_OBJS[name] = f
        _FONT_BASE[name] = size
        g[name] = f


class FontScaler:
    """Maps window size to a clamped scale factor and resizes all named fonts."""
    def __init__(self, lo=0.80, hi=1.55):
        self.lo, self.hi = lo, hi
        self._last = None

    def apply(self, w, h, base_w, base_h):
        if w <= 1 or h <= 1:
            return
        scale = max(self.lo, min(self.hi, min(w / base_w, h / base_h)))
        q = round(scale, 2)
        if q == self._last:
            return
        self._last = q
        for name, f in _FONT_OBJS.items():
            f.configure(size=max(6, int(round(_FONT_BASE[name] * q))))


def attach_font_scaling(window, scaler, base_w, base_h):
    """Rescale fonts (debounced) whenever this top-level window is resized."""
    state = {"job": None}

    def _on_configure(e):
        if e.widget is not window:
            return            # ignore child-widget configure events
        if state["job"] is not None:
            window.after_cancel(state["job"])
        state["job"] = window.after(
            60, lambda: scaler.apply(window.winfo_width(),
                                     window.winfo_height(), base_w, base_h))

    window.bind("<Configure>", _on_configure, add="+")


def copyable_label(parent, var, fg=TEXT, font=None, bg=CARD,
                   justify="left", width=0):
    """Read-only Entry styled as a plain label.
    Supports native text selection and Ctrl+C."""
    e = tk.Entry(
        parent,
        textvariable=var,
        fg=fg,
        readonlybackground=bg,
        font=font or F_VAL,
        relief="flat", bd=0,
        highlightthickness=0,
        state="readonly",
        cursor="xterm",
        justify=justify,
    )
    if width:
        e.config(width=width)
    return e


# ---------------------------------------------------------------------------
# Canvas sparkline widget
# ---------------------------------------------------------------------------

def _nice_ticks(lo, hi, n=3):
    """Return n evenly-spaced round tick values covering [lo, hi]."""
    span = hi - lo or 1
    raw_step = span / n
    mag = 10 ** math.floor(math.log10(raw_step)) if raw_step > 0 else 1
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if span / step <= n + 1:
            break
    start = math.ceil(lo / step) * step if step else lo
    ticks = []
    v = start
    while v <= hi + step * 0.01:
        ticks.append(v)
        v += step
    return ticks


class Sparkline(tk.Canvas):
    def __init__(self, parent, maxlen=HISTORY_LEN, color=BLUE, height=56,
                 unit="", fmt="{:.1f}", y_init=None, **kw):
        super().__init__(parent, height=height, bg=CARD, highlightthickness=0, **kw)
        self.color = color
        self.unit = unit
        self.fmt = fmt
        self.y_init = y_init   # (lo, hi) preset range; used until buffer is full
        self.data: deque = deque(maxlen=maxlen)
        self.bind("<Configure>", lambda _: self._draw())

    def push(self, value):
        self.data.append(value)
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 2 or len(self.data) < 2:
            return
        vals = list(self.data)
        # Use preset range until the buffer fills, then auto-scale
        if self.y_init and len(self.data) < self.data.maxlen:
            lo, hi = self.y_init
        else:
            lo, hi = min(vals), max(vals)
            # Enforce a minimum visible range so a stable signal (e.g. SNR ≈ 19.2 dB)
            # doesn't appear as a frozen flat line.
            min_span = max(abs((lo + hi) / 2) * 0.10, 1.0)
            if hi - lo < min_span:
                mid = (lo + hi) / 2
                lo, hi = mid - min_span / 2, mid + min_span / 2
        ticks = _nice_ticks(lo, hi, n=2)

        LMARGIN = 42
        BOTTOM = 14   # room for X-axis labels
        TOP = 4
        plot_w = w - LMARGIN - 4
        plot_h = h - TOP - BOTTOM

        def to_y(v):
            return (h - BOTTOM) - (v - lo) / (hi - lo) * plot_h

        def to_x(i, n):
            return LMARGIN + i * plot_w / max(n - 1, 1)

        # Y gridlines + labels
        for tick in ticks:
            y = to_y(tick)
            self.create_line(LMARGIN, y, w - 4, y, fill=BORDER, dash=(2, 4))
            self.create_text(LMARGIN - 4, y, text=self.fmt.format(tick),
                             fill=DIM, font=F_TINY, anchor="e")

        # Axes
        self.create_line(LMARGIN, TOP, LMARGIN, h - BOTTOM, fill=BORDER)
        self.create_line(LMARGIN, h - BOTTOM, w - 4, h - BOTTOM, fill=BORDER)

        # X-axis time labels: show age of leftmost point and midpoint
        n_pts = len(vals)
        total_s = n_pts * POLL_INTERVAL
        for frac, anchor in ((0.0, "w"), (0.5, "center"), (1.0, "e")):
            age_s = total_s * (1.0 - frac)
            if age_s < 60:
                label = f"-{int(age_s)}s" if age_s > 0 else "now"
            else:
                label = f"-{int(age_s/60)}m" if age_s > 0 else "now"
            x = LMARGIN + frac * plot_w
            self.create_text(x, h - BOTTOM + 3, text=label, fill=DIM,
                             font=F_TINY, anchor="n")

        # Data line
        xs = [to_x(i, n_pts) for i in range(n_pts)]
        ys = [to_y(v) for v in vals]
        pts = []
        for x, y in zip(xs, ys):
            pts += [x, y]
        self.create_line(*pts, fill=self.color, width=2, smooth=True)


# ---------------------------------------------------------------------------
# Reusable card widgets
# ---------------------------------------------------------------------------

def make_card(parent, title, colspan=1, rowspan=1):
    frame = tk.Frame(parent, bg=CARD, bd=0, highlightthickness=1,
                     highlightbackground=BORDER)
    lbl = tk.Label(frame, text=title.upper(), bg=CARD, fg=TEXT,
                   font=F_CARD, anchor="w", padx=8, pady=6)
    lbl.pack(fill="x")
    return frame


class MetricCard:
    """Big number + unit + optional colour threshold + sparkline."""
    def __init__(self, parent, title, unit="", fmt="{:.1f}",
                 low_good=False, warn=None, crit=None, spark_color=BLUE,
                 spark_y_init=None):
        self.frame = make_card(parent, title)
        self.unit = unit
        self.fmt = fmt
        self.low_good = low_good
        self.warn = warn
        self.crit = crit

        self.val_var = tk.StringVar(value="--")
        self.val_lbl = tk.Label(self.frame, textvariable=self.val_var,
                                bg=CARD, fg=TEXT, font=F_BIG, anchor="center")
        self.val_lbl.pack(fill="x", padx=8)
        # click-to-copy on the big number
        def _copy_metric(_, v=self.val_var, w=self.val_lbl):
            w.clipboard_clear(); w.clipboard_append(v.get())
            orig = w.cget("fg"); w.config(fg=DIM)
            w.after(200, lambda: w.config(fg=orig))
        self.val_lbl.bind("<Button-1>", _copy_metric)
        self.val_lbl.config(cursor="hand2")

        self.unit_lbl = tk.Label(self.frame, text=unit, bg=CARD, fg=TEXT,
                                 font=F_VAL, anchor="center")
        self.unit_lbl.pack(fill="x")

        self.spark = Sparkline(self.frame, color=spark_color, height=56,
                               unit=unit, fmt=fmt, y_init=spark_y_init)
        self.spark.pack(fill="x", padx=4, pady=4)

    def update(self, value):
        if value is None:
            self.val_var.set("--")
            return
        self.val_var.set(self.fmt.format(value))
        self.spark.push(value)

        color = GREEN
        if self.crit is not None:
            if self.low_good:
                if value >= self.crit:
                    color = RED
                elif value >= self.warn:
                    color = YELLOW
            else:
                if value <= self.crit:
                    color = RED
                elif self.warn is not None and value <= self.warn:
                    color = YELLOW
        self.val_lbl.configure(fg=color)


class StatusPanel:
    """Shows dish status flags and pointing info derived from confirmed fields."""
    def __init__(self, parent):
        self.frame = make_card(parent, "Status")
        self.rows = {}
        for key in ["Obstr. Events", "Obstr. Map", "Ethernet",
                    "Elevation", "Azimuth", "SNR", "Uptime", "Firmware"]:
            row = tk.Frame(self.frame, bg=CARD)
            row.pack(fill="x", padx=8, pady=1)
            tk.Label(row, text=f"{key}:", bg=CARD, fg=DIM,
                     font=F_KEY, width=14, anchor="w").pack(side="left")
            var = tk.StringVar(value="--")
            e = copyable_label(row, var)
            e.pack(side="left", fill="x", expand=True)
            self.rows[key] = (var, e)

    def set(self, key, value, color=None):
        if key in self.rows:
            var, e = self.rows[key]
            var.set(str(value))
            if color:
                e.configure(fg=color)


class InfoPanel:
    def __init__(self, parent):
        self.frame = make_card(parent, "Dish Info")
        self.rows = {}
        self._entries = {}
        for key in ["ID", "Hardware", "Firmware", "Uptime", "Usage", "Tilt"]:
            row = tk.Frame(self.frame, bg=CARD)
            row.pack(fill="x", padx=8, pady=1)
            tk.Label(row, text=f"{key}:", bg=CARD, fg=DIM,
                     font=F_KEY, width=10, anchor="w").pack(side="left")
            var = tk.StringVar(value="--")
            e = copyable_label(row, var)
            e.pack(side="left", fill="x", expand=True)
            self.rows[key] = var
            self._entries[key] = e
        # Firmware-mismatch alert — empty (hidden) until a mismatch is seen
        self._fw_alert_var = tk.StringVar(value="")
        tk.Label(self.frame, textvariable=self._fw_alert_var, bg=CARD, fg=YELLOW,
                 font=F_TINY, anchor="w", wraplength=250,
                 justify="left").pack(fill="x", padx=8, pady=(2, 4))

    def set(self, key, value):
        if key in self.rows:
            self.rows[key].set(str(value))

    def set_firmware(self, version, known):
        """Set the firmware value and flag a mismatch against the verified build."""
        self.rows["Firmware"].set(version or "--")
        if version == known:
            self._entries["Firmware"].configure(fg=GREEN)
            self._fw_alert_var.set("")
        else:
            self._entries["Firmware"].configure(fg=ORANGE)
            self._fw_alert_var.set(
                f"⚠ Firmware differs from verified {known}. "
                "Readings may be inaccurate if field numbers changed; "
                "dashboard continues running.")


class StatusBar:
    def __init__(self, parent):
        self.frame = tk.Frame(parent, bg="#0a0f16", height=22)
        self.frame.pack(fill="x", side="bottom")
        self.left = tk.Label(self.frame, bg="#0a0f16", fg=DIM,
                             font=F_SMALL, anchor="w", padx=8)
        self.left.pack(side="left")
        self.right = tk.Label(self.frame, bg="#0a0f16", fg=DIM,
                              font=F_SMALL, anchor="e", padx=8)
        self.right.pack(side="right")

    def update(self, ok, msg=""):
        if ok:
            self.left.configure(text=f"● Connected  {msg}", fg=GREEN)
        else:
            self.left.configure(text=f"● {msg}", fg=RED)
        self.right.configure(text=time.strftime("%H:%M:%S"))


# ---------------------------------------------------------------------------
# IP geolocation (best-effort — Starlink does not expose GPS via gRPC)
# ---------------------------------------------------------------------------

def fetch_geolocation():
    """Returns dict with lat, lon, city, region, country, isp or raises."""
    import urllib.request, json
    with urllib.request.urlopen("http://ip-api.com/json?fields=lat,lon,city,regionName,country,isp,query", timeout=6) as r:
        return json.loads(r.read())


class SectorChart(tk.Canvas):
    """Ring bar chart for per-sector signal quality (10 sectors)."""
    SIZE = 200

    def __init__(self, parent):
        super().__init__(parent, width=self.SIZE, height=self.SIZE,
                         bg=CARD, highlightthickness=0)
        self._draw([])

    def _draw(self, values):
        self.delete("all")
        cx = cy = self.SIZE // 2
        r_outer = cx - 12
        r_inner = cx - 45

        n = 10
        gap_deg = 4
        sector_deg = (360 / n) - gap_deg

        lo, hi = 20, 50  # expected dB range for display

        for i, val in enumerate(values[:n]):
            start = 270 + i * (360 / n) - sector_deg / 2
            frac = max(0, min(1, (val - lo) / (hi - lo)))
            bar_r = r_inner + frac * (r_outer - r_inner)
            color = GREEN if frac > 0.6 else (YELLOW if frac > 0.3 else RED)

            # Background track
            self.create_arc(cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer,
                            start=start, extent=sector_deg,
                            fill=BORDER, outline="")
            self.create_arc(cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner,
                            start=start, extent=sector_deg,
                            fill=CARD, outline="")

            # Value fill
            self.create_arc(cx - bar_r, cy - bar_r, cx + bar_r, cy + bar_r,
                            start=start, extent=sector_deg,
                            fill=color, outline="")
            self.create_arc(cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner,
                            start=start, extent=sector_deg,
                            fill=CARD, outline="")

            # Sector label
            label_r = r_outer + 10
            angle_rad = math.radians(start + sector_deg / 2)
            lx = cx + label_r * math.cos(angle_rad)
            ly = cy - label_r * math.sin(angle_rad)
            self.create_text(lx, ly, text=str(val),
                             fill=TEXT, font=("Consolas", 7), anchor="center")

        if not values:
            self.create_text(cx, cy, text="No Data", fill=DIM, font=("Consolas", 9))
        else:
            self.create_text(cx, cy - 8, text="Map",
                             fill=TEXT, font=("Consolas", 9, "bold"))
            self.create_text(cx, cy + 8, text="per sector",
                             fill=DIM, font=("Consolas", 8))

    def update(self, sector_signal_msg):
        vals = [getattr(sector_signal_msg, f"s{i}", 0) for i in range(1, 11)]
        if any(v > 0 for v in vals):
            self._draw(vals)


class ReadyStatesPanel:
    # (attr, acronym, plain-language description). These are the dish's internal,
    # undocumented subsystem bring-up flags; descriptions are best-effort.
    _FLAGS = [
        ("cady", "CADY",  "Modem (Cady ASIC)"),
        ("scp",  "SCP",   "System control proc."),
        ("l1l2", "L1/L2", "Link layers 1 & 2"),
        ("xphy", "XPHY",  "PHY / RF subsystem"),
        ("aap",  "AAP",   "App / access layer"),
    ]

    def __init__(self, parent):
        self.frame = make_card(parent, "Ready States")
        tk.Label(self.frame,
                 text="Dish subsystem bring-up — all green = fully operational",
                 bg=CARD, fg=DIM, font=F_TINY, anchor="w",
                 wraplength=260, justify="left").pack(fill="x", padx=8, pady=(0, 4))
        self._rows = {}
        for attr, acro, desc in self._FLAGS:
            row = tk.Frame(self.frame, bg=CARD)
            row.pack(fill="x", padx=8, pady=1)
            dot = tk.Label(row, text="●", bg=CARD, fg=DIM, font=F_VAL)
            dot.pack(side="left")
            tk.Label(row, text=acro, bg=CARD, fg=TEXT, font=F_SMALL_BOLD,
                     width=6, anchor="w").pack(side="left", padx=(4, 2))
            tk.Label(row, text=desc, bg=CARD, fg=DIM, font=F_SMALL,
                     anchor="w").pack(side="left")
            status = tk.Label(row, text="--", bg=CARD, fg=DIM,
                              font=F_SMALL, anchor="e")
            status.pack(side="right")
            self._rows[attr] = (dot, status)

    def update(self, ready_states_msg):
        for attr, _, _ in self._FLAGS:
            ok = getattr(ready_states_msg, attr, False)
            dot, status = self._rows[attr]
            dot.configure(fg=GREEN if ok else RED)
            status.configure(text="Ready" if ok else "Down",
                             fg=GREEN if ok else RED)


class DetailInfoPanel:
    """Shows extended fields for the detail window."""
    def __init__(self, parent):
        self.frame = make_card(parent, "Extended Info")
        self.rows = {}
        keys = ["Country", "GPS Valid", "GPS Accuracy",
                "Sec. Elevation", "Sec. Azimuth", "Obstr. Events", "Likely Sat",
                "Dish ID", "Router ID", "Dish Clock"]
        for key in keys:
            row = tk.Frame(self.frame, bg=CARD)
            row.pack(fill="x", padx=8, pady=1)
            tk.Label(row, text=f"{key}:", bg=CARD, fg=DIM,
                     font=F_KEY, width=16, anchor="w").pack(side="left")
            var = tk.StringVar(value="--")
            e = copyable_label(row, var)
            e.pack(side="left", fill="x", expand=True)
            self.rows[key] = (var, e)

    def set(self, key, value, color=None):
        if key in self.rows:
            var, e = self.rows[key]
            var.set(str(value))
            e.configure(fg=color or TEXT)


# ---------------------------------------------------------------------------
# CSV data logger
# ---------------------------------------------------------------------------

LOG_FIELDS = [
    "timestamp_utc",
    "dl_mbps", "ul_mbps", "latency_ms", "drop_pct",
    "snr_db", "boresight_el_deg", "boresight_az_deg", "tilt_deg",
    "obstr_events", "eth_mbps", "uptime_s",
    "dish_gps_valid", "dish_gps_accuracy_m", "align_metric_f7",
    "gps_lat", "gps_lon", "gps_sats", "gps_quality",
    "firmware", "country",
    "cum_dl_gb", "cum_ul_gb",
    "likely_sat", "likely_sat_sep_deg",
    # Per-sector map values (field 1028) — logged to study long-term behaviour
    "sector1", "sector2", "sector3", "sector4", "sector5",
    "sector6", "sector7", "sector8", "sector9", "sector10",
]


class DataLogger:
    """Appends one CSV row per poll. Rotates to a new file at the UTC day boundary."""

    DATA_DIR = Path(__file__).parent / "data"

    def __init__(self):
        self.DATA_DIR.mkdir(exist_ok=True)
        self._date = None
        self._fh   = None
        self._csv  = None

    @staticmethod
    def _header_matches(path):
        """True if the file's first row equals the current LOG_FIELDS."""
        try:
            with open(path, newline="", encoding="utf-8") as f:
                return next(csv.reader(f), None) == LOG_FIELDS
        except Exception:
            return False

    def _rotate(self):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        if today == self._date and self._fh:
            return
        if self._fh:
            self._fh.close()
        self._date = today
        # Find a day-file that's either new or already uses the current schema, so
        # a column change (e.g. added likely_sat) never mixes widths within a file.
        base = f"starlink_{today.isoformat()}"
        path = self.DATA_DIR / f"{base}.csv"
        suffix = 1
        while path.exists() and not self._header_matches(path):
            suffix += 1
            path = self.DATA_DIR / f"{base}_{suffix}.csv"
        write_header = not path.exists()
        self._fh  = open(path, "a", newline="", encoding="utf-8")
        self._csv = csv.writer(self._fh)
        if write_header:
            self._csv.writerow(LOG_FIELDS)
        self._fh.flush()

    def log(self, row: dict):
        self._rotate()
        self._csv.writerow([row.get(f, "") for f in LOG_FIELDS])
        self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


# ---------------------------------------------------------------------------
# Optional "Likely satellite" estimate via TLE look-angle matching
# ---------------------------------------------------------------------------
#
# The local gRPC API never exposes which satellite the dish is talking to, so
# this is an *estimate*: download the public Starlink TLE catalog from CelesTrak,
# propagate every satellite with SGP4 to "now", convert each to a topocentric
# az/el as seen from the dish, and report whichever sits closest to the dish's
# reported boresight. Beam handoffs happen every ~15 s and several satellites can
# share a look-angle, so treat the result as a best-guess, not ground truth.
#
# Dependencies (sgp4 + numpy) are imported lazily; if missing, the feature stays
# disabled with a helpful message instead of breaking the dashboard.

class SatelliteMatcher:
    TLE_URL = ("https://celestrak.org/NORAD/elements/gp.php"
               "?GROUP=starlink&FORMAT=tle")
    CACHE   = Path(__file__).parent / "data" / "starlink_tle.txt"
    REFRESH_H = 8       # re-download TLEs if cache older than this
    MIN_EL    = 10.0    # ignore satellites below this elevation (deg)

    def __init__(self):
        self._array = None     # sgp4 SatrecArray
        self._names = None
        self._np = None

    # -- public ---------------------------------------------------------
    def load(self):
        """Download/refresh the TLE cache and build the propagation array.
        Returns (ok: bool, message: str). Safe to call from a worker thread."""
        try:
            import numpy as np
            from sgp4.api import Satrec, SatrecArray
        except ImportError:
            return False, "needs 'pip install sgp4 numpy'"
        try:
            self._refresh_cache()
        except Exception as e:
            if not self.CACHE.exists():
                return False, f"TLE download failed: {e}"
            # fall back to stale cache rather than failing outright
        names, recs = [], []
        lines = [l.rstrip() for l in
                 self.CACHE.read_text(encoding="utf-8").splitlines() if l.strip()]
        i = 0
        while i + 2 < len(lines) + 1 and i + 2 <= len(lines) - 1:
            name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
            if l1.startswith("1 ") and l2.startswith("2 "):
                try:
                    recs.append(Satrec.twoline2rv(l1, l2))
                    names.append(name.strip())
                except Exception:
                    pass
                i += 3
            else:
                i += 1
        if not recs:
            return False, "no TLEs parsed"
        self._np = np
        self._names = names
        self._array = SatrecArray(recs)
        return True, f"{len(recs)} satellites"

    def match(self, lat_deg, lon_deg, az_deg, el_deg, alt_m=0.0):
        """Return (name, separation_deg, sat_az, sat_el) of the closest satellite,
        or None. Vectorised SGP4 over the whole catalogue (~tens of ms)."""
        if self._array is None:
            return None
        np = self._np
        from sgp4.api import jday
        now = datetime.datetime.now(datetime.timezone.utc)
        jd, fr = jday(now.year, now.month, now.day,
                      now.hour, now.minute, now.second + now.microsecond * 1e-6)
        err, r, _v = self._array.sgp4(np.array([jd]), np.array([fr]))
        r = r[:, 0, :] * 1000.0          # km -> m, TEME frame
        good = err[:, 0] == 0

        # Rotate TEME -> ECEF about Z by GMST (sub-0.1° accuracy, ample for ~1° matching)
        theta = self._gmst_rad(jd + fr)
        cT, sT = math.cos(theta), math.sin(theta)
        x, y, z = r[:, 0], r[:, 1], r[:, 2]
        xe = cT * x + sT * y
        ye = -sT * x + cT * y
        ze = z

        ox, oy, oz, e_hat, n_hat, u_hat = self._observer_ecef(lat_deg, lon_deg, alt_m)
        dx, dy, dz = xe - ox, ye - oy, ze - oz
        E = dx * e_hat[0] + dy * e_hat[1] + dz * e_hat[2]
        N = dx * n_hat[0] + dy * n_hat[1] + dz * n_hat[2]
        U = dx * u_hat[0] + dy * u_hat[1] + dz * u_hat[2]
        el = np.degrees(np.arctan2(U, np.hypot(E, N)))
        az = np.degrees(np.arctan2(E, N)) % 360.0

        # Angular separation between each satellite and the dish boresight
        el_b, az_b = math.radians(el_deg), math.radians(az_deg)
        el_r, az_r = np.radians(el), np.radians(az)
        cos_sep = (np.sin(el_r) * math.sin(el_b) +
                   np.cos(el_r) * math.cos(el_b) * np.cos(az_r - az_b))
        sep = np.degrees(np.arccos(np.clip(cos_sep, -1.0, 1.0)))

        mask = good & (el > self.MIN_EL)
        if not mask.any():
            return None
        sep_masked = np.where(mask, sep, 1e9)
        idx = int(np.argmin(sep_masked))
        return (self._names[idx], float(sep[idx]), float(az[idx]), float(el[idx]))

    def snapshot(self, lat_deg, lon_deg, alt_m=0.0, min_el=0.0):
        """For the current instant, return per-satellite arrays for the sky map:
        dict(name=[...], az=[...], el=[...], sublat=[...], sublon=[...]) for every
        satellite above min_el as seen from the dish. Vectorised; ~tens of ms."""
        if self._array is None:
            return None
        np = self._np
        from sgp4.api import jday
        now = datetime.datetime.now(datetime.timezone.utc)
        jd, fr = jday(now.year, now.month, now.day,
                      now.hour, now.minute, now.second + now.microsecond * 1e-6)
        err, r, _v = self._array.sgp4(np.array([jd]), np.array([fr]))
        r = r[:, 0, :] * 1000.0
        good = err[:, 0] == 0

        theta = self._gmst_rad(jd + fr)
        cT, sT = math.cos(theta), math.sin(theta)
        x, y, z = r[:, 0], r[:, 1], r[:, 2]
        xe = cT * x + sT * y          # ECEF (m)
        ye = -sT * x + cT * y
        ze = z

        # topocentric az/el from the dish
        ox, oy, oz, e_hat, n_hat, u_hat = self._observer_ecef(lat_deg, lon_deg, alt_m)
        dx, dy, dz = xe - ox, ye - oy, ze - oz
        E = dx * e_hat[0] + dy * e_hat[1] + dz * e_hat[2]
        N = dx * n_hat[0] + dy * n_hat[1] + dz * n_hat[2]
        U = dx * u_hat[0] + dy * u_hat[1] + dz * u_hat[2]
        el = np.degrees(np.arctan2(U, np.hypot(E, N)))
        az = np.degrees(np.arctan2(E, N)) % 360.0

        # sub-satellite geodetic lat/lon (Bowring) from ECEF
        a, f = 6378137.0, 1 / 298.257223563
        b = a * (1 - f)
        e2 = f * (2 - f)
        ep2 = (a * a - b * b) / (b * b)
        p = np.hypot(xe, ye)
        th = np.arctan2(ze * a, p * b)
        sublat = np.degrees(np.arctan2(ze + ep2 * b * np.sin(th) ** 3,
                                       p - e2 * a * np.cos(th) ** 3))
        sublon = np.degrees(np.arctan2(ye, xe))

        mask = good & (el > min_el)
        idx = np.nonzero(mask)[0]
        return {
            "name":   [self._names[i] for i in idx],
            "az":     az[idx].tolist(),
            "el":     el[idx].tolist(),
            "sublat": sublat[idx].tolist(),
            "sublon": sublon[idx].tolist(),
        }

    # -- internals ------------------------------------------------------
    def _refresh_cache(self):
        self.CACHE.parent.mkdir(exist_ok=True)
        if (self.CACHE.exists() and
                time.time() - self.CACHE.stat().st_mtime < self.REFRESH_H * 3600):
            return
        import urllib.request
        req = urllib.request.Request(
            self.TLE_URL, headers={"User-Agent": "starlink-dashboard"})
        data = urllib.request.urlopen(req, timeout=30).read().decode()
        if len(data) < 100 or "\n1 " not in ("\n" + data):
            raise RuntimeError("empty/invalid TLE response")
        self.CACHE.write_text(data, encoding="utf-8")

    @staticmethod
    def _observer_ecef(lat_deg, lon_deg, h):
        lat, lon = math.radians(lat_deg), math.radians(lon_deg)
        a, f = 6378137.0, 1 / 298.257223563
        e2 = f * (2 - f)
        sL, cL = math.sin(lat), math.cos(lat)
        sO, cO = math.sin(lon), math.cos(lon)
        N = a / math.sqrt(1 - e2 * sL * sL)
        ox = (N + h) * cL * cO
        oy = (N + h) * cL * sO
        oz = (N * (1 - e2) + h) * sL
        e_hat = (-sO, cO, 0.0)
        n_hat = (-sL * cO, -sL * sO, cL)
        u_hat = (cL * cO, cL * sO, sL)
        return ox, oy, oz, e_hat, n_hat, u_hat

    @staticmethod
    def _gmst_rad(jd_ut1):
        T = (jd_ut1 - 2451545.0) / 36525.0
        sec = (67310.54841 + (876600 * 3600 + 8640184.812866) * T
               + 0.093104 * T * T - 6.2e-6 * T * T * T)
        return math.radians((sec % 86400.0) / 240.0 % 360.0)


class BorderMap:
    """Fetches & caches a world admin-1 (states/provinces) GeoJSON and parses it
    into outline rings (coastlines + state/country borders) for the sky map."""
    URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
           "master/geojson/ne_50m_admin_1_states_provinces.geojson")
    CACHE = Path(__file__).parent / "data" / "geo_borders.json"
    REFRESH_DAYS = 30

    def __init__(self):
        self.rings = None   # list of (lons, lats, bbox=(minlon,minlat,maxlon,maxlat))

    def load(self):
        """Download/cache and parse. Returns (ok, message). Worker-thread safe."""
        import json
        try:
            self._refresh()
        except Exception as e:
            if not self.CACHE.exists():
                return False, f"map download failed: {e}"
        try:
            gj = json.loads(self.CACHE.read_text(encoding="utf-8"))
        except Exception as e:
            return False, f"map parse failed: {e}"
        rings = []
        for feat in gj.get("features", []):
            geom = feat.get("geometry") or {}
            t, coords = geom.get("type"), geom.get("coordinates")
            polys = [coords] if t == "Polygon" else (coords if t == "MultiPolygon" else [])
            for poly in polys:
                for ring in poly:                       # exterior + holes
                    lons = [p[0] for p in ring if len(p) >= 2]
                    lats = [p[1] for p in ring if len(p) >= 2]
                    if len(lons) >= 2:
                        rings.append((lons, lats,
                                      (min(lons), min(lats), max(lons), max(lats))))
        if not rings:
            return False, "no border rings parsed"
        self.rings = rings
        return True, f"{len(rings)} border rings"

    def _refresh(self):
        self.CACHE.parent.mkdir(exist_ok=True)
        if (self.CACHE.exists() and
                time.time() - self.CACHE.stat().st_mtime < self.REFRESH_DAYS * 86400):
            return
        import urllib.request
        req = urllib.request.Request(self.URL, headers={"User-Agent": "starlink-dashboard"})
        data = urllib.request.urlopen(req, timeout=60).read()
        if len(data) < 1000:
            raise RuntimeError("empty geojson")
        self.CACHE.write_bytes(data)


class SkyMapPanel:
    """Top-down, dish-centred sky map: coastline/state/country borders, a fixed-scale
    view with a reference ring, all Starlink sub-satellite points (re-propagated every
    poll so they move), and the likely satellite highlighted with a line to the dish."""
    VIEW_HALF_KM = 450.0   # fixed left/right half-range (centre -> left/right edge)
    RING_KM      = 200.0   # reference ring radius

    def __init__(self, parent, sat_enabled_var, on_toggle, status_var):
        self.frame = make_card(parent, "Satellite Sky Map")
        ctl = tk.Frame(self.frame, bg=CARD)
        ctl.pack(fill="x", padx=8)
        tk.Checkbutton(ctl, text="Track satellites (TLE)", variable=sat_enabled_var,
                       command=on_toggle, bg=CARD, fg=DIM, selectcolor=BG,
                       activebackground=CARD, activeforeground=TEXT, font=F_SMALL,
                       bd=0, highlightthickness=0, cursor="hand2").pack(side="left")
        tk.Label(ctl, textvariable=status_var, bg=CARD, fg=DIM, font=F_TINY,
                 anchor="e").pack(side="right")
        self.canvas = tk.Canvas(self.frame, bg="#0a0f16", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.borders = None
        self._last = None
        self.canvas.bind("<Configure>", lambda e: self._redraw())

    def set_borders(self, rings):
        self.borders = rings
        self._redraw()

    def update(self, dish_lat, dish_lon, snap, likely_name, likely_sep, baz, bel):
        self._last = (dish_lat, dish_lon, snap, likely_name, likely_sep, baz, bel)
        self._redraw()

    @staticmethod
    def _gc_km(lat1, lon1, lat2, lon2):
        dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) *
             math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
        return 6371.0 * 2 * math.asin(math.sqrt(a))

    def _redraw(self):
        # Contain any drawing error: a bad frame shows a note and is logged, but
        # never propagates to take the app down.
        try:
            self._redraw_impl()
        except Exception:
            import traceback
            traceback.print_exc()
            try:
                self.canvas.delete("all")
                self.canvas.create_text(8, 8, anchor="nw", fill=RED, font=F_TINY,
                                         text="sky map: draw error (logged)")
            except Exception:
                pass

    def _redraw_impl(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return
        if not self._last:
            c.create_text(w // 2, h // 2, text="Acquiring satellites…",
                          fill=DIM, font=F_SMALL)
            return
        dish_lat, dish_lon, snap, likely, sep, baz, bel = self._last
        if dish_lat is None:
            c.create_text(w // 2, h // 2, text="Need a dish GPS fix for the sky map",
                          fill=DIM, font=F_SMALL)
            return

        cx, cy = w / 2.0, h / 2.0
        margin = 16
        # Fixed scale: the left/right edges are VIEW_HALF_KM from the dish, so the
        # zoom never changes with the satellite geometry.
        ppk = (w / 2.0 - margin) / self.VIEW_HALF_KM
        li = snap["name"].index(likely) if (snap and likely in snap["name"]) else -1
        coslat = max(0.1, math.cos(math.radians(dish_lat)))

        def proj(lat, lon):
            north = (lat - dish_lat) * 111.32
            east = (lon - dish_lon) * 111.32 * coslat
            x = cx + east * ppk
            y = cy - north * ppk
            # Guard against non-finite or absurd coordinates, which can throw a
            # TclError deep in the canvas; off-canvas points clip harmlessly.
            if not (math.isfinite(x) and math.isfinite(y)):
                return -1.0e4, -1.0e4
            return max(-3.2e4, min(3.2e4, x)), max(-3.2e4, min(3.2e4, y))

        # visible half-extents (km) -> view bbox for border/graticule culling
        half_w_km = self.VIEW_HALF_KM
        half_h_km = (h / 2.0 - margin) / ppk
        dlat = half_h_km / 111.32
        dlon = half_w_km / (111.32 * coslat)
        vb = (dish_lon - dlon, dish_lat - dlat, dish_lon + dlon, dish_lat + dlat)

        # --- borders / coastlines (only rings overlapping the view) ---
        if self.borders:
            for lons, lats, bbox in self.borders:
                if bbox[2] < vb[0] or bbox[0] > vb[2] or bbox[3] < vb[1] or bbox[1] > vb[3]:
                    continue
                flat = []
                for lon, lat in zip(lons, lats):
                    x, y = proj(lat, lon)
                    flat.extend((x, y))
                if len(flat) >= 4:
                    c.create_line(*flat, fill="#2c3a4a", width=1)

        # --- lat/lon graticule ---
        step = 2 if (vb[2] - vb[0]) > 8 else 1
        lo = math.floor(vb[0])
        while lo <= math.ceil(vb[2]):
            x0, y0 = proj(vb[1], lo); x1, y1 = proj(vb[3], lo)
            c.create_line(x0, y0, x1, y1, fill="#172029")
            lo += step
        la = math.floor(vb[1])
        while la <= math.ceil(vb[3]):
            x0, y0 = proj(la, vb[0]); x1, y1 = proj(la, vb[2])
            c.create_line(x0, y0, x1, y1, fill="#172029")
            la += step

        # --- reference ring ---
        rp = self.RING_KM * ppk
        c.create_oval(cx - rp, cy - rp, cx + rp, cy + rp, outline=BLUE, dash=(3, 3))
        c.create_text(cx + rp * 0.71, cy - rp * 0.71 - 6,
                      text=f"{self.RING_KM:.0f} km", fill=BLUE, font=F_TINY)

        # --- boresight direction (extend well past the edge; tk clips it) ---
        if baz is not None:
            reach = math.hypot(w, h)
            bx = cx + math.sin(math.radians(baz)) * reach
            by = cy - math.cos(math.radians(baz)) * reach
            c.create_line(cx, cy, bx, by, fill=ORANGE, dash=(2, 4))

        # --- satellites ---
        n_in = 0
        if snap:
            for k in range(len(snap["name"])):
                if k == li:
                    continue
                x, y = proj(snap["sublat"][k], snap["sublon"][k])
                if -20 <= x <= w + 20 and -20 <= y <= h + 20:
                    n_in += 1
                    c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=TEAL, outline="")

        # --- likely satellite highlight (drawn last, on top) ---
        if li >= 0:
            x, y = proj(snap["sublat"][li], snap["sublon"][li])
            c.create_line(cx, cy, x, y, fill=YELLOW, width=2)
            c.create_oval(x - 6, y - 6, x + 6, y + 6, outline=YELLOW, width=2, fill=RED)
            tag = likely if not sep else f"{likely}  Δ{sep}°"
            c.create_text(x, y - 12, text=tag, fill=YELLOW, font=F_SMALL)
            n_in += 1

        # --- dish marker ---
        c.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=GREEN, outline=BG)
        c.create_text(cx, cy + 11, text="DISH", fill=GREEN, font=F_TINY)

        # --- north indicator (map is north-up) ---
        nx, ny = w - 20, 32
        c.create_line(nx, ny, nx, ny - 18, fill=TEXT, width=2,
                      arrow="last", arrowshape=(7, 9, 3))
        c.create_text(nx, ny - 27, text="N", fill=TEXT, font=F_SMALL)

        # --- info overlay ---
        info = f"{n_in} sats shown · {self.VIEW_HALF_KM:.0f} km L/R"
        if baz is not None:
            info += f" · boresight {baz:.0f}°az {bel:.0f}°el"
        c.create_text(8, 8, text=info, fill=DIM, font=F_TINY, anchor="nw")


LOCATION_FILE = Path(__file__).parent / "location.json"

def _load_config():
    try:
        import json
        d = json.loads(LOCATION_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _save_config(d):
    import json
    LOCATION_FILE.write_text(json.dumps(d, indent=2))

def load_saved_location():
    """Return (lat, lon, label) from disk, or (None, None, '')."""
    d = _load_config()
    return d.get("lat"), d.get("lon"), d.get("label", "")

def save_location(lat, lon, label=""):
    d = _load_config()
    d.update({"lat": lat, "lon": lon, "label": label})
    _save_config(d)

def load_gps_port():
    """Return persisted GPS port, or GPS_PORT default."""
    return _load_config().get("gps_port", GPS_PORT)

def save_gps_port(port):
    d = _load_config()
    d["gps_port"] = port
    _save_config(d)


class LocationPanel:
    """Two-column panel: ground station (IP-based) on left, user-set dish location on right."""

    def __init__(self, parent, on_set_location):
        self.frame = make_card(parent, "Location")
        self._on_set = on_set_location

        body = tk.Frame(self.frame, bg=CARD)
        body.pack(fill="both", expand=True, padx=4, pady=2)
        # Equal, fixed-share columns so neither side can grow into the other.
        body.columnconfigure(0, weight=1, uniform="loc")
        body.columnconfigure(1, weight=1, uniform="loc")

        # --- Ground station column ---
        gs_hdr = tk.Label(body, text="Ground (IP)", bg=CARD, fg=ORANGE,
                          font=F_SMALL, anchor="w")
        gs_hdr.grid(row=0, column=0, sticky="w", padx=6, pady=(2, 0))

        self._gs = {}
        gs_keys = [("Lat/Lon", "gs_latlon"), ("City", "gs_city"),
                   ("Region", "gs_region"), ("ISP", "gs_isp"), ("IP", "gs_ip")]
        for r, (label, key) in enumerate(gs_keys, start=1):
            tk.Label(body, text=f"{label}:", bg=CARD, fg=DIM,
                     font=F_SMALL, anchor="w").grid(
                         row=r, column=0, sticky="w", padx=(10, 2))
            var = tk.StringVar(value="--")
            # width-bounded so long values still clip-and-scroll rather than
            # spilling over the divider; the wide two-column panel fits full values
            e = copyable_label(body, var, font=F_SMALL, justify="right", width=18)
            e.grid(row=r, column=0, sticky="e", padx=(0, 6))
            self._gs[key] = var

        # --- Separator (only spans the two data columns, not the full-width controls) ---
        tk.Frame(body, bg=BORDER, width=1).grid(
            row=0, column=0, rowspan=6, sticky="nse", padx=2)

        # --- Dish location column ---
        self._dish_hdr_var = tk.StringVar(value="Dish (set)")
        dish_hdr = tk.Label(body, textvariable=self._dish_hdr_var, bg=CARD, fg=TEAL,
                            font=F_SMALL, anchor="w")
        dish_hdr.grid(row=0, column=1, sticky="w", padx=6, pady=(2, 0))

        # GPS status row
        self._gps_var = tk.StringVar(value="GPS: --")
        self._gps_label = tk.Label(body, textvariable=self._gps_var, bg=CARD, fg=DIM,
                                   font=F_SMALL, anchor="w")
        self._gps_label.grid(row=1, column=1, sticky="w", padx=(10, 2))

        self._dish = {}
        # Lat/Lon row
        tk.Label(body, text="Lat/Lon:", bg=CARD, fg=DIM,
                 font=F_SMALL, anchor="w").grid(row=2, column=1, sticky="w", padx=(10, 2))
        _ll_var = tk.StringVar(value="--")
        self._dish["dish_latlon"] = _ll_var
        copyable_label(body, _ll_var, fg=TEAL, font=F_SMALL, justify="right",
                       width=18).grid(row=2, column=1, sticky="e", padx=(0, 6))
        # Label row
        tk.Label(body, text="Label:", bg=CARD, fg=DIM,
                 font=F_SMALL, anchor="w").grid(row=3, column=1, sticky="w", padx=(10, 2))
        _lbl_var = tk.StringVar(value="--")
        self._dish["dish_label"] = _lbl_var
        copyable_label(body, _lbl_var, fg=TEAL, font=F_SMALL, justify="right",
                       width=18).grid(row=3, column=1, sticky="e", padx=(0, 6))

        # Distance row
        tk.Label(body, text="Distance:", bg=CARD, fg=DIM,
                 font=F_SMALL, anchor="w").grid(row=4, column=1, sticky="w", padx=(10, 2))
        self._dist_var = tk.StringVar(value="--")
        copyable_label(body, self._dist_var, fg=YELLOW,
                       font=F_SMALL_BOLD, width=24).grid(
            row=5, column=1, sticky="w", padx=(10, 2))

        # GPS port selector + buttons span the FULL panel width (both columns)
        # so the controls have room and never clip against the narrow columns.
        port_frame = tk.Frame(body, bg=CARD)
        port_frame.grid(row=6, column=0, columnspan=2, sticky="w", padx=8, pady=(5, 1))

        tk.Label(port_frame, text="GPS Port:", bg=CARD, fg=DIM,
                 font=F_SMALL).pack(side="left")

        self._port_var = tk.StringVar(value=GPS_PORT)
        self._port_combo = ttk.Combobox(
            port_frame, textvariable=self._port_var,
            width=7, font=F_SMALL, state="readonly")
        self._port_combo.pack(side="left", padx=(4, 0))
        self._port_combo.bind("<ButtonPress>", self._refresh_ports)

        btn_frame = tk.Frame(body, bg=CARD)
        btn_frame.grid(row=7, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))
        self._connect_btn = tk.Button(
            btn_frame, text="Connect GPS", command=self._on_connect_gps,
            bg=BORDER, fg=TEXT, font=F_SMALL,
            relief="flat", cursor="hand2", padx=6, pady=2)
        self._connect_btn.pack(side="left", padx=(0, 5))
        tk.Button(btn_frame, text="Set Manual…", command=self._on_set,
                  bg=BORDER, fg=TEXT, font=F_SMALL,
                  relief="flat", cursor="hand2", padx=6, pady=2).pack(side="left")

        # Live NMEA feed — raw serial sentences, 5 visible lines, auto-scrolling
        tk.Label(body, text="NMEA feed:", bg=CARD, fg=DIM, font=F_SMALL,
                 anchor="w").grid(row=8, column=0, columnspan=2,
                                  sticky="w", padx=8, pady=(2, 0))
        self._nmea_text = tk.Text(
            body, height=5, bg=BG, fg=TEAL, font=F_TINY, wrap="none",
            relief="flat", bd=0, highlightthickness=1, highlightbackground=BORDER,
            state="disabled", cursor="xterm")
        self._nmea_text.grid(row=9, column=0, columnspan=2, sticky="nsew",
                             padx=8, pady=(0, 6))
        body.rowconfigure(9, weight=1)

        self._gs_lat = None
        self._gs_lon = None
        self._dish_lat = None
        self._dish_lon = None
        self._on_connect_gps_cb = None  # set by Dashboard after construction

    def _refresh_ports(self, _event=None):
        ports = list_serial_ports()
        current = self._port_var.get()
        # Always keep the currently selected port in the list even if not detected
        if current and current not in ports:
            ports = [current] + ports
        if not ports:
            ports = [GPS_PORT]
        self._port_combo["values"] = ports
        if not self._port_var.get():
            self._port_var.set(ports[0])

    def _on_connect_gps(self):
        if self._on_connect_gps_cb:
            self._on_connect_gps_cb(self._port_var.get())

    def set_ground_station(self, lat, lon, city, region, isp, ip):
        self._gs_lat, self._gs_lon = lat, lon
        self._gs["gs_latlon"].set(f"{lat:.4f}, {lon:.4f}")
        self._gs["gs_city"].set(city)
        self._gs["gs_region"].set(region)
        self._gs["gs_isp"].set(isp)
        self._gs["gs_ip"].set(ip)
        self._update_distance()

    def set_gps_connecting(self, port):
        self._gps_var.set(f"GPS: connecting {port}…")
        self._gps_label.config(fg=DIM)

    def append_nmea(self, line):
        """Append one raw NMEA sentence to the feed box and auto-scroll (UI thread)."""
        t = self._nmea_text
        t.config(state="normal")
        t.insert("end", line + "\n")
        # cap the buffer so it cannot grow without bound
        if int(t.index("end-1c").split(".")[0]) > 200:
            t.delete("1.0", "2.0")
        t.see("end")
        t.config(state="disabled")

    def set_gps_status(self, lat, lon, quality, num_sats):
        """Called via root.after from the GPS reader thread."""
        sats_txt = f" ({num_sats} sats)" if num_sats else ""
        if quality == -1:
            self._gps_var.set("GPS: pyserial not installed")
            self._gps_label.config(fg=RED)
        elif quality == -2:
            self._gps_var.set("GPS: port unavailable")
            self._gps_label.config(fg=RED)
        elif quality == 0 or lat is None:
            # Distinguish between initial acquisition and re-acquisition after fix loss
            current = self._dish_hdr_var.get()
            if "GPS" in current and "set" not in current:
                self._gps_var.set(f"GPS: Reacquiring…{sats_txt}")
            else:
                self._gps_var.set(f"GPS: Acquiring…{sats_txt}")
            self._gps_label.config(fg=YELLOW)
        else:
            self._gps_var.set(f"GPS: Fixed{sats_txt}")
            self._gps_label.config(fg=GREEN)
            self._dish_hdr_var.set("Dish (GPS)")
            self.set_dish_location(lat, lon, "GPS Fix")

    def set_dish_location(self, lat, lon, label=""):
        self._dish_lat, self._dish_lon = lat, lon
        self._dish["dish_latlon"].set(f"{lat:.4f}, {lon:.4f}")
        self._dish["dish_label"].set(label or "")
        self._update_distance()

    def _update_distance(self):
        if None in (self._gs_lat, self._gs_lon, self._dish_lat, self._dish_lon):
            return
        km = _haversine_km(self._dish_lat, self._dish_lon,
                           self._gs_lat, self._gs_lon)
        self._dist_var.set(f"{km:.0f} km to ground station")


def list_serial_ports():
    """Return sorted list of available COM port names."""
    try:
        import serial.tools.list_ports
        ports = [p.device for p in serial.tools.list_ports.comports()]
        return sorted(ports, key=lambda s: int(s.replace("COM", "")) if s.startswith("COM") else 0)
    except Exception:
        return []


def _nmea_to_deg(value, hemi):
    """Convert NMEA DDDMM.MMMM + hemisphere char to signed decimal degrees."""
    if not value or not hemi:
        return None
    try:
        raw = float(value)
    except ValueError:
        return None
    deg = int(raw / 100)
    minutes = raw - deg * 100
    result = deg + minutes / 60.0
    if hemi in ("S", "W"):
        result = -result
    return result


class GpsReader:
    """Background thread that reads NMEA sentences from a serial port and parses position."""

    def __init__(self, port, baud, on_update, on_raw=None):
        self._on_update = on_update  # callable(lat_or_None, lon_or_None, quality, num_sats)
        self._on_raw = on_raw        # callable(raw_nmea_line) for the live feed display
        self._stop = threading.Event()
        self._sats_in_view = 0   # accumulated from GSV sentences
        self._thread = threading.Thread(
            target=self._run, args=(port, baud), daemon=True, name="gps-reader")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self, port, baud):
        try:
            import serial
        except ImportError:
            self._on_update(None, None, -1, 0)  # serial not installed
            return

        while not self._stop.is_set():
            try:
                with serial.Serial(port, baud, timeout=2) as ser:
                    while not self._stop.is_set():
                        try:
                            raw = ser.readline()
                            line = raw.decode("ascii", errors="ignore").strip()
                            if self._on_raw and line.startswith("$"):
                                self._on_raw(line)
                            self._parse(line)
                        except Exception:
                            pass
            except Exception:
                # Port unavailable — retry after a pause
                self._on_update(None, None, -2, 0)
                self._stop.wait(5)

    def _parse(self, line):
        if not line.startswith("$") or "*" not in line:
            return
        payload = line.split("*")[0]
        parts = payload.split(",")
        sentence = parts[0][1:]  # e.g. "GPGGA"

        if sentence in ("GPGGA", "GNGGA", "GLGGA"):
            # $xxGGA,time,lat,NS,lon,EW,quality,num_sats,...
            if len(parts) < 9:
                return
            quality = int(parts[6]) if parts[6].isdigit() else 0
            tracked = int(parts[7]) if parts[7].isdigit() else 0
            # prefer GSV in-view count when available; fall back to tracked count
            num_sats = self._sats_in_view if self._sats_in_view else tracked
            lat = _nmea_to_deg(parts[2], parts[3])
            lon = _nmea_to_deg(parts[4], parts[5])
            self._on_update(lat, lon, quality, num_sats)

        elif sentence in ("GPRMC", "GNRMC", "GLRMC"):
            # $xxRMC,time,status,lat,NS,lon,EW,...
            if len(parts) < 7:
                return
            status = parts[2]  # A=active, V=void
            lat = _nmea_to_deg(parts[3], parts[4])
            lon = _nmea_to_deg(parts[5], parts[6])
            quality = 1 if status == "A" else 0
            self._on_update(lat, lon, quality, self._sats_in_view)

        elif sentence.endswith("GSV"):
            # $xxGSV,numMsg,msgNum,numSVsInView,...
            # Accumulate total-in-view across all constellations; take the max seen
            if len(parts) >= 4 and parts[3].isdigit():
                count = int(parts[3])
                if count > self._sats_in_view:
                    self._sats_in_view = count


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Main Dashboard
# ---------------------------------------------------------------------------

class Dashboard:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Starlink Monitor")
        root.configure(bg=BG)
        root.geometry("1200x820")
        root.minsize(1000, 700)

        init_fonts(root)             # create scalable named fonts before any widgets
        self._build_ui()
        self._build_detail_window()
        # Live font scaling: rescale text as either window is resized (clamped range)
        self._font_scaler = FontScaler()
        attach_font_scaling(root, self._font_scaler, 1200, 760)
        attach_font_scaling(self._detail, self._font_scaler, 900, 680)
        self._client = None
        self._error_count = 0
        self._obstr_event_count = 0
        self._cum_dl_gb = 0.0   # cumulative download since dashboard start
        self._cum_ul_gb = 0.0   # cumulative upload since dashboard start
        self._obstr_last_event_time = None
        self._last_gps = {}   # latest GPS fix: lat, lon, sats, quality
        self._logger = DataLogger()
        # "Likely satellite" TLE matcher (detail window, on by default)
        self._sat_matcher = SatelliteMatcher()
        self._sat_on = False           # plain mirror of the tk checkbox (thread-safe read)
        self._sat_loaded = False
        self._last_boresight = None    # (el, az) from most recent status
        self._last_sat_match_t = 0.0
        self._last_sat_name = ""       # most recent likely-satellite match (for logging)
        self._last_sat_sep = ""
        self._border_map = BorderMap()  # coastline/state/country borders for the sky map
        threading.Thread(target=self._load_borders, daemon=True).start()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        threading.Thread(target=self._fetch_location, daemon=True).start()
        # Restore saved dish location if present (GPS will override when it gets a fix)
        lat, lon, label = load_saved_location()
        if lat is not None:
            self.location_panel.set_dish_location(lat, lon, label)
        # Start GPS reader; populate port combo with available ports
        self._gps_reader = None
        self.location_panel._on_connect_gps_cb = self._reconnect_gps
        self.location_panel._refresh_ports()
        saved_port = load_gps_port()
        self.location_panel._port_var.set(saved_port)
        self._reconnect_gps(saved_port)
        # Satellite estimate is on by default — kick off the TLE load now
        # (the checkbox command only fires on user clicks, not on initial value)
        if self._sat_enabled.get():
            self._on_toggle_sat()

    # ------------------------------------------------------------------
    # Optional "Likely satellite" TLE estimate
    # ------------------------------------------------------------------
    def _on_toggle_sat(self):
        self._sat_on = self._sat_enabled.get()
        if not self._sat_on:
            self._sat_status_var.set("")
            self.detail_info.set("Likely Sat", "--", DIM)
            return
        if self._sat_loaded:
            self._sat_status_var.set("Sat: matching…")
            self._last_sat_match_t = 0.0   # force a match on next poll
        else:
            self._sat_status_var.set("Sat: loading TLE catalogue…")
            threading.Thread(target=self._load_sat_tle, daemon=True).start()

    def _load_sat_tle(self):
        ok, msg = self._sat_matcher.load()
        self._sat_loaded = ok
        self._last_sat_match_t = 0.0
        self.root.after(0, self._sat_status_var.set,
                        f"TLE: {msg}" if ok else f"Sat unavailable — {msg}")

    def _maybe_match_satellite(self):
        """Runs in the poll thread. Throttled, vectorised, never blocks the UI."""
        if not (self._sat_on and self._sat_loaded):
            return
        if time.time() - self._last_sat_match_t < SAT_MATCH_INTERVAL:
            return
        bs = self._last_boresight
        lat = self.location_panel._dish_lat
        lon = self.location_panel._dish_lon
        if bs is None or lat is None or lon is None:
            self.root.after(0, self._show_sat_match, None, "need GPS fix + pointing")
            return
        self._last_sat_match_t = time.time()
        try:
            res = self._sat_matcher.match(lat, lon, bs[1], bs[0])
        except Exception as e:
            self.root.after(0, self._show_sat_match, None, str(e)[:40])
            return
        self.root.after(0, self._show_sat_match, res, None)

    def _load_borders(self):
        ok, _msg = self._border_map.load()
        if ok:
            self.root.after(0, self.sky_map.set_borders, self._border_map.rings)

    def _update_sky_map(self):
        """Runs in the poll thread every cycle: snapshot all sats and redraw the map."""
        if not (self._sat_on and self._sat_loaded):
            return
        lat = self.location_panel._dish_lat
        lon = self.location_panel._dish_lon
        if lat is None or lon is None:
            self.root.after(0, self.sky_map.update, None, None, None, None, None, None)
            return
        try:
            snap = self._sat_matcher.snapshot(lat, lon, min_el=20.0)
        except Exception:
            return
        bs = self._last_boresight
        baz = bs[1] if bs else None
        bel = bs[0] if bs else None
        self.root.after(0, self.sky_map.update, lat, lon, snap,
                        self._last_sat_name, self._last_sat_sep, baz, bel)

    def _show_sat_match(self, res, err):
        if err:
            self.detail_info.set("Likely Sat", "--", DIM)
            self._sat_status_var.set(f"Sat: {err}")
            self._last_sat_name, self._last_sat_sep = "", ""
            return
        if not res:
            self.detail_info.set("Likely Sat", "no sat above horizon", DIM)
            self._last_sat_name, self._last_sat_sep = "", ""
            return
        name, sep, _saz, _sel = res
        color = GREEN if sep < 3 else (YELLOW if sep < 8 else ORANGE)
        self.detail_info.set("Likely Sat", f"{name}  (Δ{sep:.1f}°)", color)
        self._sat_status_var.set(f"Sat: nearest of catalogue, Δ{sep:.1f}° from boresight")
        self._last_sat_name, self._last_sat_sep = name, f"{sep:.2f}"

    def _reconnect_gps(self, port):
        save_gps_port(port)
        if self._gps_reader is not None:
            self._gps_reader.stop()
        self._gps_reader = GpsReader(port, GPS_BAUD, self._on_gps_update,
                                     on_raw=self._on_gps_raw)
        self.location_panel.set_gps_connecting(port)

    def _on_gps_update(self, lat, lon, quality, num_sats):
        if lat is not None and quality > 0:
            self._last_gps = {"lat": lat, "lon": lon,
                              "sats": num_sats, "quality": quality}
        self.root.after(0, self.location_panel.set_gps_status,
                        lat, lon, quality, num_sats)

    def _on_gps_raw(self, line):
        # Called from the GPS thread for each NMEA sentence; marshal to the UI thread.
        self.root.after(0, self.location_panel.append_nmea, line)

    def _on_close(self):
        self._logger.close()
        self.root.destroy()

    # ------------------------------------------------------------------
    def _build_ui(self):
        title = tk.Label(self.root, text="  STARLINK  DISH  MONITOR",
                         bg=BG, fg=BLUE, font=F_TITLE,
                         anchor="w", pady=8, padx=12)
        title.pack(fill="x")
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

        self.status_bar = StatusBar(self.root)

        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=10, pady=8)
        main.columnconfigure((0, 1, 2, 3), weight=1, uniform="col")
        # Row 1 (Location with its NMEA feed) needs more height than the
        # metric/history rows, so it gets extra weight instead of equal sizing.
        main.rowconfigure(0, weight=3)
        main.rowconfigure(1, weight=4)
        main.rowconfigure(2, weight=3)

        # Row 0 — throughput + latency metrics
        self.card_latency = MetricCard(
            main, "Ping Latency", unit="ms", fmt="{:.1f}",
            low_good=True, warn=80, crit=150, spark_color=TEAL)
        self.card_latency.frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        self.card_drop = MetricCard(
            main, "Packet Loss", unit="%", fmt="{:.2f}",
            low_good=True, warn=1.0, crit=5.0, spark_color=ORANGE)
        self.card_drop.frame.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)

        self.card_dl = MetricCard(
            main, "Download", unit="Mbps", fmt="{:.1f}",
            spark_color=BLUE)
        self.card_dl.frame.grid(row=0, column=2, sticky="nsew", padx=4, pady=4)

        self.card_ul = MetricCard(
            main, "Upload", unit="Mbps", fmt="{:.1f}",
            spark_color=PURPLE)
        self.card_ul.frame.grid(row=0, column=3, sticky="nsew", padx=4, pady=4)

        # Row 1 — SNR, location (wide), status.  Dish Info now lives in the
        # detail window, so Location spans two columns and shows full values.
        self.card_snr = MetricCard(
            main, "SNR", unit="dB", fmt="{:.1f}",
            spark_color=GREEN)
        self.card_snr.frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

        self.location_panel = LocationPanel(main, self._open_set_location)
        self.location_panel.frame.grid(row=1, column=1, columnspan=2,
                                       sticky="nsew", padx=4, pady=4)

        self.status_panel = StatusPanel(main)
        self.status_panel.frame.grid(row=1, column=3, sticky="nsew", padx=4, pady=4)

        # Row 2 — throughput history chart
        hist_frame = make_card(main, "Throughput History  (last 20 min)")
        hist_frame.grid(row=2, column=0, columnspan=4, sticky="nsew", padx=4, pady=4)

        # 20-minute mean download/upload — copyable labels overlaid top-right.
        self._avg_dl_var = tk.StringVar(value="↓ --")
        self._avg_ul_var = tk.StringVar(value="↑ -- Mbps")
        avg_hdr = tk.Frame(hist_frame, bg=CARD)
        avg_hdr.place(relx=1.0, x=-10, y=4, anchor="ne")
        tk.Label(avg_hdr, text=f"{BOXCAR_N}-sample avg:", bg=CARD, fg=DIM,
                 font=F_SMALL).pack(side="left")
        copyable_label(avg_hdr, self._avg_dl_var, fg=BLUE, font=F_VAL,
                       width=8).pack(side="left", padx=(4, 4))
        copyable_label(avg_hdr, self._avg_ul_var, fg=PURPLE, font=F_VAL,
                       width=11).pack(side="left")

        self.hist_canvas = tk.Canvas(hist_frame, bg=CARD, highlightthickness=0, height=100)
        self.hist_canvas.pack(fill="both", expand=True, padx=6, pady=4)
        self._dl_history: deque = deque(maxlen=HIST_POINTS)
        self._ul_history: deque = deque(maxlen=HIST_POINTS)
        self.hist_canvas.bind("<Configure>", lambda _: self._draw_history())

    def _build_detail_window(self):
        """Second window: sky position, tilt, sector signal, extended info."""
        self._detail = tk.Toplevel(self.root)
        self._detail.title("Starlink Detail")
        self._detail.configure(bg=BG)
        self._detail.geometry("900x680")
        self._detail.minsize(760, 560)
        # Keep alive with main window
        self._detail.protocol("WM_DELETE_WINDOW",
                               lambda: self._detail.withdraw())

        title = tk.Label(self._detail, text="  STARLINK  DETAIL  VIEW",
                         bg=BG, fg=PURPLE, font=F_TITLE,
                         anchor="w", pady=8, padx=12)
        title.pack(fill="x")
        tk.Frame(self._detail, bg=BORDER, height=1).pack(fill="x")

        main = tk.Frame(self._detail, bg=BG)
        main.pack(fill="both", expand=True, padx=10, pady=8)
        main.columnconfigure((0, 1, 2), weight=1, uniform="col")
        main.rowconfigure((0, 1), weight=1, uniform="row")

        # Satellite sky map — replaces the old Sky Position + Dish Tilt panels,
        # spanning their two cells. (Tilt now shows as a value in Dish Info.)
        self._sat_enabled = tk.BooleanVar(value=True)
        self._sat_status_var = tk.StringVar(value="")
        self.sky_map = SkyMapPanel(main, self._sat_enabled,
                                   self._on_toggle_sat, self._sat_status_var)
        self.sky_map.frame.grid(row=0, column=0, columnspan=2,
                                sticky="nsew", padx=4, pady=4)

        # Ready states
        self.ready_panel = ReadyStatesPanel(main)
        self.ready_panel.frame.grid(row=0, column=2, sticky="nsew", padx=4, pady=4)

        # Per-sector map (field 1028 updates slowly — like the sky/obstruction map,
        # it shifts over ~hours as the dish re-scans, not poll-to-poll)
        sector_frame = make_card(main, "Per-Sector Map")
        sector_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        tk.Label(sector_frame,
                 text="Slowly-updating sky scan — changes over hours, not seconds",
                 bg=CARD, fg=DIM, font=F_TINY, anchor="w",
                 wraplength=260, justify="left").pack(fill="x", padx=8, pady=(0, 2))
        self.sector_chart = SectorChart(sector_frame)
        self.sector_chart.pack(expand=True)

        # Dish info (moved from main window)
        self.info_panel = InfoPanel(main)
        self.info_panel.frame.grid(row=1, column=1, sticky="nsew", padx=4, pady=4)

        # Extended info
        self.detail_info = DetailInfoPanel(main)
        self.detail_info.frame.grid(row=1, column=2, sticky="nsew", padx=4, pady=4)

    # ------------------------------------------------------------------
    def _fetch_location(self):
        try:
            geo = fetch_geolocation()
            lat, lon = geo.get("lat", 0), geo.get("lon", 0)
            def apply():
                self.location_panel.set_ground_station(
                    lat, lon,
                    geo.get("city", "--"),
                    geo.get("regionName", "--"),
                    geo.get("isp", "--"),
                    geo.get("query", "--"),
                )
            self.root.after(0, apply)
        except Exception:
            pass

    def _open_set_location(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Set Dish Location")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.grab_set()

        pad = dict(padx=12, pady=6)
        tk.Label(dlg, text="Enter your dish coordinates:", bg=BG, fg=TEXT,
                 font=("Consolas", 11, "bold")).grid(
                     row=0, column=0, columnspan=2, sticky="w", **pad)

        fields = [("Latitude  (e.g. 47.6062)", "lat"),
                  ("Longitude (e.g. -122.3321)", "lon"),
                  ("Label     (optional)", "label")]
        entries = {}
        saved_lat, saved_lon, saved_label = load_saved_location()
        prefill = {"lat": str(saved_lat) if saved_lat is not None else "",
                   "lon": str(saved_lon) if saved_lon is not None else "",
                   "label": saved_label or ""}
        for r, (lbl_text, key) in enumerate(fields, start=1):
            tk.Label(dlg, text=lbl_text, bg=BG, fg=DIM,
                     font=("Consolas", 10)).grid(row=r, column=0, sticky="w", **pad)
            var = tk.StringVar(value=prefill[key])
            e = tk.Entry(dlg, textvariable=var, bg=CARD, fg=TEXT,
                         insertbackground=TEXT, font=("Consolas", 11), width=28,
                         relief="flat", highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=BLUE)
            e.grid(row=r, column=1, sticky="w", **pad)
            entries[key] = var

        err_var = tk.StringVar()
        tk.Label(dlg, textvariable=err_var, bg=BG, fg=RED,
                 font=("Consolas", 9)).grid(row=4, column=0, columnspan=2, **pad)

        def on_save():
            try:
                lat = float(entries["lat"].get().strip())
                lon = float(entries["lon"].get().strip())
            except ValueError:
                err_var.set("Latitude and longitude must be numbers.")
                return
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                err_var.set("Lat must be -90…90, Lon -180…180.")
                return
            label = entries["label"].get().strip()
            save_location(lat, lon, label)
            self.location_panel.set_dish_location(lat, lon, label)
            dlg.destroy()

        btn_frame = tk.Frame(dlg, bg=BG)
        btn_frame.grid(row=5, column=0, columnspan=2, pady=8)
        tk.Button(btn_frame, text="Save", command=on_save,
                  bg=BLUE, fg=BG, font=("Consolas", 11, "bold"),
                  relief="flat", padx=16, pady=4, cursor="hand2").pack(side="left", padx=8)
        tk.Button(btn_frame, text="Cancel", command=dlg.destroy,
                  bg=BORDER, fg=TEXT, font=("Consolas", 11),
                  relief="flat", padx=16, pady=4, cursor="hand2").pack(side="left")

    def _connect(self):
        if self._client is None:
            self._client = StarlinkClient()

    def _poll_loop(self):
        first = True
        while True:
            try:
                self._connect()
                if first:
                    first = False
                    hist = self._client.get_history()
                    self.root.after(0, self._seed_history, hist)
                status = self._client.get_status()
                self.root.after(0, self._apply_status, status)
                self._maybe_match_satellite()
                self._error_count = 0
            except Exception as e:
                self._error_count += 1
                self._client = None
                first = True  # re-seed on reconnect
                msg = str(e)[:80]
                self.root.after(0, self.status_bar.update, False,
                                f"Error ({self._error_count}): {msg}")
            # Sky map runs on TLE + GPS, independent of the dish gRPC link, so it
            # keeps moving even while the dish is briefly unreachable. Guarded in its
            # own try so a bad frame here can never kill the poll thread.
            try:
                self._update_sky_map()
            except Exception as e:
                self.root.after(0, self.status_bar.update, True,
                                f"sky map skipped: {str(e)[:60]}")
            time.sleep(POLL_INTERVAL)

    def _seed_history(self, h):
        """Pre-populate sparklines from the dish's onboard 900-second history buffer.

        Note: SNR is deliberately NOT seeded. The history array at field 1010 does
        not match the live signal_stats.snr_db (it ranges ~16-89, mean ~32 vs a live
        ~16 dB), so it is not the SNR metric. Rather than show wrong buffered values,
        the SNR sparkline builds up from live polls only.
        """
        dl  = [v / 1e6 for v in h.downlink_throughput_bps]
        ul  = [v / 1e6 for v in h.uplink_throughput_bps]
        lat = list(h.pop_ping_latency_ms)
        drop = [v * 100 for v in h.pop_ping_drop_rate]

        for v in lat:  self.card_latency.spark.push(v)
        for v in drop: self.card_drop.spark.push(v)
        for v in dl:   self.card_dl.spark.push(v)
        for v in ul:   self.card_ul.spark.push(v)

        # Seeded data is 1 Hz; live polls are every POLL_INTERVAL seconds.
        # Downsample so each stored point represents the same time step as live data,
        # keeping the X-axis scale consistent and the 20-min window accurate.
        self._dl_history.extend(dl[::POLL_INTERVAL])
        self._ul_history.extend(ul[::POLL_INTERVAL])
        self._draw_history()

    def _apply_status(self, s):
        dl = s.downlink_throughput_bps / 1e6
        ul = s.uplink_throughput_bps / 1e6
        snr = s.signal_stats.snr_db
        el = s.boresight_elevation_deg
        az = s.boresight_azimuth_deg
        self._last_boresight = (el, az)   # consumed by optional TLE sat matcher
        # Main window metrics
        self.card_latency.update(s.pop_ping_latency_ms)
        self.card_drop.update(s.pop_ping_drop_rate * 100)
        self.card_dl.update(dl)
        self.card_ul.update(ul)
        self.card_snr.update(snr if snr > 0 else None)

        di = s.device_info
        ds = s.device_state
        uptime_h = ds.uptime_s // 3600
        uptime_m = (ds.uptime_s % 3600) // 60
        self.info_panel.set("ID", di.id)
        self.info_panel.set("Hardware", di.hardware_version)
        self.info_panel.set_firmware(di.software_version, KNOWN_FIRMWARE)
        self.info_panel.set("Uptime", f"{uptime_h}h {uptime_m}m")
        self._cum_dl_gb += dl * POLL_INTERVAL / 8 / 1e3  # Mbps × s → MB → GB
        self._cum_ul_gb += ul * POLL_INTERVAL / 8 / 1e3
        cum_total = self._cum_dl_gb + self._cum_ul_gb
        if cum_total < 1.0:
            usage_str = f"↓{self._cum_dl_gb*1e3:.0f} ↑{self._cum_ul_gb*1e3:.0f} MB"
        else:
            usage_str = f"↓{self._cum_dl_gb:.2f} ↑{self._cum_ul_gb:.2f} GB"
        self.info_panel.set("Usage", usage_str)

        # currently_obstructed reflects learned sky-map state in fw 2026.05.26,
        # not real-time signal loss — show cumulative events with age, hide if >12 h old
        obstr_events = s.obstruction_stats.obstruction_event_count
        if obstr_events > self._obstr_event_count:
            self._obstr_last_event_time = time.time()
            self._obstr_event_count = obstr_events
        elif self._obstr_last_event_time is None and obstr_events > 0:
            self._obstr_last_event_time = time.time()
            self._obstr_event_count = obstr_events

        age_s = (time.time() - self._obstr_last_event_time
                 if self._obstr_last_event_time else None)
        STALE = 43200  # 12 hours

        if obstr_events == 0:
            self.status_panel.set("Obstr. Events", "None", GREEN)
            self.status_panel.set("Obstr. Map", "--", DIM)
        elif age_s is not None and age_s > STALE:
            self.status_panel.set("Obstr. Events", "None recent", GREEN)
            self.status_panel.set("Obstr. Map", "--", DIM)
        else:
            if age_s is None:
                age_str = "this session"
            elif age_s < 60:
                age_str = f"{int(age_s)}s ago"
            elif age_s < 3600:
                age_str = f"{int(age_s/60)}m ago"
            else:
                age_str = f"{int(age_s/3600)}h {int(age_s%3600/60)}m ago"
            evt_color = YELLOW if obstr_events < 5 else RED
            self.status_panel.set("Obstr. Events",
                                  f"{obstr_events}  ({age_str})", evt_color)
            self.status_panel.set("Obstr. Map",
                                  f"last seen {age_str}", DIM)
        self.status_panel.set("Ethernet", f"{s.eth_speed_mbps} Mbps")
        self.status_panel.set("Elevation", f"{el:.1f}°")
        self.status_panel.set("Azimuth", f"{az:.1f}°")
        self.status_panel.set("SNR", f"{snr:.1f} dB" if snr > 0 else "--")
        self.status_panel.set("Uptime", f"{uptime_h}h {uptime_m}m")
        self.status_panel.set("Firmware", di.software_version)

        self._dl_history.append(dl)
        self._ul_history.append(ul)
        self._draw_history()

        # Dish tilt from orientation quaternion (fields: x=1, w=2, y=3, z=4) ->
        # shown as a value in the Dish Info panel (the gauge graphic was removed).
        q = s.tilt_quaternion
        w, x, y, z = q.w, q.x, q.y, q.z
        if abs(w) > 0.01 or abs(x) > 0.01:
            rz = w*w - x*x - y*y + z*z
            tilt_deg = math.degrees(math.acos(max(-1.0, min(1.0, rz))))
            self.info_panel.set("Tilt", f"{tilt_deg:.1f}° from vertical")

        self.sector_chart.update(s.sector_signal)
        self.ready_panel.update(s.ready_states)

        # Extended info panel
        gps_valid = s.gps_status.valid
        self.detail_info.set("Country", di.country_code)
        self.detail_info.set("GPS Valid", "Yes" if gps_valid else "No",
                             GREEN if gps_valid else RED)
        gps_acc = s.gps_status.accuracy
        acc_color = GREEN if gps_acc < 5 else (YELLOW if gps_acc < 20 else RED)
        self.detail_info.set("GPS Accuracy", f"{gps_acc:.2f} m", acc_color)
        # (Obstruction is reported via event count below; the old "Obstr. Score"
        #  used signal_stats f7, which is an alignment metric, not obstruction.)
        self.detail_info.set("Sec. Elevation", f"{s.signal_stats.secondary_elevation_deg:.1f}°")
        self.detail_info.set("Sec. Azimuth",   f"{s.signal_stats.secondary_azimuth_deg:.1f}°")
        self.detail_info.set("Obstr. Events",  s.obstruction_stats.obstruction_event_count)
        self.detail_info.set("Dish ID", di.id)
        if s.router_id:
            self.detail_info.set("Router ID", s.router_id)
        if s.dish_timestamp > 0:
            dt = datetime.datetime.fromtimestamp(s.dish_timestamp, tz=datetime.timezone.utc)
            self.detail_info.set("Dish Clock", dt.strftime("%Y-%m-%d %H:%M:%S UTC"))

        self.status_bar.update(True,
            f"dl {dl:.1f} Mbps  ul {ul:.1f} Mbps  "
            f"latency {s.pop_ping_latency_ms:.0f} ms  "
            f"loss {s.pop_ping_drop_rate*100:.2f}%  "
            f"SNR {snr:.1f} dB")

        # --- Data logging ---
        q = s.tilt_quaternion
        w2, x2, y2, z2 = q.w, q.x, q.y, q.z
        tilt_log = ""
        if abs(w2) > 0.01 or abs(x2) > 0.01:
            rz = w2*w2 - x2*x2 - y2*y2 + z2*z2
            tilt_log = f"{math.degrees(math.acos(max(-1.0, min(1.0, rz)))):.2f}"
        gps = self._last_gps
        self._logger.log({
            "timestamp_utc":       datetime.datetime.now(datetime.timezone.utc)
                                   .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dl_mbps":             f"{dl:.3f}",
            "ul_mbps":             f"{ul:.3f}",
            "latency_ms":          f"{s.pop_ping_latency_ms:.1f}",
            "drop_pct":            f"{s.pop_ping_drop_rate * 100:.4f}",
            "snr_db":              f"{snr:.2f}",
            "boresight_el_deg":    f"{el:.2f}",
            "boresight_az_deg":    f"{az:.2f}",
            "tilt_deg":            tilt_log,
            "obstr_events":        obstr_events,
            "eth_mbps":            s.eth_speed_mbps,
            "uptime_s":            s.device_state.uptime_s,
            "dish_gps_valid":      1 if s.gps_status.valid else 0,
            "dish_gps_accuracy_m": f"{s.gps_status.accuracy:.2f}",
            "align_metric_f7":     f"{s.signal_stats.align_metric:.4f}",
            "gps_lat":             f"{gps['lat']:.6f}" if gps.get("lat") else "",
            "gps_lon":             f"{gps['lon']:.6f}" if gps.get("lon") else "",
            "gps_sats":            gps.get("sats", ""),
            "gps_quality":         gps.get("quality", ""),
            "firmware":            di.software_version,
            "country":             di.country_code,
            "cum_dl_gb":           f"{self._cum_dl_gb:.6f}",
            "cum_ul_gb":           f"{self._cum_ul_gb:.6f}",
            "likely_sat":          self._last_sat_name,
            "likely_sat_sep_deg":  self._last_sat_sep,
            **{f"sector{i}": getattr(s.sector_signal, f"s{i}", "")
               for i in range(1, 11)},
        })

    def _draw_history(self):
        c = self.hist_canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 4 or h < 4:
            return

        all_vals = list(self._dl_history) + list(self._ul_history)
        lo = 0
        hi = max(all_vals) if all_vals else 1
        if hi == lo:
            hi = lo + 1
        ticks = _nice_ticks(lo, hi, n=4)

        LMARGIN = 48
        TOP = 22     # space for legend
        BOTTOM = 16  # space for X-axis labels
        plot_w = w - LMARGIN - 6
        plot_h = h - TOP - BOTTOM

        def to_y(v):
            return (h - BOTTOM) - (v - lo) / (hi - lo) * plot_h

        def to_x(i, n):
            return LMARGIN + i * plot_w / max(n - 1, 1)

        # Y gridlines + labels
        for tick in ticks:
            y = to_y(tick)
            c.create_line(LMARGIN, y, w - 6, y, fill=BORDER, dash=(2, 4))
            c.create_text(LMARGIN - 4, y, text=f"{tick:.1f}",
                          fill=TEXT, font=F_TINY, anchor="e")

        # Axes
        c.create_line(LMARGIN, TOP, LMARGIN, h - BOTTOM, fill=BORDER)
        c.create_line(LMARGIN, h - BOTTOM, w - 6, h - BOTTOM, fill=BORDER)

        # "Mbps" axis title
        c.create_text(LMARGIN - 4, TOP - 6, text="Mbps",
                      fill=DIM, font=F_TINY, anchor="e")

        # X-axis time labels — each stored point is POLL_INTERVAL seconds apart;
        # cap at HIST_POINTS * POLL_INTERVAL so the scale never exceeds 20 min.
        n_pts = max(len(self._dl_history), len(self._ul_history))
        total_s = min(n_pts * POLL_INTERVAL, HIST_POINTS * POLL_INTERVAL)
        for frac, anchor in ((0.0, "w"), (0.25, "center"), (0.5, "center"),
                              (0.75, "center"), (1.0, "e")):
            age_s = total_s * (1.0 - frac)
            if age_s == 0:
                label = "now"
            elif age_s < 60:
                label = f"-{int(age_s)}s"
            else:
                label = f"-{int(age_s/60)}m{int(age_s%60):02d}s"
            x = LMARGIN + frac * plot_w
            c.create_line(x, h - BOTTOM, x, h - BOTTOM + 3, fill=BORDER)
            c.create_text(x, h - BOTTOM + 5, text=label, fill=TEXT,
                          font=F_TINY, anchor="n")

        def draw_series(data, color):
            if len(data) < 2:
                return
            vals = list(data)
            xs = [to_x(i, len(vals)) for i in range(len(vals))]
            ys = [to_y(v) for v in vals]
            pts = []
            for x, y in zip(xs, ys):
                pts += [x, y]
            c.create_line(*pts, fill=color, width=2, smooth=True)

        draw_series(self._dl_history, BLUE)
        draw_series(self._ul_history, PURPLE)

        # Legend
        lx = LMARGIN + 8
        c.create_rectangle(lx, 6, lx+10, 16, fill=BLUE, outline="")
        c.create_text(lx + 14, 11, text="Download", fill=TEXT,
                      font=("Consolas", 9), anchor="w")
        c.create_rectangle(lx + 90, 6, lx + 100, 16, fill=PURPLE, outline="")
        c.create_text(lx + 104, 11, text="Upload", fill=TEXT,
                      font=("Consolas", 9), anchor="w")

        # Moving boxcar mean over the most recent BOXCAR_N samples
        if self._dl_history or self._ul_history:
            dl_box = list(self._dl_history)[-BOXCAR_N:]
            ul_box = list(self._ul_history)[-BOXCAR_N:]
            dl_mean = sum(dl_box) / len(dl_box) if dl_box else 0.0
            ul_mean = sum(ul_box) / len(ul_box) if ul_box else 0.0
            self._avg_dl_var.set(f"↓ {dl_mean:.1f}")
            self._avg_ul_var.set(f"↑ {ul_mean:.1f} Mbps")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _setup_crash_logging(root):
    """Make the dashboard survivable and self-diagnosing: log any UI-callback
    exception (and any hard crash) to data/crash.log and keep running, rather
    than letting a single bad frame take the whole app down."""
    import faulthandler, traceback
    log_path = Path(__file__).parent / "data" / "crash.log"
    try:
        log_path.parent.mkdir(exist_ok=True)
        fh = open(log_path, "a", buffering=1, encoding="utf-8")
    except Exception:
        return
    fh.write(f"\n=== session start {datetime.datetime.now().isoformat()} ===\n")
    try:
        faulthandler.enable(file=fh)          # C-level trace on segfault/abort
    except Exception:
        pass

    def _report(exc, val, tb):
        fh.write(f"\n--- UI callback exception {datetime.datetime.now().isoformat()} ---\n")
        traceback.print_exception(exc, val, tb, file=fh)
        fh.flush()
        try:
            traceback.print_exception(exc, val, tb)   # also to stderr if present
        except Exception:
            pass
    # tkinter calls this for exceptions raised inside callbacks; default behaviour
    # already continues, but we route it to a persistent log we can inspect later.
    root.report_callback_exception = _report


def main():
    print("Compiling Starlink protobuf definitions...")
    try:
        ensure_proto_compiled()
        print("OK")
    except Exception as e:
        print(f"Failed to compile proto: {e}")
        sys.exit(1)

    root = tk.Tk()
    _setup_crash_logging(root)

    # Dark title bar on Windows — must run after mainloop starts so the HWND exists
    def _apply_dark_titlebar():
        try:
            from ctypes import windll, byref, sizeof, c_int
            hwnd = windll.user32.GetParent(root.winfo_id())
            windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, byref(c_int(1)), sizeof(c_int))
        except Exception:
            pass
    root.after(50, _apply_dark_titlebar)

    app = Dashboard(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
