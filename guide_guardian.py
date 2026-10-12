#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Loran Hughes
# SPDX-License-Identifier: GPL-3.0-only
"""Guide Guardian - guiding watchdog and auto-recenter for NINA (Advanced API v2).

Reads guiding RMS either from the NINA "Advanced API" plugin or straight from PHD2's
event server (TCP 4400). When Total RMS stays above a threshold for a sustained
period, it runs a recovery routine:

    stop sequence / abort exposure -> stop guiding -> slew + plate-solve center on
    the reference target -> restart guiding and wait for it to settle -> resume the
    sequence -> cooldown

All network work happens in a QThread (GuardWorker); the GUI only reacts to signals.
A PHD2 guide log can be replayed through the same trigger code offline (Replay guide log... button, or --replay).
Endpoint paths were taken from the plugin's own source (christian-photo/ninaAPI).
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import glob
import json
import logging
import math
import os
import platform
import queue
import re
import secrets
import socket
import statistics
import struct
import sys
import threading
import time as _time
import traceback
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from html import escape
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

import requests
import web_ui
from PyQt6.QtCore import (PYQT_VERSION_STR, QT_VERSION_STR, QBuffer, QCoreApplication, QIODevice, QObject, QPointF,
                          QRectF, QSettings, Qt, QThread, QTimer, pyqtSignal)
from PyQt6.QtGui import QBrush, QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap, QTransform
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)



class _SimClock:
    """A clock that only moves when it is told to (guide-log replay)."""

    def __init__(self, now: float = 0.0):
        self.now = now


class _Clock:
    """Stands in for the `time` module in this file: the real clock, except on a thread that is replaying a guide log.

    That thread installs a `_SimClock`, so the trigger logic can be driven through a whole night in seconds while the
    live monitor keeps running, untouched, on its own thread. The debug log is muted on a simulating thread too.
    """
    _local = threading.local()

    @property
    def sim(self) -> _SimClock | None:
        return getattr(self._local, "sim", None)

    def install(self, sim: _SimClock | None) -> None:
        self._local.sim = sim

    def monotonic(self) -> float:
        sim = self.sim
        return sim.now if sim is not None else _time.monotonic()

    def time(self) -> float:
        sim = self.sim
        return sim.now if sim is not None else _time.time()

    def __getattr__(self, name: str):                      # sleep, strftime, ...: the real module
        return getattr(_time, name)


time = _Clock()

APP_NAME = "Guide Guardian"
APP_VERSION = "1.0.2"
PROJECT_URL = "https://github.com/WestwoodAstro/GuideGuardian"
APP_TAGLINE = "Guiding watchdog and session recovery for PHD2 and NINA"
DEFAULT_URL = "http://localhost:1888/v2/api"
POLL_TIMEOUT = (3.05, 8)          # (connect, read) seconds for status polls
CTRL_TIMEOUT = (5, 30)            # for ordinary control calls
SEQ_POLL_EVERY_S = 5.0            # sequence tree is heavier than guider/camera info
RECENTER_ACCEPT_ARCMIN = 10.0     # accept an "unflagged" center result if the mount ends up this close to the target
MAX_TARGET_SEPARATION_DEG = 5.0   # refuse to recenter if the mount is further than this from the target
ATTEMPT_WINDOW_S = 3600.0
BREACH_LOG_AFTER_S = 10.0         # a breach is shown in the log only once it has lasted this long (shorter ones: debug log)
POLL_HEARTBEAT_S = 60.0           # polled bodies are re-logged in full at least this often (debug log)
SNAP_EVERY_S = 5.0                # dashboard snapshot interval in the debug log (also on any change)
EVAL_EVERY_S = 10.0               # evaluation line interval in the debug log while nothing is happening
IMAGE_POLL_EVERY_S = 60.0         # NINA image history -> debug log
ROI_EDGE_PX = 60                  # guide-star search regions stay this far inside the frame edges
SETTINGS_VERSION = 2              # bump to reset MIGRATE_KEYS to their new defaults once
MIGRATE_KEYS = ("sustain_s", "lost_pct_enabled", "lost_pct", "lost_window", "settle_hold_s")
OBSOLETE_KEYS = ("sequence_mode", "trigger_on_lost_lock")      # removed options: drop any value saved by older versions
SETTINGS_APPLY_DELAY_MS = 700    # the GUI applies edits this long after the last keystroke / click
SETTINGS_RELOAD_EVERY_S = 3.0    # headless: how often the saved settings are checked for changes
DITHER_MAX_S = 180.0             # a dither indicator never stays on longer than this without a SettleDone
STAR_BACK_HOLD_S = 20.0           # guiding must look good this long before the star counts as "back"
TRANSPARENCY_SHARE = 0.7          # share of lost frames that are "low SNR"/"mass changed" for a cloud-like loss
RETRY_DELAYS_S = (300, 600, 1200) # wait before the 2nd, 3rd and 4th recovery attempt after a failed one (then give up)
FAIL_RESET_GOOD_S = 600.0         # guiding good this long clears the consecutive-failure count
RESTORE_ACCEPT_ARCMIN = 3.0       # after slewing back, the mount must be this close to its previous position
SEQ_END_GRACE_S = 60.0           # sequence must stay not-running this long before "disarm when it finishes"
AUTO_ARM_HOLD_S = 10.0           # sequence running + guiding must hold this long before auto-arming
PHD2_MIN_SAMPLES = 10             # RMS from the PHD2 stream is not reported before this many guide steps


# --------------------------------------------------------------------------- settings
@dataclass(frozen=True)
class Settings:
    base_url: str = DEFAULT_URL
    threshold: float = 2.5
    unit: str = "arcsec"              # "arcsec" | "pixel"
    rms_source: str = "nina"          # "nina" = Advanced API guider/info, "phd2" = PHD2 event server
    phd2_host: str = ""               # blank = same host as the NINA URL
    phd2_port: int = 4400
    reselect_star: str = "star_loss"  # PHD2 guide-star re-select in recovery: "never" | "star_loss" | "always"
    phd2_window: int = 40             # guide steps in the rolling RMS (PHD2 source only)
    sustain_s: int = 120              # how long guiding must be 'currently bad' before a recovery starts
    recent_steps: int = 10            # PHD2 source: the last N steps' RMS must also exceed the threshold (0 = off)
    cooldown_s: int = 300
    max_attempts_per_hour: int = 3
    lost_pct_enabled: bool = True     # trigger on the share of frames that lost the star (PHD2 source only)
    wait_for_star_s: int = 900        # cloud-like star loss: wait this long for the star before any recovery (0 = off)
    lost_pct: int = 30                # % of the last `lost_window` frames
    lost_window: int = 20             # frames in the rolling star-loss window
    debug_log_enabled: bool = False   # write a verbose debug log into debug_log_dir
    debug_log_dir: str = ""
    web_enabled: bool = False         # optional LAN web interface (live view + token-protected controls)
    web_port: int = 8765
    web_token: str = ""
    web_view_needs_token: bool = False
    disarm_on_seq_end: bool = True    # disarm when the NINA sequence has finished / been stopped
    auto_arm: bool = False            # arm by itself when the sequence is running and PHD2 is guiding
    use_manual_target: bool = False
    manual_ra_deg: float = 0.0        # J2000 degrees
    manual_dec_deg: float = 0.0
    poll_s: float = 1.0
    recenter_timeout_s: int = 900
    settle_hold_s: int = 3
    settle_timeout_s: int = 180


def migrate_saved_settings(qs: QSettings) -> list[str]:
    """One-off cleanup of values saved by older versions; returns what was reset (for the log)."""
    d, changed = Settings(), []
    for k in OBSOLETE_KEYS:
        qs.remove(k)
    if qs.value("settings_version", 1, type=int) < SETTINGS_VERSION:
        for k in MIGRATE_KEYS:                            # saved values from the old defaults would hide the new ones
            if qs.contains(k):
                old, new = qs.value(k), getattr(d, k)
                if str(old).lower() != str(new).lower():
                    changed.append(f"{k} {old} -> {new}")
                qs.remove(k)
        qs.setValue("settings_version", SETTINGS_VERSION)
    return changed


def load_saved_settings(qs: QSettings) -> Settings:
    """Settings as saved by the GUI (same store), for headless runs."""
    d = Settings()
    return Settings(**{f.name: qs.value(f.name, getattr(d, f.name), type=type(getattr(d, f.name))) for f in fields(Settings)})


LEGACY_ORG = "PHD2Guard"          # the settings store of the app's previous name (PHD2 Guard 1.0.0)


def open_qsettings(path: str | None) -> QSettings:
    """The settings store: an .ini file, or the registry. First run under the new name: carry over what the previous name
    saved (its store is left untouched)."""
    if path:
        return QSettings(path, QSettings.Format.IniFormat)
    qs = QSettings("GuideGuardian", "GuideGuardian")
    if not qs.allKeys():
        old = QSettings(LEGACY_ORG, LEGACY_ORG)
        keys = old.allKeys()
        for k in keys:
            qs.setValue(k, old.value(k))
        if keys:
            qs.sync()
    return qs


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/") or DEFAULT_URL
    if "://" not in url:
        url = "http://" + url
    if not url.lower().endswith("/v2/api"):
        url += "/v2/api"
    return url


# --------------------------------------------------------------------------- astro helpers
def _precess(ra_deg: float, dec_deg: float, zeta: float, z: float, theta: float) -> tuple[float, float]:
    """Meeus eq. 21.4 rotation (all angles in radians)."""
    ra, dec = math.radians(ra_deg), math.radians(dec_deg)
    a = math.cos(dec) * math.sin(ra + zeta)
    b = math.cos(theta) * math.cos(dec) * math.cos(ra + zeta) - math.sin(theta) * math.sin(dec)
    c = math.sin(theta) * math.cos(dec) * math.cos(ra + zeta) + math.cos(theta) * math.sin(dec)
    return math.degrees(math.atan2(a, b) + z) % 360.0, math.degrees(math.asin(max(-1.0, min(1.0, c))))


def jnow_to_j2000(ra_deg: float, dec_deg: float, when: datetime | None = None) -> tuple[float, float]:
    """Precession only (no nutation/aberration, ~20" accuracy) - fine for a plate-solve centering target."""
    when = when or datetime.now(timezone.utc)
    t = (when.timestamp() / 86400.0 + 2440587.5 - 2451545.0) / 36525.0
    arc = math.radians(1.0 / 3600.0)
    zeta = (2306.2181 * t + 0.30188 * t * t + 0.017998 * t ** 3) * arc
    z = (2306.2181 * t + 1.09468 * t * t + 0.018203 * t ** 3) * arc
    theta = (2004.3109 * t - 0.42665 * t * t - 0.041833 * t ** 3) * arc
    return _precess(ra_deg, dec_deg, -z, -zeta, -theta)      # inverse rotation


def separation_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    h = math.sin((d2 - d1) / 2) ** 2 + math.cos(d1) * math.cos(d2) * math.sin((r2 - r1) / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(h))))


# --------------------------------------------------------------------------- verbose debug log
class DebugLog:
    """Verbose debug log: one timestamped line per event, flushed immediately, rotated by size. Off until open()."""
    MAX_BYTES, BACKUPS = 25 * 1024 * 1024, 20

    def __init__(self):
        self._logger = logging.getLogger("guideguardian.debug")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        self._handler: RotatingFileHandler | None = None
        self.path = ""

    @property
    def enabled(self) -> bool:
        return self._handler is not None and time.sim is None

    def open(self, folder: str) -> str:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"guideguardian_debug_{datetime.now():%Y%m%d_%H%M%S}.log")
        handler = RotatingFileHandler(path, maxBytes=self.MAX_BYTES, backupCount=self.BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", "%Y-%m-%d %H:%M:%S"))
        self.close()
        self._logger.addHandler(handler)
        self._handler, self.path = handler, path
        return path

    def close(self) -> None:
        handler, self._handler, self.path = self._handler, None, ""
        if handler is not None:
            self._logger.removeHandler(handler)
            handler.close()

    def write(self, tag: str, msg: str) -> None:
        if self._handler is not None and time.sim is None:
            self._logger.debug("%-6s %s", tag, msg)


DEBUG = DebugLog()
BODY_LIMIT = 30000                    # characters of an HTTP response body kept per debug-log line


# --------------------------------------------------------------------------- NINA client
class NinaError(Exception):
    def __init__(self, message: str, status: int | None = None, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body = body                                      # the decoded response envelope, when there was one


class NinaConnectionError(NinaError):
    """Network-level failure (refused, timeout, DNS ...). Safe to retry idempotent calls."""


class PreflightError(NinaError):
    """A recovery cannot start (no target / mount far away). Raised before anything is stopped."""


class Canceled(Exception):
    pass


def _b(value: bool) -> str:
    return "true" if value else "false"


class NinaClient:
    def __init__(self, base_url: str, on_call=None):
        self.base_url = normalize_url(base_url)
        self._session = requests.Session()
        self._on_call = on_call
        self._last_sig: dict[str, tuple] = {}             # polled paths: (signature, time of last full body logged)

    def request(self, path: str, params: dict | None = None, timeout=POLL_TIMEOUT,
                cancel: threading.Event | None = None, quiet: bool = False) -> dict:
        """GET `path`; returns the decoded envelope. Raises NinaError / NinaConnectionError / Canceled.

        With `cancel`, the HTTP call runs in a helper thread so a long blocking request
        (e.g. slew+center) can be abandoned promptly when the event is set.
        """
        if self._on_call and not quiet:
            self._on_call(path, params)
        if cancel is None:
            return self._do(path, params, timeout, quiet)
        box: dict = {}
        done = threading.Event()

        def run():
            try:
                box["result"] = self._do(path, params, timeout, quiet)
            except BaseException as exc:                      # noqa: BLE001 - re-raised below
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        while not done.wait(0.25):
            if cancel.is_set():
                raise Canceled()
        if "error" in box:
            raise box["error"]
        return box["result"]

    # Polled endpoints: the full body is logged only when its signature (the fields that matter) changes, and as a heartbeat.
    _SIGNATURES = {
        "/sequence/json": re.compile(r'"Status":"(\w+)"'),
        "/equipment/guider/info": re.compile(r'"(?:State|Connected)":("?\w+"?)'),
        "/equipment/camera/info": re.compile(r'"(?:IsExposing|CameraState|Connected)":("?\w+"?)'),
    }

    def _debug_http(self, path: str, params: dict | None, resp, t0: float, quiet: bool) -> None:
        text = resp.text
        head = f"GET {path} {params or ''} -> {resp.status_code} ({(time.monotonic() - t0) * 1000:.0f} ms)"
        pattern = self._SIGNATURES.get(path) if quiet else None
        if pattern is not None:
            sig, now = tuple(pattern.findall(text)), time.monotonic()
            last = self._last_sig.get(path)
            if last is not None and last[0] == sig and now - last[1] < POLL_HEARTBEAT_S:
                DEBUG.write("HTTP", f"{head} (poll, unchanged)")
                return
            self._last_sig[path] = (sig, now)
        body = text if len(text) <= BODY_LIMIT else f"{text[:BODY_LIMIT]}... [{len(text)} chars, truncated]"
        DEBUG.write("HTTP", f"{head} {'(poll) ' if quiet else ''}{body}")

    def _do(self, path: str, params: dict | None, timeout, quiet: bool = False) -> dict:
        t0 = time.monotonic()
        try:
            resp = self._session.get(self.base_url + path, params=params, timeout=timeout)
        except requests.RequestException as exc:
            DEBUG.write("HTTP", f"GET {path} {params or ''} -> {type(exc).__name__}: {exc} "
                                f"({(time.monotonic() - t0) * 1000:.0f} ms)")
            raise NinaConnectionError(f"{type(exc).__name__}: {exc}") from exc
        if DEBUG.enabled:
            self._debug_http(path, params, resp, t0, quiet)
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "Success" in body:
            if not body["Success"]:
                raise NinaError(str(body.get("Error") or body.get("Response") or "request failed"),
                                body.get("StatusCode", resp.status_code), body)
            return body
        if not resp.ok:
            raise NinaError(f"HTTP {resp.status_code}", resp.status_code)
        if not isinstance(body, dict):
            raise NinaError("Unexpected (non-JSON) response")
        return body


# --------------------------------------------------------------------------- sequence parsing
@dataclass
class SeqInfo:
    running: bool = False
    target: str = ""
    activity: str = ""
    exposure_s: float | None = None


def _clean(name: str) -> str:
    return name[:-len("_Container")] if name.endswith("_Container") else name


def _running_chain(items: list) -> list[tuple[str, dict]]:
    """Follow the chain of RUNNING nodes from the root downward -> [(parent name, node), ...]."""
    chain: list[tuple[str, dict]] = []
    level, parent = items, "root"
    while True:
        nxt = next((n for n in level if isinstance(n, dict) and "Name" in n
                    and str(n.get("Status", "")).upper() == "RUNNING"), None)
        if nxt is None:
            break
        chain.append((parent, nxt))
        parent, level = str(nxt["Name"]), nxt.get("Items") or []
    return chain


def parse_sequence(items: list) -> SeqInfo:
    """Summarize /sequence/json (or /sequence/state): running?, current target, activity, frame length."""
    chain = _running_chain(items)
    info = SeqInfo(running=bool(chain))
    for par, node in chain:
        if par == "Targets_Container":
            info.target = _clean(str(node["Name"]))
        if node.get("ExposureTime") is not None:
            try:
                info.exposure_s = float(node["ExposureTime"])
            except (TypeError, ValueError):
                pass
    if chain:
        info.activity = _clean(str(chain[-1][1]["Name"]))
    return info


def _input_coords_deg(ic: dict) -> tuple[float, float] | None:
    """NINA InputCoordinates (RA h/m/s, Dec d/m/s + NegativeDec) -> J2000 degrees."""
    try:
        ra = (float(ic["RAHours"]) + float(ic["RAMinutes"]) / 60 + float(ic["RASeconds"]) / 3600) * 15.0
        dec = abs(float(ic["DecDegrees"])) + float(ic["DecMinutes"]) / 60 + float(ic["DecSeconds"]) / 3600
        return ra % 360.0, -dec if (ic.get("NegativeDec") or float(ic["DecDegrees"]) < 0) else dec
    except (KeyError, TypeError, ValueError):
        return None


def _node_coords(node: dict) -> tuple[tuple[float, float], str] | None:
    """Coordinates of a target container: its `Target.InputCoordinates`, else a Center instruction inside it."""
    target = node.get("Target")
    if isinstance(target, dict) and isinstance(target.get("InputCoordinates"), dict):
        c = _input_coords_deg(target["InputCoordinates"])
        if c:
            return c, "target"
    for child in node.get("Items") or []:
        if not isinstance(child, dict):
            continue
        co = child.get("Coordinates")
        if isinstance(co, dict) and "center" in str(child.get("Name", "")).lower():
            if co.get("RADegrees") is not None and co.get("Dec") is not None:
                epoch, ra, dec = str(co.get("Epoch", "J2000")).upper(), float(co["RADegrees"]), float(co["Dec"])
                if epoch == "JNOW":
                    ra, dec = jnow_to_j2000(ra, dec)
                if epoch in ("JNOW", "J2000"):
                    return (ra, dec), "Center instruction"
            c = _input_coords_deg(co)
            if c:
                return c, "Center instruction"
    return None


@dataclass
class RunningTarget:
    name: str
    ra_deg: float
    dec_deg: float
    source: str


def find_running_target(items: list) -> RunningTarget | None:
    """The target container currently RUNNING under 'Targets', with its J2000 coordinates (None if unknown)."""
    for parent, node in _running_chain(items):
        if parent == "Targets_Container":
            found = _node_coords(node)
            if found:
                (ra, dec), source = found
                return RunningTarget(_clean(str(node["Name"])), ra, dec, source)
    return None


# --------------------------------------------------------------------------- PHD2 RPC (guide-star re-select)
class Phd2Error(Exception):
    """PHD2 answered a JSON-RPC call with an error."""


def phd2_call(host: str, port: int, method: str, params: dict | None = None, timeout: float = 6.0):
    """One JSON-RPC call on a short-lived connection to PHD2's server; returns the result (events are skipped)."""
    req = {"method": method, "id": 1}
    if params:
        req["params"] = params
    DEBUG.write("RPC>", f"{host}:{port} {json.dumps(req)}")
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(req) + "\r\n").encode())
        buf, deadline = b"", time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == 1 and "Event" not in msg:
                    DEBUG.write("RPC<", line.decode(errors="replace").strip())
                    if "error" in msg:
                        raise Phd2Error(str((msg["error"] or {}).get("message", msg["error"])))
                    return msg.get("result")
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed by PHD2")
            buf += chunk
    raise TimeoutError(f"PHD2 did not answer {method}")


def rois_excluding(old: tuple[float, float] | None, frame: tuple[int, int], margin: int = 40,
                   min_side: int = 100, edge: int = ROI_EDGE_PX) -> list[list[int] | None]:
    """Search regions [x, y, w, h] that keep PHD2's star finder away from `old`: the four half-frames beyond `margin` px
    of it (largest first), all kept `edge` px inside the frame borders so no star is picked at the very edge, then the
    whole inner area, then None (full frame) as a last resort."""
    w, h = frame
    inner = [edge, edge, w - 2 * edge, h - 2 * edge]
    if old is None:
        return [inner, None]
    x, y = old
    boxes = [(0, 0, x - margin, h), (x + margin, 0, w, h), (0, 0, w, y - margin), (0, y + margin, w, h)]   # x0, y0, x1, y1
    cands = []
    for x0, y0, x1, y1 in boxes:
        x0, y0, x1, y1 = max(x0, edge), max(y0, edge), min(x1, w - edge), min(y1, h - edge)
        if x1 - x0 >= min_side and y1 - y0 >= min_side:
            cands.append([int(x0), int(y0), int(x1 - x0), int(y1 - y0)])
    cands.sort(key=lambda c: c[2] * c[3], reverse=True)
    return cands + [inner, None]


def looks_like_transparency(reasons: dict) -> bool:
    """True when most dropped frames say "low SNR" or "mass changed" - thin cloud / transparency, not a mechanical fault
    (a real loss of the star, e.g. after a mount slip, reads "No star found")."""
    total = sum(reasons.values())
    cloudy = sum(n for r, n in reasons.items() if "low snr" in r.lower() or "mass changed" in r.lower())
    return total > 0 and cloudy / total >= TRANSPARENCY_SHARE


# --------------------------------------------------------------------------- PHD2 event server
class Phd2Monitor(threading.Thread):
    """Listens to PHD2's event server (newline-delimited JSON, default TCP 4400).

    PHD2 does not broadcast RMS, so it is computed here from the raw per-frame errors in
    `GuideStep` events: RMS = sqrt(mean(d^2)) over the last `window` steps, per axis, with
    total = sqrt(RA^2 + Dec^2). Steps taken while PHD2 is settling (e.g. after a dither) are
    left out so a dither does not look like bad guiding; the buffer is cleared whenever guiding
    stops or restarts. Arcsec values need the pixel scale from the `get_pixel_scale` RPC.

    Star loss: while guiding, PHD2 emits one `StarLost` event per dropped frame and one `GuideStep`
    per good frame, so the share of dropped frames over the last `lost_window` frames is tracked too.
    """
    _PIXSCALE_ID, _STATE_ID = 1, 2

    def __init__(self, host: str, port: int, window: int, lost_window: int, recent: int = 10):
        super().__init__(daemon=True, name="phd2-monitor")
        self.host, self.port = host, port
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._ra: collections.deque[float] = collections.deque(maxlen=window)
        self._dec: collections.deque[float] = collections.deque(maxlen=window)
        self._frames: collections.deque[bool] = collections.deque(maxlen=lost_window)   # True = star lost
        self._reasons: collections.deque[str] = collections.deque(maxlen=lost_window)   # PHD2's reason ("" = good frame)
        self._last_lost = ""
        self._recent = recent
        self._connected, self._error = False, ""
        self._state, self._settling, self._scale = "Unknown", False, None
        self._dithering, self._dither_at = False, 0.0
        self._sock: socket.socket | None = None

    # ----- control --------------------------------------------------------------------
    def set_window(self, window: int, lost_window: int, recent: int | None = None) -> None:
        with self._lock:
            if recent is not None:
                self._recent = recent
            if window != self._ra.maxlen:
                self._ra = collections.deque(self._ra, maxlen=window)
                self._dec = collections.deque(self._dec, maxlen=window)
            if lost_window != self._frames.maxlen:
                self._frames = collections.deque(self._frames, maxlen=lost_window)
                self._reasons = collections.deque(self._reasons, maxlen=lost_window)

    def stop(self) -> None:
        self._stop_evt.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def snapshot(self) -> dict:
        with self._lock:
            n = len(self._ra)
            rms = lambda d: math.sqrt(sum(v * v for v in d) / len(d))
            ok = n >= min(PHD2_MIN_SAMPLES, self._ra.maxlen)
            ra, dec = (rms(self._ra), rms(self._dec)) if ok else (None, None)
            k = self._recent                                   # RMS of just the newest k steps (total, px)
            tail = list(zip(self._ra, self._dec))[-k:] if (k and ok) else []
            recent = (math.sqrt(sum(a * a + b * b for a, b in tail) / len(tail))
                      if len(tail) >= min(k or 1, PHD2_MIN_SAMPLES) and tail else None)
            nf = len(self._frames)
            lost = sum(self._frames)
            return {"recent_px": recent, "lost_reasons": dict(collections.Counter(r for r in self._reasons if r)),
                    "lost": lost, "frames": nf, "last_lost": self._last_lost,
                    "lost_pct": 100.0 * lost / nf if nf >= min(PHD2_MIN_SAMPLES, self._frames.maxlen) else None,
                    "connected": self._connected, "error": self._error, "state": self._state,
                    "settling": self._settling, "scale": self._scale, "samples": n,
                    "dithering": self._dithering and time.monotonic() - self._dither_at < DITHER_MAX_S,
                    "window": self._ra.maxlen, "ra_px": ra, "dec_px": dec,
                    "total_px": None if ra is None else math.hypot(ra, dec)}

    # ----- connection -----------------------------------------------------------------
    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._session()
            except (OSError, ValueError) as exc:
                self._set_connected(False, str(exc))
            self._stop_evt.wait(3.0)

    def _set_connected(self, ok: bool, error: str = "") -> None:
        DEBUG.write("PHD2", f"{self.host}:{self.port} " + ("connected" if ok else f"disconnected: {error}"))
        with self._lock:
            self._connected, self._error = ok, error
            if not ok:
                self._state, self._settling, self._dithering = "Unknown", False, False
                self._clear()

    def _send(self, method: str, req_id: int) -> None:
        DEBUG.write("PHD2>", f"{method} id={req_id}")
        self._sock.sendall((json.dumps({"method": method, "id": req_id}) + "\r\n").encode())

    def _session(self) -> None:
        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            self._sock = sock
            sock.settimeout(1.0)
            self._set_connected(True)
            self._send("get_pixel_scale", self._PIXSCALE_ID)
            self._send("get_app_state", self._STATE_ID)
            buf = b""
            while not self._stop_evt.is_set():
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    raise ConnectionError("closed by PHD2")
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        DEBUG.write("PHD2<", line.decode(errors="replace").strip())
                        try:
                            self._handle(json.loads(line))
                        except (ValueError, TypeError, KeyError):
                            pass                                  # ignore a malformed line, keep the stream
        self._sock = None

    # ----- message handling -----------------------------------------------------------
    def _clear(self) -> None:
        self._ra.clear(); self._dec.clear(); self._frames.clear(); self._reasons.clear(); self._last_lost = ""

    def _handle(self, msg: dict) -> None:
        with self._lock:
            ev = msg.get("Event")
            if ev is None:                                        # RPC reply
                result = msg.get("result")
                if msg.get("id") == self._PIXSCALE_ID and isinstance(result, (int, float)) and result > 0:
                    self._scale = float(result)
                elif msg.get("id") == self._STATE_ID and isinstance(result, str) and self._state == "Unknown":
                    self._state = result
                return
            if ev == "AppState":
                self._state = msg["State"]
                if self._state != "Guiding":
                    self._clear()
            elif ev == "GuideStep":
                self._state = "Guiding"
                self._frames.append(False)
                self._reasons.append("")
                # ErrorCode on a GuideStep is only a star warning (1 = saturated, or the primary star flagged while
                # multi-star guiding carries on); PHD2 still used the offset. Dropped frames arrive as StarLost.
                if not self._settling:
                    self._ra.append(float(msg["RADistanceRaw"])); self._dec.append(float(msg["DECDistanceRaw"]))
            elif ev == "StartGuiding":
                self._state = "Guiding"; self._clear()
            elif ev in ("GuidingStopped", "LoopingExposuresStopped"):
                self._state, self._settling, self._dithering = "Stopped", False, False; self._clear()
            elif ev == "LoopingExposures":
                self._state = "Looping"; self._clear()
            elif ev == "StarLost":
                if self._state in ("Guiding", "LostLock"):    # ignore frames dropped while calibrating / looping
                    self._state = "LostLock"
                    self._frames.append(True)
                    self._last_lost = str(msg.get("Status") or "")
                    self._reasons.append(self._last_lost or "unknown")
            elif ev == "Paused":
                self._state = "Paused"
            elif ev == "Resumed":
                self._state = "Guiding"
            elif ev == "StartCalibration":
                self._state = "Calibrating"; self._clear()
            elif ev in ("SettleBegin", "Settling"):
                self._settling = True
            elif ev == "GuidingDithered":                         # PHD2 moved the lock position; it now settles
                self._dithering, self._dither_at = True, time.monotonic()
            elif ev == "SettleDone":
                self._settling = self._dithering = False
            if self._scale is None and ev in ("StartGuiding", "CalibrationComplete") and self._sock:
                try:
                    self._send("get_pixel_scale", self._PIXSCALE_ID)
                except OSError:
                    pass


# --------------------------------------------------------------------------- worker
class GuardWorker(QThread):
    log = pyqtSignal(str, str, str)          # timestamp, level, message
    status = pyqtSignal(dict)                # dashboard snapshot
    phase = pyqtSignal(str, str)             # kind, text   (idle/monitoring/pending/recovering/cooldown/halted)
    connection = pyqtSignal(bool, str)       # connected, detail
    phd2_connection = pyqtSignal(bool, str)  # PHD2 event server reachable, detail
    debug_log_state = pyqtSignal(str, str)   # current debug-log file ("" = not recording), error text
    armed_changed = pyqtSignal(bool)
    attempts_changed = pyqtSignal(int)
    reference_changed = pyqtSignal(str)
    alert = pyqtSignal(str)
    settings_applied = pyqtSignal(str)       # one-line summary of a settings change that has just taken effect

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._settings_lock = threading.Lock()
        self._pending_settings: Settings | None = None
        self._pending_source = ""
        self._cmds: queue.Queue[str] = queue.Queue()
        self._wake = threading.Event()
        self._quit = threading.Event()
        self._abort = threading.Event()
        self._client = NinaClient(settings.base_url, self._log_call)
        self._file_log = logging.getLogger("guideguardian")
        self._debug_dir = ""
        self._debug_settings: Settings | None = None

        self._armed = False
        self._recovering = False
        self._connected: bool | None = None
        self._phase_key = ("", "")
        self._breach_since: float | None = None
        self._breach_logged = False
        self._wait_since: float | None = None      # waiting for a cloud-lost star to return
        self._wait_ok_since: float | None = None
        self._fail_count = 0                       # consecutive failed recoveries
        self._retry_at: float | None = None
        self._restore_needed = False               # the last failed recovery could not restore guiding/sequence
        self._last_star_loss = False
        self._fail_good_since: float | None = None
        self._cached_target: RunningTarget | None = None   # target of the last attempt (the sequence is stopped when retrying)
        self._pending_seq_restart = False          # the failed attempt stopped a running sequence that is still stopped
        self._chain_pre_pos: tuple[float, float] | None = None   # pointing before the first attempt of a failed chain
        self._snap_logged: tuple = (None, 0.0)
        self._eval_logged = 0.0
        self._img_polled, self._img_count = 0.0, None
        self._cooldown_until = 0.0
        self._attempts: collections.deque[float] = collections.deque()
        self._seq_seen_running = False    # the sequence has been running since this arming
        self._seq_gone_since: float | None = None
        self._auto_since: float | None = None
        self._auto_latch = False          # set by any arm/disarm/halt; cleared when the sequence or guiding stops
        self._label_key: tuple | None = None
        self._label_retry_at = 0.0
        self._seq = SeqInfo()
        self._seq_polled = 0.0
        self._settling = False
        self._phd2: Phd2Monitor | None = None
        self._phd2_conn: bool | None = None
        self._scale_warned = False

    # ----- thread-safe API for the GUI ------------------------------------------------
    def update_settings(self, settings: Settings, source: str = "") -> None:
        """Hand the worker new settings (any thread). Monitoring carries on: they are applied between two evaluations,
        never half-way through one and not during a recovery, which keeps the settings it started with. The change
        is logged ("Setting changed: ..."). `source` names where it came from, for the log."""
        if not self.isRunning():                              # no loop to wait for (tests, replays): apply right now
            with self._settings_lock:
                self._pending_settings, self._pending_source = settings, source
            self._apply_pending_settings()
            return
        with self._settings_lock:
            self._pending_settings, self._pending_source = settings, source
        if self._recovering and settings != self.settings:
            self._log("INFO", "Settings change received - it takes effect when the recovery in progress has finished")
        self._wake.set()

    def command(self, name: str) -> None:
        if name == "disarm":
            self._abort.set()                # interrupts a recovery in progress
        self._cmds.put(name)
        self._wake.set()

    def shutdown(self) -> None:
        self._quit.set()
        self._abort.set()
        self._wake.set()

    # ----- logging --------------------------------------------------------------------
    def _log(self, level: str, msg: str) -> None:
        self._file_log.log({"ERROR": logging.ERROR, "WARN": logging.WARNING}.get(level, logging.INFO), "[%s] %s", level, msg)
        DEBUG.write(level, msg)
        self.log.emit(datetime.now().strftime("%H:%M:%S"), level, msg)

    SECRET_SETTINGS = ("web_token",)                          # logged as "changed", never with the value
    RULE_SETTINGS = ("threshold", "unit", "rms_source", "phd2_host", "phd2_port", "phd2_window", "sustain_s",
                     "recent_steps", "lost_pct_enabled", "lost_pct", "lost_window", "wait_for_star_s")

    @classmethod
    def _config_json(cls, s: Settings) -> str:
        """The settings as one JSON line for the debug log, secrets masked."""
        d = asdict(s)
        for k in cls.SECRET_SETTINGS:
            if d.get(k):
                d[k] = "***"
        return json.dumps(d)

    @staticmethod
    def _rule_text(s: Settings) -> str:
        rule = f"Total RMS > {s.threshold:g} {s.unit}" + (f" (and the last {s.recent_steps} steps)" if s.recent_steps else "")
        if s.lost_pct_enabled:
            rule += f", or >= {s.lost_pct}% of the last {s.lost_window} frames lost"
            if s.wait_for_star_s:
                rule += f"; a cloud-like star loss first waits up to {s.wait_for_star_s // 60} min for the star to return"
        return f"recovery after guiding has been bad for {s.sustain_s}s ({rule}); " \
               f"cooldown {s.cooldown_s}s, max {s.max_attempts_per_hour}/hour"

    def _apply_pending_settings(self) -> None:
        """Take over settings handed in with update_settings() and log every change. Runs on the worker thread between
        evaluations, so an evaluation always sees one consistent set of settings and monitoring is never interrupted."""
        with self._settings_lock:
            new, source = self._pending_settings, self._pending_source
            self._pending_settings, self._pending_source = None, ""
        if new is None or new == self.settings:
            return
        old, self.settings = self.settings, new
        changed = [f.name for f in fields(Settings) if getattr(old, f.name) != getattr(new, f.name)]
        parts = [f"{k} changed" if k in self.SECRET_SETTINGS else f"{k} {getattr(old, k)} -> {getattr(new, k)}"
                 for k in changed]
        self._log("INFO", "Setting changed: " + "; ".join(parts) + (f" [{source}]" if source else ""))
        if self._armed and any(k in self.RULE_SETTINGS for k in changed):
            if self._breach_since is not None:                # judge the pending breach by the new rule from scratch
                self._breach_since, self._breach_logged = None, False
                self._log("INFO", "The trigger rule changed while guiding was degraded: the sustain timer restarts "
                                  "under the new rule (monitoring continues)")
            self._wait_ok_since = None
            self._log("OK", f"Rule in force from now on: {self._rule_text(new)}")
        DEBUG.write("CONFIG", self._config_json(new))         # the replay / session report reads the rule from here
        self._debug_settings = new
        self.settings_applied.emit("; ".join(parts))

    def _sync_debug(self) -> None:
        """Open/close the verbose debug log to match the settings; record every settings change in it."""
        s = self.settings
        want = s.debug_log_dir.strip() if s.debug_log_enabled else ""
        if want != self._debug_dir:
            self._debug_dir = want
            was_open = DEBUG.enabled
            DEBUG.close()
            self._debug_settings = None
            if not want:
                self.debug_log_state.emit("", "")
                if was_open:
                    self._log("INFO", "Verbose debug log closed")
            else:
                try:
                    path = DEBUG.open(want)
                except OSError as exc:
                    self.debug_log_state.emit("", str(exc))
                    self._log("ERROR", f"Cannot open debug log in '{want}': {exc}")
                else:
                    DEBUG.write("START", f"{APP_NAME} debug log | Python {platform.python_version()} | {platform.platform()} "
                                         f"| file {path}")
                    self.debug_log_state.emit(path, "")
                    self._log("OK", f"Verbose debug log: {path}")
        if DEBUG.enabled and s != self._debug_settings:
            self._debug_settings = s
            DEBUG.write("CONFIG", self._config_json(s))

    def _log_call(self, path: str, params: dict | None) -> None:
        self._log("API", f"GET {path}" + (f"  {params}" if params else ""))

    def _set_phase(self, kind: str, text: str) -> None:
        if (kind, text) != self._phase_key:
            self._phase_key = (kind, text)
            DEBUG.write("PHASE", f"{kind}: {text}")
            self.phase.emit(kind, text)

    # ----- main loop ------------------------------------------------------------------
    def run(self) -> None:
        self._log("INFO", f"{APP_NAME} started")
        self._set_phase("idle", "Idle - not armed")
        while not self._quit.is_set():
            self._wake.clear()
            try:
                self._drain_commands()
                if self._quit.is_set():
                    break
                self._apply_pending_settings()
                self._sync_debug()
                self._sync_client()
                self._sync_phd2()
                snap = self._poll()
                if snap is not None and self._armed:
                    self._evaluate(snap)
                elif snap is not None and not self._recovering:
                    self._maybe_auto_arm(snap)
            except Canceled:
                pass
            except Exception as exc:                          # noqa: BLE001 - keep the watchdog alive
                self._log("ERROR", f"Unexpected error: {type(exc).__name__}: {exc}")
                self._file_log.exception("unexpected error")
                DEBUG.write("TRACE", traceback.format_exc())
            self._wake.wait(max(0.2, self.settings.poll_s))
        if self._phd2 is not None:
            self._phd2.stop()
        self._log("INFO", "Worker stopped")
        DEBUG.close()

    def _sync_client(self) -> None:
        if normalize_url(self.settings.base_url) != self._client.base_url:
            self._client = NinaClient(self.settings.base_url, self._log_call)
            self._connected = None
            self._seq_polled = 0.0
            self._log("INFO", f"API base URL -> {self._client.base_url}")

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                return
            try:
                if cmd == "test":
                    self._test_connection()
                elif cmd == "arm":
                    self._arm()
                elif cmd == "disarm":
                    self._disarm("Stopped by user")
                elif cmd == "manual":
                    self._abort.clear()
                    self._recover("manual trigger", manual=True)
            except Canceled:
                pass

    # ----- polling --------------------------------------------------------------------
    def _set_connected(self, ok: bool, detail: str = "") -> None:
        if ok != self._connected:
            self._connected = ok
            self.connection.emit(ok, detail)
            self._log("INFO" if ok else "WARN", "Connected to NINA API" if ok else f"Cannot reach NINA API ({detail})")

    def _test_connection(self) -> None:
        self._client = NinaClient(self.settings.base_url, self._log_call)
        try:
            ver = self._client.request("/version").get("Response")
            self._connected = None                            # force a fresh transition message
            self._set_connected(True)
            self._log("OK", f"NINA Advanced API reachable - plugin version {ver}")
        except NinaError as exc:
            self._connected = None
            self._set_connected(False, str(exc))

    def _poll(self) -> dict | None:
        """Fetch guider/camera (and occasionally sequence) info and publish the dashboard snapshot."""
        s = self.settings
        try:
            g = self._client.request("/equipment/guider/info", quiet=True)["Response"]
        except NinaConnectionError as exc:
            self._set_connected(False, str(exc))
            self._breach_since = None
            self.status.emit({"connected": False})
            return None
        except NinaError as exc:
            self._set_connected(True)
            self._log("WARN", f"guider/info: {exc}")
            return None
        self._set_connected(True)

        exposing = None
        try:
            exposing = bool(self._client.request("/equipment/camera/info", quiet=True)["Response"].get("IsExposing"))
        except NinaError:
            pass
        if time.monotonic() - self._seq_polled >= SEQ_POLL_EVERY_S:
            self._seq_polled = time.monotonic()
            self._seq = self._fetch_sequence()
            self._track_target()

        phd2_settling = samples = window = lost = recent = None
        phd2_dither = False
        if self._phd2 is not None:                            # RMS + state from the PHD2 event server
            p = self._phd2.snapshot()
            self._note_phd2(p)
            v = self._phd2_view(p)
            ra, dec, total, state, recent = v["ra"], v["dec"], v["total"], v["state"], v["recent"]
            phd2_settling, samples, window, lost = v["phd2_settling"], v["samples"], v["window"], v["lost"]
            phd2_dither = v["dithering"]
        else:                                                 # RMS + state from NINA's guider info
            key = "Arcseconds" if s.unit == "arcsec" else "Pixel"
            rms = g.get("RMSError") or {}

            def pick(name):
                v = (rms.get(name) or {}).get(key)
                return float(v) if v is not None else None

            ra, dec, total = pick("RA"), pick("Dec"), pick("Total")
            state = g.get("State") or ("Stopped" if g.get("Connected") else "Not connected")
        if self._settling or phd2_settling or state == "Calibrating":
            settle = "Calibrating" if state == "Calibrating" else "Settling"
        else:
            settle = "Settled" if state == "Guiding" else "-"
        snap = {
            "connected": True, "guider_connected": bool(g.get("Connected")), "state": state,
            "ra": ra, "dec": dec, "total": total, "recent": recent, "unit": s.unit, "phd2_settling": phd2_settling,
            # PHD2 says so (GuidingDithered .. SettleDone) or NINA is running its Dither instruction (works with either source)
            "dithering": bool(phd2_dither) or self._seq.activity == "Dither",
            "samples": samples, "window": window, "lost": lost,
            "settle": settle, "exposing": exposing, "target": self._seq.target,
            "activity": self._seq.activity or ("Sequence idle" if not self._seq.running else ""),
            "exposure_s": self._seq.exposure_s, "seq_running": self._seq.running,
        }
        if DEBUG.enabled:
            key = (state, exposing, settle, phd2_settling, snap["dithering"], snap["target"], snap["activity"], snap["seq_running"],
                   lost["count"] if lost else None)
            now = time.monotonic()
            if key != self._snap_logged[0] or now - self._snap_logged[1] >= SNAP_EVERY_S:
                self._snap_logged = (key, now)
                DEBUG.write("SNAP", json.dumps({k: snap[k] for k in (
                    "state", "ra", "dec", "total", "recent", "unit", "settle", "phd2_settling", "dithering", "samples", "window", "lost",
                    "exposing", "target", "activity", "seq_running")}, default=str, separators=(",", ":")))
            self._log_images(now)
        self.status.emit(snap)
        return snap

    def _log_images(self, now: float) -> None:
        """Debug log: one line per new NINA image (HFR, stars, guiding RMS during the exposure ...) to judge recoveries."""
        if now - self._img_polled < IMAGE_POLL_EVERY_S:
            return
        self._img_polled = now
        try:
            n = self._client.request("/image-history", {"count": "true"}, quiet=True).get("Response")
            if not isinstance(n, int):
                return
            first = self._img_count if self._img_count is not None else max(0, n - 3)
            for idx in range(first, min(n, first + 20)):
                item = self._client.request("/image-history", {"index": idx}, quiet=True).get("Response")
                item = item[0] if isinstance(item, list) and item else item
                if isinstance(item, dict):
                    DEBUG.write("IMAGE", f"#{idx} " + " ".join(f"{k}={item.get(k)}" for k in (
                        "TargetName", "Filter", "ExposureTime", "HFR", "Stars", "RmsText", "Median", "Date", "Filename")))
            self._img_count = min(n, first + 20)
        except NinaError:
            pass

    def _phd2_view(self, p: dict) -> dict:
        """Turn a Phd2Monitor snapshot into the fields the dashboard and `_evaluate` use (unit conversion)."""
        s = self.settings

        def px(v):
            if v is None or s.unit == "pixel":
                return v
            return v * p["scale"] if p["scale"] else None

        if p["connected"] and p["total_px"] is not None and s.unit == "arcsec" and not p["scale"]:
            if not self._scale_warned:
                self._scale_warned = True
                self._log("WARN", "PHD2 reports no pixel scale (set focal length / calibrate) - use the 'pixel' unit")
        return {"ra": px(p["ra_px"]), "dec": px(p["dec_px"]), "total": px(p["total_px"]), "recent": px(p["recent_px"]),
                "state": p["state"] if p["connected"] else "PHD2 offline",
                "phd2_settling": p["settling"], "dithering": p["dithering"], "samples": p["samples"], "window": p["window"],
                "lost": {"pct": p["lost_pct"], "count": p["lost"], "frames": p["frames"], "reason": p["last_lost"],
                         "reasons": p["lost_reasons"]}}

    def _phd2_endpoint(self) -> tuple[str, int]:
        s = self.settings
        return s.phd2_host.strip() or urlparse(normalize_url(s.base_url)).hostname or "localhost", s.phd2_port

    def _sync_phd2(self) -> None:
        """Start/stop/retarget the PHD2 event-server listener to match the settings."""
        s = self.settings
        if s.rms_source != "phd2":
            if self._phd2 is not None:
                self._phd2.stop(); self._phd2 = None
                self._phd2_conn = None
            return
        host, _ = self._phd2_endpoint()
        if self._phd2 is not None and (self._phd2.host, self._phd2.port) != (host, s.phd2_port):
            self._phd2.stop(); self._phd2 = None
        if self._phd2 is None:
            self._phd2 = Phd2Monitor(host, s.phd2_port, s.phd2_window, s.lost_window, s.recent_steps)
            self._phd2_conn, self._scale_warned = None, False
            self._phd2.start()
            self._log("INFO", f"RMS source: PHD2 event server {host}:{s.phd2_port}")
        self._phd2.set_window(s.phd2_window, s.lost_window, s.recent_steps)

    def _note_phd2(self, p: dict) -> None:
        if p["connected"] != self._phd2_conn:
            self._phd2_conn = p["connected"]
            detail = f"{self._phd2.host}:{self._phd2.port}" + ("" if p["connected"] else f" - {p['error']}")
            self.phd2_connection.emit(p["connected"], detail)
            self._log("INFO" if p["connected"] else "WARN",
                      "Connected to PHD2 server" if p["connected"] else f"Cannot reach PHD2 server ({detail})")

    def _fetch_sequence(self) -> SeqInfo:
        try:
            body = self._client.request("/sequence/json", quiet=True)
            return parse_sequence(body.get("Response") or [])
        except NinaError:                                     # 409 = sequencer not initialized
            return SeqInfo()

    # ----- arming ---------------------------------------------------------------------
    def _reset_recovery_state(self) -> None:
        self._wait_since = self._wait_ok_since = self._retry_at = self._fail_good_since = None
        self._fail_count, self._restore_needed = 0, False
        self._cached_target, self._pending_seq_restart, self._chain_pre_pos = None, False, None

    def _arm(self) -> None:
        if self._armed:                                       # e.g. a double click / a repeated remote request
            return
        self._abort.clear()
        s = self.settings
        self._seq = self._fetch_sequence()
        self._label_key = None
        self._track_target()
        self._armed = True
        self._auto_latch = True
        self._seq_seen_running, self._seq_gone_since = False, None
        self._reset_recovery_state()
        self._breach_since = None
        self._cooldown_until = 0.0
        self.armed_changed.emit(True)
        self._set_phase("monitoring", "Monitoring")
        self._log("OK", f"Guarding armed: {self._rule_text(s)}")

    def _maybe_auto_arm(self, snap: dict) -> None:
        """Arm once the sequence is running and PHD2 has been guiding (not settling) for AUTO_ARM_HOLD_S.

        Edge-triggered: after any arm, Stop Guarding or safety stop it stays off until the sequence or
        guiding stops and starts again, so a user's Stop (or a safety stop) is never overridden.
        """
        ready = snap["seq_running"] and snap["state"] == "Guiding" and not snap["phd2_settling"]
        if not ready:
            self._auto_since, self._auto_latch = None, False
        if self._phase_key[0] == "idle":
            self._set_phase("idle", "Idle - waiting to auto-arm" if self.settings.auto_arm and not self._auto_latch
                            else "Idle - not armed")
        if not (self.settings.auto_arm and ready and not self._auto_latch):
            return
        now = time.monotonic()
        self._auto_since = self._auto_since or now
        if now - self._auto_since >= AUTO_ARM_HOLD_S:
            self._log("INFO", f"Auto-arm: sequence running and guiding for {AUTO_ARM_HOLD_S:.0f}s")
            self._arm()

    def _disarm(self, why: str) -> None:
        was = self._armed
        self._armed = False
        self._auto_latch = True
        self._reset_recovery_state()
        self._breach_since = None
        self._settling = False
        self.armed_changed.emit(False)
        self._set_phase("idle", "Idle - not armed")
        if was:
            self._log("INFO", f"Guarding disarmed ({why})")

    def _halt(self, msg: str) -> None:
        self._log("ERROR", f"SAFETY STOP: {msg}")
        self._armed = False
        self._auto_latch = True
        self._reset_recovery_state()
        self._breach_since = None
        self._settling = False
        self.armed_changed.emit(False)
        self._set_phase("halted", "HALTED - needs attention")
        self.alert.emit(msg)

    def _mount_j2000(self) -> tuple[float, float]:
        m = self._client.request("/equipment/mount/info", timeout=CTRL_TIMEOUT)["Response"]
        if not m.get("Connected"):
            raise NinaError("Mount not connected")
        c = m.get("Coordinates") or {}
        ra, dec, epoch = c.get("RADegrees"), c.get("Dec"), c.get("Epoch", "J2000")
        if ra is None or dec is None:                         # fall back to ASCOM-style hours/degrees (JNow)
            ra, dec, epoch = float(m["RightAscension"]) * 15.0, float(m["Declination"]), "JNOW"
        if isinstance(epoch, int):
            epoch = {0: "JNOW", 1: "B1950", 2: "J2000", 3: "J2050"}.get(epoch, str(epoch))
        epoch = str(epoch).upper()
        if epoch == "JNOW":
            return jnow_to_j2000(float(ra), float(dec))
        if epoch == "J2000":
            return float(ra), float(dec)
        raise NinaError(f"Unsupported mount epoch {epoch}; use manual coordinates")

    def _resolve_target(self) -> RunningTarget:
        """Coordinates of the target container that is running right now (fresh query)."""
        for path in ("/sequence/state", "/sequence/json"):    # state exposes Target; json exposes the Center instruction
            try:
                body = self._client.request(path, timeout=CTRL_TIMEOUT, quiet=True)
            except NinaConnectionError:
                raise
            except NinaError:
                continue
            found = find_running_target(body.get("Response") or [])
            if found:
                return found
        raise PreflightError("No running target with coordinates found in the NINA sequence")

    def _track_target(self) -> None:
        """Keep the dashboard's 'Recenter target' in sync with the running target (re-query on change)."""
        s, now = self.settings, time.monotonic()
        if self._recovering:                                  # the sequence is deliberately stopped meanwhile
            return
        key = (s.use_manual_target, s.manual_ra_deg, s.manual_dec_deg, self._seq.target if not s.use_manual_target else "")
        if key == self._label_key and (self._label_retry_at == 0.0 or now < self._label_retry_at):
            return
        self._label_key, self._label_retry_at = key, 0.0
        if s.use_manual_target:
            self.reference_changed.emit(f"Fixed: RA {s.manual_ra_deg:.5f}  Dec {s.manual_dec_deg:+.5f} (J2000 deg)")
            return
        if not self._seq.target:
            self.reference_changed.emit("waiting for a running target")
            return
        try:
            tgt = self._resolve_target()
        except NinaConnectionError:
            self._label_retry_at = now + 10
            return
        except PreflightError as exc:
            self._label_retry_at = now + 30
            self.reference_changed.emit(f"{self._seq.target}: coordinates not found")
            self._log("WARN", f"Target '{self._seq.target}': {exc}")
            return
        label = f"{tgt.name}: RA {tgt.ra_deg:.5f}  Dec {tgt.dec_deg:+.5f} (J2000 deg, from {tgt.source})"
        self.reference_changed.emit(label)
        self._log("INFO", f"Recenter target - {label}")

    # ----- evaluation -----------------------------------------------------------------
    def _recent_attempts(self) -> int:
        cutoff = time.time() - ATTEMPT_WINDOW_S
        while self._attempts and self._attempts[0] < cutoff:
            self._attempts.popleft()
        return len(self._attempts)

    def _sequence_finished(self, snap: dict, now: float, in_cooldown: bool) -> bool:
        """True once the sequence ran since arming and then stayed not-running for SEQ_END_GRACE_S.

        Arming before the sequence starts is fine (nothing is judged until it has been seen running), and the
        recovery's own stop/restart and its cooldown do not count toward the grace period.
        """
        if snap["seq_running"]:
            self._seq_seen_running, self._seq_gone_since = True, None
            return False
        if not self._seq_seen_running or in_cooldown:
            self._seq_gone_since = None
            return False
        self._seq_gone_since = self._seq_gone_since or now
        if now - self._seq_gone_since >= SEQ_END_GRACE_S:
            self._log("INFO", f"Sequence has not been running for {SEQ_END_GRACE_S:.0f}s - it has finished or was stopped")
            return True
        return False

    def _evaluate(self, snap: dict) -> None:
        s, now = self.settings, time.monotonic()
        if DEBUG.enabled and (self._breach_since is not None or now < self._cooldown_until
                              or now - self._eval_logged >= EVAL_EVERY_S):
            self._eval_logged = now
            DEBUG.write("EVAL", f"state={snap['state']} total={snap['total']} recent={snap.get('recent')} thr={s.threshold} "
                                f"settling={snap['phd2_settling']} lost={snap['lost']} breach_for="
                                f"{'-' if self._breach_since is None else round(now - self._breach_since, 1)} "
                                f"cooldown_left={max(0.0, round(self._cooldown_until - now, 1))} "
                                f"attempts_1h={self._recent_attempts()}/{s.max_attempts_per_hour}")
        if s.disarm_on_seq_end and self._sequence_finished(snap, now, in_cooldown=now < self._cooldown_until):
            self._disarm("sequence finished")
            return
        if now < self._cooldown_until:
            self._breach_since = None
            self._set_phase("cooldown", f"Cooldown ({int(self._cooldown_until - now)}s left)")
            return
        if self._phase_key[0] == "cooldown":
            self._log("INFO", "Cooldown finished - RMS evaluation resumed")
        if self._retry_at is not None:                        # a recovery failed: wait before the next attempt
            if now < self._retry_at:
                left = int(self._retry_at - now)
                self._set_phase("retry", f"Retry {self._fail_count + 1}/{len(RETRY_DELAYS_S) + 1} in {left // 60}:{left % 60:02d}")
                return
            self._retry_at = None
            if self._restore_needed:
                if not self._begin_attempt(s, f"RETRY {self._fail_count + 1}/{len(RETRY_DELAYS_S) + 1}: the session was "
                                              f"not restored after the failed recovery"):
                    return
                self._recover("retry after a failed recovery", manual=False, star_loss=self._last_star_loss)
                return
            self._log("INFO", "Retry window over - normal monitoring resumes")
        total, state = snap["total"], snap["state"]
        if snap["phd2_settling"]:                             # e.g. dither settle - not a guiding fault
            self._breach_since = None
            self._set_phase("monitoring", "Monitoring (dithering - PHD2 settling)" if snap.get("dithering")
                            else "Monitoring (PHD2 settling)")
            return

        # "Currently bad": the rolling RMS AND the newest steps both exceed the threshold (so a burst that has already
        # passed does not count), or too many of the recent frames lost the star. It must persist for sustain_s.
        recent = snap.get("recent")
        breach_reason, star_loss = None, False
        if state == "Guiding" and total is not None and total > s.threshold and (recent is None or recent > s.threshold):
            breach_reason = f"Total RMS {total:.2f} {s.unit} > {s.threshold:g}" + (
                f" (last {s.recent_steps} steps {recent:.2f})" if recent is not None else "")
        lost = snap["lost"]
        if (breach_reason is None and s.lost_pct_enabled and lost and lost["pct"] is not None
                and lost["pct"] >= s.lost_pct):
            breach_reason = (f"star lost in {lost['count']} of the last {lost['frames']} frames "
                             f"({lost['pct']:.0f}% >= {s.lost_pct}%"
                             + (f", PHD2: {lost['reason']}" if lost["reason"] else "") + ")")

        give_up = False
        if self._wait_since is not None:                      # waiting for a cloud-lost star to come back
            outcome = self._wait_step(s, now, breach_reason)
            if outcome != "give_up":
                return
            give_up = True
            breach_reason = breach_reason or f"star still missing after waiting {s.wait_for_star_s}s"
            held = now - (self._breach_since or now)
        else:
            if breach_reason is None:
                if self._breach_since is not None:
                    dur = now - self._breach_since
                    if self._breach_logged:
                        self._log("INFO", f"Guiding recovered by itself after {dur:.0f}s - breach timer reset")
                    else:
                        DEBUG.write("EVAL", f"short breach of {dur:.0f}s ended by itself")
                self._breach_since, self._breach_logged = None, False
                if self._fail_count:                          # a good stretch clears the consecutive-failure count
                    self._fail_good_since = self._fail_good_since or now
                    if now - self._fail_good_since >= FAIL_RESET_GOOD_S:
                        self._log("INFO", f"Guiding good for {FAIL_RESET_GOOD_S / 60:.0f} min - failed-recovery count cleared")
                        self._fail_count, self._fail_good_since = 0, None
                self._set_phase("monitoring", "Monitoring")
                return
            self._fail_good_since = None
            if self._breach_since is None:
                self._breach_since, self._breach_logged = now, False
                DEBUG.write("EVAL", f"breach started: {breach_reason}")
            held = now - self._breach_since
            if not self._breach_logged and held >= BREACH_LOG_AFTER_S:
                self._breach_logged = True
                self._log("WARN", f"Guiding degraded for {held:.0f}s: {breach_reason} (recovery starts after {s.sustain_s}s)")
            self._set_phase("pending", f"Breach pending ({held:.0f}/{s.sustain_s}s)")
            if held < s.sustain_s:
                return
            star_loss = star_loss or bool(lost and lost["pct"] is not None and lost["pct"] >= s.lost_pct)
            if s.wait_for_star_s > 0 and star_loss and lost and looks_like_transparency(lost.get("reasons") or {}):
                self._wait_since, self._wait_ok_since = now, None
                self._log("WARN", f"Star lost for {held:.0f}s with a low-SNR pattern ({lost['reasons']}) - looks like cloud / "
                                  f"transparency, which a recovery cannot fix. Waiting up to {s.wait_for_star_s}s for the star "
                                  f"to return; imaging and guiding are left alone")
                self._set_phase("waiting", f"Star lost - waiting for it to return ({s.wait_for_star_s // 60}:"
                                           f"{s.wait_for_star_s % 60:02d} left)")
                return

        star_loss = star_loss or give_up or bool(lost and lost["pct"] is not None and lost["pct"] >= s.lost_pct)
        if not self._begin_attempt(s, f"BREACH CONFIRMED ({breach_reason} for {held:.0f}s)"):
            return
        self._recover(breach_reason, manual=False, star_loss=star_loss)

    # ----- recovery state machine -----------------------------------------------------
    def _step(self, n: int, text: str) -> None:
        self._abort_check()
        self._log("INFO", f"Recovery step {n}/7: {text}")
        self._set_phase("recovering", f"Recovering - {text}")

    def _abort_check(self) -> None:
        if self._abort.is_set():
            raise Canceled()

    def _sleep(self, seconds: float) -> None:
        if self._abort.wait(seconds):
            raise Canceled()

    def _api(self, path: str, params: dict | None = None, *, timeout=CTRL_TIMEOUT, retries: int = 3) -> dict:
        """Control call with retry on network errors only (NINA-level errors are not retried)."""
        for attempt in range(1, retries + 1):
            self._abort_check()
            try:
                return self._client.request(path, params, timeout=timeout, cancel=self._abort)
            except NinaConnectionError as exc:
                if attempt == retries:
                    raise
                self._log("WARN", f"{path}: {exc} - retry {attempt}/{retries - 1} in 3s")
                self._sleep(3)
        raise AssertionError("unreachable")

    def _sequence_running(self) -> bool:
        return self._fetch_sequence().running

    def _begin_attempt(self, s: Settings, why: str) -> bool:
        """Hourly-limit check + bookkeeping before an automatic recovery attempt; False if the limit stopped the automation."""
        used = self._recent_attempts()
        if used >= s.max_attempts_per_hour:
            self._halt(f"{used} recoveries already ran in the last hour (limit {s.max_attempts_per_hour}) "
                       f"and guiding is degraded again. Automation stopped.")
            return False
        self._attempts.append(time.time())
        self.attempts_changed.emit(self._recent_attempts())
        self._log("WARN", f"{why} - starting recovery {used + 1}/{s.max_attempts_per_hour} this hour")
        return True

    def _wait_step(self, s: Settings, now: float, breach_reason: str | None) -> str:
        """One evaluation while waiting for a cloud-lost star: 'waiting', 'back' (star returned) or 'give_up' (timed out)."""
        waited = now - (self._wait_since or now)
        if breach_reason is None:
            self._wait_ok_since = self._wait_ok_since or now
            if now - self._wait_ok_since >= STAR_BACK_HOLD_S:
                self._log("OK", f"Guide star is back after waiting {waited:.0f}s - no recovery needed")
                self._wait_since = self._wait_ok_since = None
                self._breach_since, self._breach_logged = None, False
                self._set_phase("monitoring", "Monitoring")
                return "back"
        else:
            self._wait_ok_since = None
        if waited >= s.wait_for_star_s:
            self._log("WARN", f"Guide star still missing after waiting {waited:.0f}s - starting the recovery")
            self._wait_since = self._wait_ok_since = None
            return "give_up"
        left = int(s.wait_for_star_s - waited)
        self._set_phase("waiting", f"Star lost - waiting for it to return ({left // 60}:{left % 60:02d} left)")
        return "waiting"

    def _pre_position(self) -> tuple[float, float] | None:
        """Where the mount is before a recovery moves it, so a failed centering can be undone."""
        try:
            return self._mount_j2000()
        except NinaError as exc:
            self._log("WARN", f"Could not record the mount position ({exc}); a failed centering cannot be undone")
            return None

    def _guider_state(self) -> str:
        try:
            return str(self._client.request("/equipment/guider/info", quiet=True)["Response"].get("State"))
        except NinaError:
            return "?"

    def _restore(self, s: Settings, moved: bool, was_running: bool, pre_pos: tuple[float, float] | None) -> bool:
        """Best effort after a failed recovery: slew back to the previous pointing, restart guiding, restart the sequence.

        The sequence is only restarted once guiding is running again (unguided frames are useless) and only if the mount is
        back where it was (a failed Center leaves the scope at the target's nominal position, which can be tens of arcmin
        from the guided framing). Returns True when the session is fully restored.
        """
        self._log("WARN", "Restoring the session after the failed recovery")
        try:
            if moved:
                if pre_pos is None:
                    raise NinaError("the previous mount position was not recorded")
                ra, dec = pre_pos
                self._api("/equipment/mount/slew", {"ra": f"{ra:.6f}", "dec": f"{dec:.6f}", "waitForResult": "true"},
                          timeout=(5, 300), retries=1)
                mra, mdec = self._mount_j2000()
                sep = separation_deg(ra, dec, mra, mdec) * 60.0
                if sep > RESTORE_ACCEPT_ARCMIN:
                    raise NinaError(f"the mount is {sep:.1f} arcmin from its previous position after slewing back")
                self._log("OK", f"Mount returned to its previous position ({sep:.1f} arcmin off)")
            if self._guider_state() != "Guiding":
                self._settling = True
                self._api("/equipment/guider/start", {"calibrate": "false"}, timeout=(5, s.settle_timeout_s + 120), retries=1)
                self._wait_settled(s)
                self._settling = False
            if was_running and not self._sequence_running():
                try:
                    self._api("/sequence/start", {"skipValidation": "true"})
                except NinaError as exc:
                    if not (exc.status == 409 and "already running" in str(exc).lower()):
                        raise
                self._wait_for(self._sequence_running, 20, "sequence to report RUNNING")
            self._log("OK", "Session restored: mount, guiding and sequence are back as before")
            return True
        except Canceled:
            raise
        except NinaError as exc:
            self._log("WARN", f"Restore incomplete: {exc}")
            return False

    def _recovery_failed(self, exc: Exception, manual: bool, moved: bool, was_running: bool,
                         pre_pos: tuple[float, float] | None, star_loss: bool) -> None:
        s = self.settings
        self._log("ERROR", f"Recovery failed: {exc}")
        if moved:
            self._best_effort("/equipment/mount/slew/stop")    # a centering may still be running inside NINA
        restored = self._restore(s, moved, was_running, pre_pos)
        if manual:
            if restored:
                self._log("WARN", "The manual recovery failed, but the session was restored")
                self._set_phase("monitoring", "Monitoring") if self._armed else self._set_phase("idle", "Idle - not armed")
            else:
                self._halt(f"Manual recovery failed ({exc}) and the session could not be fully restored - check NINA.")
            return
        self._fail_count += 1
        self._last_star_loss = star_loss
        if self._fail_count > len(RETRY_DELAYS_S):
            self._halt(f"Recovery failed {self._fail_count} times in a row (last: {exc}). "
                       + ("The session was restored." if restored else "The session is NOT restored - guiding and imaging may be stopped.")
                       + " Automation stopped - check NINA.")
            return
        delay = RETRY_DELAYS_S[self._fail_count - 1]
        self._restore_needed = not restored
        self._pending_seq_restart = was_running and not restored
        self._retry_at = time.monotonic() + delay
        self._cooldown_until = 0.0
        self._breach_since = None
        self._log("WARN", f"Next attempt {self._fail_count + 1}/{len(RETRY_DELAYS_S) + 1} in "
                          f"{f'{delay // 60} min' if delay >= 60 else f'{delay} s'}"
                          + ("" if restored else " (the session is not restored - the recovery will run again then)"))

    def _recover(self, reason: str, manual: bool, star_loss: bool = False) -> None:
        s = self.settings
        reselect = s.reselect_star == "always" or (s.reselect_star == "star_loss" and star_loss)
        self._recovering = True
        self._breach_since = None
        slew_pending, moved, seq_was_running, pre_pos = False, False, False, None
        try:
            self._set_phase("recovering", "Recovering")
            self._log("WARN", f"=== RECOVERY START ({'manual' if manual else 'automatic'}: {reason}) ===")
            ra, dec = self._target_coords(s)
            old_lock = self._phd2_lock_position() if reselect else None     # before guiding is stopped
            pre_pos = self._chain_pre_pos if (self._fail_count and self._chain_pre_pos) else self._pre_position()
            if not self._fail_count:
                self._chain_pre_pos = pre_pos                                # pointing to return to if everything fails

            # 2. interrupt the exposure: stop the sequence (so it cannot start another exposure during centering)
            seq_was_running = self._sequence_running() or self._pending_seq_restart
            self._step(2, "interrupting exposure")
            if seq_was_running:
                self._api("/sequence/stop")
                self._wait_for(lambda: not self._sequence_running(), 45, "sequence to stop")
            try:
                self._api("/equipment/camera/abort-exposure")
            except NinaConnectionError:
                raise
            except NinaError as exc:
                self._log("INFO", f"abort-exposure: {exc}")
            self._wait_for(lambda: not self._camera_exposing(), 30, "camera to stop exposing")

            # 3. stop guiding
            self._step(3, "stopping guiding")
            self._api("/equipment/guider/stop")

            # 4. slew + plate solve + center
            self._step(4, f"slew / plate-solve / center on RA {ra:.4f} Dec {dec:+.4f}")
            slew_pending = moved = True
            try:
                res = self._client.request(
                    "/equipment/mount/slew",
                    {"ra": f"{ra:.6f}", "dec": f"{dec:.6f}", "center": "true", "waitForResult": "true"},
                    timeout=(5, s.recenter_timeout_s), cancel=self._abort)
                slew_pending = False
                if "finished" not in str(res.get("Response", "")).lower():
                    raise NinaError(f"Centering did not complete ({res.get('Response')!r})")
            except NinaError as exc:
                slew_pending = False
                body = exc.body or {}
                if not (exc.status == 200 and str(body.get("Response")) == "Slew failed" and not body.get("Error")):
                    raise
                self._accept_unflagged_center(ra, dec)
            self._log("OK", "Target re-centered")

            # 5. restart guiding and wait for it to settle
            self._step(5, "re-selecting guide star, restarting guiding" if reselect else "restarting guiding")
            self._settling = True
            if reselect:
                self._reselect_star(old_lock)
            self._api("/equipment/guider/start", {"calibrate": "false"}, timeout=(5, s.settle_timeout_s + 120), retries=1)
            self._wait_settled(s)
            self._settling = False
            if reselect:
                self._report_star(old_lock)

            # 6. resume imaging
            self._step(6, "resuming imaging")
            if seq_was_running:
                try:
                    self._api("/sequence/start", {"skipValidation": "true"})
                    self._log("OK", "Sequence start sent (NINA resumes at the interrupted instruction)")
                except NinaError as exc:
                    if exc.status == 409 and "already running" in str(exc).lower():
                        self._log("WARN", "NINA says the sequence is already running - it was started outside this app "
                                          "(e.g. manually in NINA) just before this step; carrying on")
                    else:
                        raise
                try:
                    self._wait_for(self._sequence_running, 20, "sequence to report RUNNING")
                except NinaError:
                    self._log("WARN", "Sequence did not report RUNNING within 20s - check NINA")
            else:
                self._log("INFO", "No sequence to restart (it was not running)")

            # 7. cooldown
            self._step(7, "cooldown")
            self._attempts_log()
            if self._armed:
                self._cooldown_until = time.monotonic() + s.cooldown_s
                self._set_phase("cooldown", f"Cooldown ({s.cooldown_s}s left)")
            else:
                self._set_phase("idle", "Idle - not armed")
            self._fail_count, self._restore_needed, self._retry_at = 0, False, None
            self._wait_since = self._wait_ok_since = None
            self._pending_seq_restart, self._chain_pre_pos = False, None
            self._log("OK", "=== RECOVERY COMPLETE ===")
        except Canceled:
            self._log("WARN", "Recovery aborted" + (" - slew stop requested" if slew_pending else ""))
            if slew_pending:
                self._best_effort("/equipment/mount/slew/stop")
            self._settling = False
            if self._quit.is_set():
                return
            self._disarm("recovery aborted")
            self._log("WARN", "Guiding / sequence may be stopped - check NINA")
        except PreflightError as exc:                         # nothing was touched yet
            if manual:
                self._log("ERROR", f"Recovery not started: {exc}")
                self._set_phase("monitoring", "Monitoring") if self._armed else self._set_phase("idle", "Idle - not armed")
            else:
                if self._attempts:
                    self._attempts.pop()
                    self.attempts_changed.emit(self._recent_attempts())
                self._halt(f"Recovery not started: {exc}. Guiding was left untouched.")
        except NinaError as exc:
            try:
                self._recovery_failed(exc, manual, moved, seq_was_running, pre_pos, star_loss)
            except Canceled:
                self._log("WARN", "Restore aborted")
                if not self._quit.is_set():
                    self._disarm("restore aborted")
                    self._log("WARN", "Guiding / sequence may be stopped - check NINA")
        finally:
            self._recovering = False
            self._settling = False

    def _phd2_lock_position(self) -> tuple[float, float] | None:
        host, port = self._phd2_endpoint()
        try:
            pos = phd2_call(host, port, "get_lock_position")
        except (OSError, ValueError, Phd2Error) as exc:
            self._log("WARN", f"PHD2 lock position unavailable ({exc}); the old guide star cannot be excluded")
            return None
        if isinstance(pos, list) and len(pos) == 2:
            self._log("INFO", f"PHD2 current guide star at ({pos[0]:.1f}, {pos[1]:.1f})")
            return float(pos[0]), float(pos[1])
        return None

    def _reselect_star(self, old: tuple[float, float] | None) -> None:
        """Best effort: have PHD2 loop on the re-centered field and pick a star away from the old one.

        Never aborts the recovery - on any problem PHD2 simply auto-selects when guiding restarts.
        """
        host, port = self._phd2_endpoint()
        try:
            exposure_ms = phd2_call(host, port, "get_exposure")
            phd2_call(host, port, "loop")                       # fresh frames of the new field (stop_capture left a stale one)
            self._log("INFO", "PHD2: looping to get a fresh frame of the re-centered field")
            self._sleep(max(3.0, 3 * float(exposure_ms or 1000) / 1000.0))
            frame = phd2_call(host, port, "get_camera_frame_size")
            phd2_call(host, port, "deselect_star")
            for roi in rois_excluding(old, (int(frame[0]), int(frame[1]))):
                try:
                    pos = phd2_call(host, port, "find_star", {"roi": roi} if roi else None, timeout=15)
                except Phd2Error as exc:
                    self._log("WARN", f"PHD2 find_star{' in ' + str(roi) if roi else ''}: {exc}")
                    continue
                where = f"region {roi}" if roi else "full frame (no region excluded the old star)"
                self._log("OK", f"PHD2 selected a guide star at ({pos[0]:.1f}, {pos[1]:.1f}) - {where}")
                return
            self._log("WARN", "PHD2 could not select a star; leaving selection to PHD2 when guiding restarts")
        except (OSError, ValueError, Phd2Error, TypeError, IndexError) as exc:
            self._log("WARN", f"Guide star re-select skipped: {exc}")

    def _report_star(self, old: tuple[float, float] | None) -> None:
        """After guiding restarted: say whether PHD2 ended up on a different star than before."""
        host, port = self._phd2_endpoint()
        try:
            pos = phd2_call(host, port, "get_lock_position")
        except (OSError, ValueError, Phd2Error):
            return
        if not (isinstance(pos, list) and len(pos) == 2):
            return
        if old is None:
            self._log("INFO", f"Guiding on star at ({pos[0]:.1f}, {pos[1]:.1f})")
        elif math.hypot(pos[0] - old[0], pos[1] - old[1]) <= 15:
            self._log("WARN", f"Guiding star is at ({pos[0]:.1f}, {pos[1]:.1f}), within 15 px of the old one - "
                              "PHD2 / NINA re-picked the same star")
        else:
            self._log("OK", f"Guiding on a different star at ({pos[0]:.1f}, {pos[1]:.1f}) "
                            f"(was ({old[0]:.1f}, {old[1]:.1f}))")

    def _accept_unflagged_center(self, ra: float, dec: float) -> None:
        """NINA's API plugin answers 200 / Success:false / "Slew failed" / no error text even when centering worked: it
        checks the Center instruction's Status after calling Execute(), but only Run() ever sets it (a real failure makes
        Execute() throw, which the plugin returns as HTTP 500). Verify with the mount position instead of halting."""
        try:
            mra, mdec = self._mount_j2000()
        except NinaError as exc:
            raise NinaError(f"NINA reported 'Slew failed' and the mount position could not be checked ({exc})") from exc
        sep = separation_deg(ra, dec, mra, mdec) * 60.0
        if sep > RECENTER_ACCEPT_ARCMIN:
            raise NinaError(f"NINA reported 'Slew failed' and the mount is {sep:.1f} arcmin from the target")
        self._log("WARN", f"NINA reported 'Slew failed' without an error (known API plugin quirk: a failed Center returns "
                          f"HTTP 500). Mount is {sep:.1f} arcmin from the target - accepting the centering")

    def _attempts_log(self) -> None:
        self.attempts_changed.emit(self._recent_attempts())

    def _best_effort(self, path: str) -> None:
        try:
            self._client.request(path, timeout=(2, 4))
        except NinaError:
            pass

    def _target_coords(self, s: Settings) -> tuple[float, float]:
        """J2000 degrees to center on: the running target (resolved fresh, before anything is stopped) or fixed coordinates."""
        if s.use_manual_target:
            self._log("INFO", f"Recenter target: fixed coordinates RA {s.manual_ra_deg:.5f} Dec {s.manual_dec_deg:+.5f}")
            return s.manual_ra_deg, s.manual_dec_deg
        try:
            tgt = self._resolve_target()
        except PreflightError:
            if not (self._restore_needed and self._cached_target):
                raise
            tgt = self._cached_target
            self._log("WARN", f"No running target (the failed recovery stopped the sequence) - using '{tgt.name}' from that attempt")
        self._cached_target = tgt
        self._log("INFO", f"Recenter target: '{tgt.name}' RA {tgt.ra_deg:.5f} Dec {tgt.dec_deg:+.5f} (J2000, from {tgt.source})")
        try:
            mra, mdec = self._mount_j2000()
        except NinaError as exc:
            raise PreflightError(f"cannot read mount position ({exc})") from exc
        sep = separation_deg(tgt.ra_deg, tgt.dec_deg, mra, mdec)
        if sep > MAX_TARGET_SEPARATION_DEG:
            raise PreflightError(f"mount is {sep:.1f} deg from '{tgt.name}' - refusing to slew back "
                                 f"(is the sequence still centering or has it moved on?)")
        return tgt.ra_deg, tgt.dec_deg

    def _camera_exposing(self) -> bool:
        try:
            return bool(self._client.request("/equipment/camera/info", quiet=True)["Response"].get("IsExposing"))
        except NinaError:
            return False

    def _wait_for(self, predicate, timeout_s: float, what: str) -> None:
        deadline = time.monotonic() + timeout_s
        while not predicate():
            if time.monotonic() > deadline:
                raise NinaError(f"Timed out waiting for {what}")
            self._sleep(1.0)

    def _wait_settled(self, s: Settings) -> None:
        """Settled = guider state 'Guiding' (and, with the PHD2 source, no PHD2 settle in progress) for settle_hold_s."""
        deadline, ok_since = time.monotonic() + s.settle_timeout_s, None
        self._log("INFO", f"Waiting for guiding to settle (hold {s.settle_hold_s}s, timeout {s.settle_timeout_s}s)")
        while True:
            self._abort_check()
            snap = self._poll()
            now = time.monotonic()
            if snap and snap["state"] == "Guiding" and not snap["phd2_settling"]:
                ok_since = ok_since or now
                if now - ok_since >= s.settle_hold_s:
                    self._log("OK", "Guiding settled")
                    return
            else:
                ok_since = None
            if now > deadline:
                raise NinaError("Guiding did not settle in time")
            self._sleep(1.0)


# --------------------------------------------------------------------------- guide-log replay
@dataclass
class ReplaySegment:
    """One guiding session (Guiding Begins ... Guiding Ends) of a PHD2 guide log, as PHD2 event-server messages."""
    start: datetime
    scale: float | None = None
    end_t: float = 0.0
    events: list = field(default_factory=list)            # (seconds since guiding start, PHD2 event dict)
    steps: int = 0
    drops: int = 0


@dataclass
class ReplaySession:
    index: int
    start: datetime
    seconds: float
    steps: int
    drops: int
    rms_median: float
    rms_p95: float
    rms_max: float
    evaluated_s: int
    recoveries: list                                      # [(datetime, reason)]
    waits: list                                           # [(datetime, message)] - waiting for a cloud-lost star
    star_back: int                                        # how many of those waits ended with the star back


@dataclass
class ReplayResult:
    settings: Settings
    poll: float
    sessions: list

    @property
    def recoveries(self) -> int:
        return sum(len(s.recoveries) for s in self.sessions)

    @property
    def evaluated_s(self) -> int:
        return sum(s.evaluated_s for s in self.sessions)


def replay_settings(**overrides) -> Settings:
    """Settings for a replay: the PHD2-source rule, the hourly limit lifted (so every trigger is listed) and no
    sequence information (a guide log has none)."""
    base = dict(rms_source="phd2", max_attempts_per_hour=10 ** 6, disarm_on_seq_end=False)
    base.update(overrides)
    return Settings(**base)


def default_guide_log_dir() -> str:
    home = os.path.expanduser("~")
    for p in (os.path.join(home, "Documents", "PHD2"), os.path.join(home, "OneDrive", "Documents", "PHD2"),
              os.path.join(home, "Documents"), home):
        if os.path.isdir(p):
            return p
    return home


def parse_guide_log(path: str, cancel: threading.Event | None = None) -> list[ReplaySegment]:
    """Split a PHD2 guide log into guiding sessions and turn its rows into the events PHD2's server would have sent."""
    segs: list[ReplaySegment] = []
    cur: ReplaySegment | None = None
    last_t = 0.0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, raw in enumerate(fh):
            if cancel is not None and n % 20000 == 0 and cancel.is_set():
                raise Canceled()
            line = raw.rstrip("\r\n")
            m = re.match(r"Guiding Begins at (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
            if m:
                cur = ReplaySegment(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                segs.append(cur)
                cur.events.append((0.0, {"Event": "StartGuiding"}))
                last_t = 0.0
                continue
            if cur is None:
                continue
            if line.startswith("Guiding Ends"):
                cur.events.append((last_t + 0.01, {"Event": "GuidingStopped"}))
                cur.end_t = last_t + 0.01
                cur = None
                continue
            m = re.match(r"Pixel scale = ([\d.]+) arc-sec/px", line)
            if m:
                cur.scale = float(m.group(1))
            elif line.startswith("INFO: DITHER by"):
                cur.events.append((last_t + 0.0005, {"Event": "GuidingDithered"}))
            elif line.startswith("INFO: SETTLING STATE CHANGE"):
                ev = "SettleBegin" if "started" in line else "SettleDone"
                cur.events.append((last_t + 0.001, {"Event": ev}))
            elif re.match(r"^\d+,", line):
                try:
                    row = next(csv.reader([line]))
                    last_t = float(row[1])
                    code = int(row[17] or 0)
                    if row[2] == "DROP":
                        cur.drops += 1
                        cur.events.append((last_t, {"Event": "StarLost", "Status": row[-1], "ErrorCode": code}))
                    else:
                        cur.steps += 1
                        ev = {"Event": "GuideStep", "RADistanceRaw": float(row[5]), "DECDistanceRaw": float(row[6])}
                        if code:                           # PHD2's server only sends ErrorCode when non-zero
                            ev["ErrorCode"] = code
                        cur.events.append((last_t, ev))
                except (ValueError, IndexError):
                    continue                              # a damaged row: skip it, keep the session
    if cur is not None:                                    # log cut off mid-session
        cur.end_t = last_t + 0.01
    return segs


def run_replay(segs: list[ReplaySegment], s: Settings, poll: float = 1.0, progress=None,
               cancel: threading.Event | None = None) -> ReplayResult:
    """Feed each session, frame by frame, through the real Phd2Monitor / GuardWorker._evaluate on a simulated clock.

    The recovery itself is replaced by a recorder, so the result says when (and why) a recovery would have started.
    A recovery is assumed to be followed by the cooldown, with the RMS / star-loss windows cleared (as a guiding
    restart does). `progress(fraction, text)` is called as the replay advances; `cancel` aborts it with Canceled.
    """
    sim = _SimClock()
    quiet = logging.getLogger("guideguardian.replay")          # the replay's log lines must not reach guide_guardian.log
    quiet.propagate = False
    if not quiet.handlers:
        quiet.addHandler(logging.NullHandler())
    total_t = sum(seg.end_t + poll for seg in segs) or 1.0
    done_t, last_pct = 0.0, -1
    sessions: list[ReplaySession] = []
    time.install(sim)
    try:
        w = GuardWorker(s)                                 # never started: only its evaluation code is used
        w._file_log = quiet
        w._armed = True
        triggers: list[tuple] = []
        notes: list[tuple[float, str]] = []                # waiting-for-star events: (simulated time, message)
        mon = Phd2Monitor("replay", 0, s.phd2_window, s.lost_window, s.recent_steps)
        mon._connected = True

        def fake_recover(reason, manual=False, star_loss=False):
            triggers.append((sim.now, reason))
            w._breach_since = None
            w._cooldown_until = sim.now + s.cooldown_s
            mon._clear()

        w._recover = fake_recover
        w.log.connect(lambda ts, lv, msg: notes.append((sim.now, msg)) if any(
            k in msg for k in ("Waiting up to", "Guide star is back", "still missing")) else None)
        for n, seg in enumerate(segs, 1):
            mon._clear()
            mon._state, mon._settling, mon._scale = "Unknown", False, seg.scale
            w._cooldown_until, w._breach_since = 0.0, None
            triggers.clear()
            notes.clear()
            t0 = seg.start.timestamp()
            rms_vals: list[float] = []
            i, tick = 0, 0.0
            while tick <= seg.end_t + poll:
                while i < len(seg.events) and seg.events[i][0] <= tick:
                    mon._handle(seg.events[i][1])
                    i += 1
                sim.now = t0 + tick
                view = w._phd2_view(mon.snapshot())
                if view["total"] is not None and view["state"] == "Guiding" and not view["phd2_settling"]:
                    rms_vals.append(view["total"])
                w._evaluate(view)
                tick += poll
                if progress is not None or cancel is not None:
                    pct = int(100 * (done_t + min(tick, seg.end_t + poll)) / total_t)
                    if pct != last_pct:
                        last_pct = pct
                        if cancel is not None and cancel.is_set():
                            raise Canceled()
                        if progress is not None:
                            progress(pct / 100.0, f"Session {n} of {len(segs)}")
            done_t += seg.end_t + poll
            q = statistics.quantiles(rms_vals, n=20) if len(rms_vals) > 20 else [0.0] * 19
            sessions.append(ReplaySession(
                index=n, start=seg.start, seconds=seg.end_t, steps=seg.steps, drops=seg.drops,
                rms_median=statistics.median(rms_vals) if rms_vals else 0.0, rms_p95=q[18],
                rms_max=max(rms_vals, default=0.0), evaluated_s=len(rms_vals),
                recoveries=[(datetime.fromtimestamp(ts), why) for ts, why in triggers],
                waits=[(datetime.fromtimestamp(ts), m) for ts, m in notes if "Waiting up to" in m],
                star_back=sum(1 for _, m in notes if "Guide star is back" in m)))
    finally:
        time.install(None)
    return ReplayResult(s, poll, sessions)


def describe_rule(s: Settings) -> str:
    return (f"threshold {s.threshold:g} {s.unit}, sustain {s.sustain_s}s, window {s.phd2_window} steps, "
            f"recent {s.recent_steps} steps, cooldown {s.cooldown_s}s"
            + (f", star-loss {s.lost_pct}% of {s.lost_window} frames" if s.lost_pct_enabled else "")
            + (f", wait for star {s.wait_for_star_s}s" if s.wait_for_star_s else ""))


def session_summary(sess: ReplaySession) -> str:
    """The 'triggers / waits' column of the report."""
    desc = "-"
    if sess.recoveries:
        when, why = sess.recoveries[0]
        desc = f"{len(sess.recoveries)}  first {when:%H:%M:%S}: {why}"
    if sess.waits:
        desc = (desc if sess.recoveries else "") + f"  | {len(sess.waits)} wait(s) for the star, {sess.star_back} ended with it back"
        if not sess.recoveries:
            desc = desc.strip(" |")
    return desc


def _events_in_order(sess: ReplaySession) -> list[tuple[datetime, str, str]]:
    """Recoveries and star waits of one session, in time order: (when, label, text)."""
    ev = [(when, "recovery would start", why) for when, why in sess.recoveries]
    ev += [(when, "waiting for the star", msg) for when, msg in sess.waits]
    return sorted(ev, key=lambda e: e[0])


def session_detail(sess: ReplaySession, unit: str) -> str:
    lines = [f"Session {sess.index} - guiding began {sess.start:%Y-%m-%d %H:%M:%S}, {sess.seconds / 60:.0f} min, "
             f"{sess.steps} steps, {sess.drops} dropped frames",
             f"RMS ({unit}): median {sess.rms_median:.2f}, 95% {sess.rms_p95:.2f}, max {sess.rms_max:.2f} "
             f"({sess.evaluated_s} evaluated seconds)"]
    if not sess.recoveries:
        lines.append("No recovery would have been triggered.")
    for when, label, text in _events_in_order(sess):
        lines.append(f"{when:%H:%M:%S}  {label}: {text}")
    if sess.waits:
        lines.append(f"{sess.star_back} of {len(sess.waits)} waits ended with the star back")
    return "\n".join(lines)


def format_replay(result: ReplayResult, quiet: bool = False, full: bool = False) -> str:
    """Plain-text report: one row per guiding session. `full` lists every recovery and wait under its row."""
    s = result.settings
    out = [f"Settings: {describe_rule(s)}",
           f"{'#':>2} {'start':<16}{'min':>6}{'steps':>7}{'drops':>7}{'RMS p50':>9}{'p95':>7}{'max':>7}  triggers / waits"]
    for sess in result.sessions:
        out.append(f"{sess.index:>2} {sess.start:%m-%d %H:%M:%S}{sess.seconds / 60:>6.0f}{sess.steps:>7}{sess.drops:>7}"
                   f"{sess.rms_median:>9.2f}{sess.rms_p95:>7.2f}{sess.rms_max:>7.2f}  {session_summary(sess)}")
        if full:
            out += [f"{'':>45}  {when:%H:%M:%S} {label}: {text}" for when, label, text in _events_in_order(sess)]
        elif not quiet and len(sess.recoveries) > 1:
            out += [f"{'':>45}  also {when:%H:%M:%S}: {why}" for when, why in sess.recoveries[1:6]]
    out.append("")
    n, k = len(result.sessions), result.recoveries
    out.append(f"{n} guiding session{'s' if n != 1 else ''}, {k} recover{'y' if k == 1 else 'ies'} would have been "
               f"triggered ({result.evaluated_s} evaluated seconds).")
    return "\n".join(out)


# --------------------------------------------------------------------------- watchdog debug log + session report
# What the guide log alone cannot say: whether the watchdog was armed, what it saw and what it actually did. The verbose
# debug log records that; this section reads it, lines it up with the PHD2 guide log and the replay, and writes a report.
DEBUG_TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
DEBUG_KEEP_TAGS = frozenset({"INFO", "WARN", "OK", "ERROR", "IMAGE", "CONFIG", "START", "TRACE"})
MATCH_TOLERANCE_S = 180.0         # a replayed trigger and an actual recovery this close together are the same event
SHORT_BREACH_S = 10.0             # breaches shorter than this are counted, not listed
QUALITY_BEFORE_S = 300.0          # guiding quality window before a recovery ...
QUALITY_AFTER_S = 600.0           # ... and after guiding resumed
IMAGE_KEYS = ("TargetName", "Filter", "ExposureTime", "HFR", "Stars", "RmsText", "Median", "Date", "Filename")
# settings that did not exist (or were not on) in older versions of the app: absent from an old CONFIG line = off
LOGGED_RULE_OFF = {"recent_steps": 0, "wait_for_star_s": 0, "lost_pct_enabled": False}
LOGGED_RULE_KEYS = ("threshold", "unit", "sustain_s", "phd2_window", "recent_steps", "cooldown_s", "lost_pct_enabled",
                    "lost_pct", "lost_window", "wait_for_star_s")


@dataclass
class DebugEvent:
    when: datetime
    tag: str
    msg: str


@dataclass
class ArmPeriod:
    start: datetime
    end: datetime | None = None
    auto: bool = False
    rule: str = ""
    ended_by: str = ""


@dataclass
class BreachRecord:
    start: datetime
    reason: str
    end: datetime | None = None
    held_s: float = 0.0
    outcome: str = "unresolved"        # cleared by itself | became a recovery | star wait | unresolved


@dataclass
class StarWait:
    start: datetime
    detail: str = ""
    end: datetime | None = None
    waited_s: float = 0.0
    outcome: str = "unresolved"        # star came back | gave up, recovery started | unresolved


@dataclass
class RecoveryRecord:
    start: datetime
    kind: str                          # automatic | manual | retry
    reason: str
    attempt: str = ""
    target: str = ""
    end: datetime | None = None
    outcome: str = "unfinished"        # complete | failed | aborted | unfinished
    error: str = ""
    steps: dict = field(default_factory=dict)          # step number -> when it began
    old_star: tuple | None = None
    new_star: tuple | None = None
    star_changed: bool | None = None
    center_offset_arcmin: float | None = None
    centered_at: datetime | None = None
    settled_at: datetime | None = None
    sequence_restarted_at: datetime | None = None
    restored: bool | None = None
    retry_info: str = ""
    notes: list = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds() if self.end else 0.0

    @property
    def resumed_at(self) -> datetime | None:
        return self.settled_at or self.sequence_restarted_at or self.end


@dataclass
class WatchdogTimeline:
    files: list
    first: datetime | None = None
    last: datetime | None = None
    app_info: str = ""
    stopped: datetime | None = None
    configs: list = field(default_factory=list)        # [(when, settings dict)]
    arms: list = field(default_factory=list)
    breaches: list = field(default_factory=list)
    waits: list = field(default_factory=list)
    recoveries: list = field(default_factory=list)
    halts: list = field(default_factory=list)          # [(when, message)]
    outages: list = field(default_factory=list)        # [(what, start, end | None)]
    targets: list = field(default_factory=list)        # [(when, name)]
    sequence: list = field(default_factory=list)       # [(when, message)]
    setting_changes: list = field(default_factory=list)
    images: list = field(default_factory=list)         # [{"when", "hfr", "stars", "filter", ...}]
    errors: list = field(default_factory=list)         # [(when, message)]

    def armed_at(self, when: datetime) -> bool:
        return any(a.start <= when <= (a.end or self.last or when) for a in self.arms)

    def armed_seconds(self, a: datetime, b: datetime) -> float:
        total = 0.0
        for arm in self.arms:
            lo, hi = max(arm.start, a), min(arm.end or self.last or b, b)
            if hi > lo:
                total += (hi - lo).total_seconds()
        return total


def debug_log_files(path: str) -> list[str]:
    """The rotated set `path` belongs to, oldest first (base.log.N ... base.log.1, base.log)."""
    path = os.path.normpath(path)
    base = re.sub(r"\.\d+$", "", path) if re.search(r"\.log\.\d+$", path, re.I) else path
    rotated = []
    for p in glob.glob(glob.escape(base) + ".*"):
        m = re.search(r"\.(\d+)$", p)
        if m:
            rotated.append((int(m.group(1)), p))
    files = [p for _, p in sorted(rotated, reverse=True)]
    if os.path.isfile(base):
        files.append(base)
    return files or ([path] if os.path.isfile(path) else [])


def parse_debug_log(files: list[str], progress=None, cancel: threading.Event | None = None) -> list[DebugEvent]:
    """The lines of the verbose debug log that describe what the watchdog did (the call-by-call traffic is skipped)."""
    total = sum(os.path.getsize(p) for p in files) or 1
    read, events = 0, []
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh):
                read += len(line)
                if n % 20000 == 0:
                    if cancel is not None and cancel.is_set():
                        raise Canceled()
                    if progress is not None:
                        progress(min(1.0, read / total))
                if len(line) < 32 or line[4] != "-" or line[10] != " ":
                    continue
                tag = line[24:30].strip()
                if tag not in DEBUG_KEEP_TAGS:
                    continue
                try:
                    when = datetime.strptime(line[:23], DEBUG_TS_FORMAT)
                except ValueError:
                    continue
                events.append(DebugEvent(when, tag, line[31:].rstrip("\r\n")))
    return events


def _parse_image(msg: str, when: datetime) -> dict | None:
    parts = re.split(r"\b(" + "|".join(IMAGE_KEYS) + r")=", msg)
    d = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if not d:
        return None
    out = {"when": when, "filter": d.get("Filter", ""), "target": d.get("TargetName", ""),
           "file": d.get("Filename", ""), "rms": d.get("RmsText", "")}
    for key, name, cast in (("HFR", "hfr", float), ("Stars", "stars", int), ("ExposureTime", "exposure", float)):
        try:
            out[name] = cast(d.get(key, ""))
        except ValueError:
            out[name] = None
    return out


def _xy(text: str) -> tuple[float, float] | None:
    m = re.search(r"\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)", text)
    return (float(m.group(1)), float(m.group(2))) if m else None


def build_timeline(events: list[DebugEvent], files: list[str]) -> WatchdogTimeline:
    """Read the debug-log lines as a story: arm periods, breaches, star waits, recoveries and everything around them.

    The wording differs a little between versions of the app (older logs say "Threshold breach started", newer ones
    "Guiding degraded for ..."), so each fact is matched on its stable part.
    """
    tl = WatchdogTimeline(files=list(files))
    st = {"arm": None, "breach": None, "wait": None, "rec": None, "last_rec": None, "auto": None,
          "attempt": "", "retry": False}
    outage: dict[str, datetime] = {}

    def close_arm(when: datetime, by: str) -> None:
        if st["arm"] is not None:
            st["arm"].end, st["arm"].ended_by = when, by
            st["arm"] = None

    def recovery_line(cur: RecoveryRecord, m: str, when: datetime, tag: str) -> bool:
        """Facts about the recovery that is running (or just failed). True when the line was consumed."""
        running = cur is st["rec"]
        if m.startswith("Recovery step"):
            mm = re.match(r"Recovery step (\d+)/7", m)
            if mm and running:
                cur.steps[int(mm.group(1))] = when
        elif m.startswith("Recenter target:"):
            mm = re.match(r"Recenter target: '(.*)' RA", m)
            cur.target = mm.group(1) if mm else cur.target
        elif m.startswith("PHD2 current guide star at"):
            cur.old_star = _xy(m)
        elif m.startswith("Guiding on a different star"):
            cur.new_star, cur.star_changed = _xy(m), True
        elif m.startswith("Guiding on star at") or m.startswith("Guiding star is at"):
            cur.new_star, cur.star_changed = _xy(m), False
        elif m.startswith("PHD2 selected a guide star"):
            cur.new_star = cur.new_star or _xy(m)
        elif "'Slew failed' without an error" in m:
            mm = re.search(r"Mount is ([\d.]+) arcmin", m)
            cur.center_offset_arcmin = float(mm.group(1)) if mm else None
        elif re.match(r"Target re-cent(er|re)d", m):
            cur.centered_at = when
        elif m == "Guiding settled":
            cur.settled_at = when
        elif m.startswith("Sequence start sent"):
            cur.sequence_restarted_at = when
        elif m == "=== RECOVERY COMPLETE ===":
            if running:
                cur.end, cur.outcome = when, "complete"
                st["last_rec"], st["rec"] = cur, None
        elif m.startswith("Recovery failed:"):
            cur.end, cur.outcome, cur.error = when, "failed", m.split(":", 1)[1].strip()
            st["last_rec"], st["rec"] = cur, None
        elif m.startswith("Recovery aborted"):
            cur.end, cur.outcome = when, "aborted"
            st["last_rec"], st["rec"] = cur, None
        elif m.startswith("Session restored") or m.startswith("The manual recovery failed, but the session was restored"):
            cur.restored = True
        elif m.startswith("Restore incomplete"):
            cur.restored = False
            cur.notes.append(f"{when:%H:%M:%S} {m}")
        elif m.startswith("Next attempt"):
            cur.retry_info = m
        elif tag in ("WARN", "ERROR") and not m.startswith(("Setting changed", "Star lost for", "Guiding degraded")):
            cur.notes.append(f"{when:%H:%M:%S} {m}")
        else:
            return False
        return True

    for ev in events:
        m, when, tag = ev.msg, ev.when, ev.tag
        if tl.first is None:
            tl.first = when
        tl.last = when
        if tag == "START":
            tl.app_info = tl.app_info or m
            close_arm(when, "debug log restarted")
            continue
        if tag == "CONFIG":
            try:
                tl.configs.append((when, json.loads(m)))
            except ValueError:
                pass
            continue
        if tag == "IMAGE":
            img = _parse_image(m, when)
            if img:
                tl.images.append(img)
            continue
        if tag == "TRACE":
            tl.errors.append((when, m.splitlines()[0][:200] if m else "traceback"))
            continue

        breach, wait = st["breach"], st["wait"]
        if m.startswith("Guarding armed"):
            close_arm(when, "re-armed")
            auto = st["auto"] is not None and (when - st["auto"]).total_seconds() < 5
            st["arm"] = ArmPeriod(when, auto=auto, rule=m.split(":", 1)[1].strip() if ":" in m else "")
            tl.arms.append(st["arm"])
            st["auto"] = None
        elif m.startswith("Auto-arm:"):
            st["auto"] = when
        elif m.startswith("Guarding disarmed"):
            why = re.search(r"\((.*)\)", m)
            close_arm(when, why.group(1) if why else "disarmed")
            if wait is not None:
                wait.end, wait.outcome = when, "interrupted by disarm"
                st["wait"] = None
        elif m.startswith("SAFETY STOP:"):
            tl.halts.append((when, m[len("SAFETY STOP:"):].strip()))
            close_arm(when, "safety stop")
        elif m == "Worker stopped":
            tl.stopped = when
            close_arm(when, "app closed")
        elif m.startswith("Threshold breach started:"):             # older versions: every breach, however short
            if breach is None:
                st["breach"] = BreachRecord(when, m.split(":", 1)[1].strip())
                tl.breaches.append(st["breach"])
        elif m.startswith("Guiding degraded for"):                  # newer versions: only once it lasted >= 10 s
            mm = re.match(r"Guiding degraded for (\d+)s: (.*) \(recovery starts after", m)
            if mm and breach is None:
                st["breach"] = BreachRecord(when - timedelta(seconds=int(mm.group(1))), mm.group(2))
                tl.breaches.append(st["breach"])
        elif m.startswith("Guiding recovered by itself after"):
            mm = re.search(r"after (\d+)s", m)
            held = float(mm.group(1)) if mm else 0.0
            if breach is None:
                breach = BreachRecord(when - timedelta(seconds=held), "(reason not logged)")
                tl.breaches.append(breach)
            breach.end, breach.held_s, breach.outcome = when, held, "cleared by itself"
            st["breach"] = None
        elif m.startswith("BREACH CONFIRMED") or m.startswith("RETRY "):
            mm = re.search(r"starting recovery (\d+)/(\d+)", m)
            st["attempt"] = f"{mm.group(1)} of {mm.group(2)} this hour" if mm else ""
            st["retry"] = m.startswith("RETRY ")
            hold = re.search(r" for (\d+)s\)", m)
            if breach is not None:
                breach.end, breach.outcome = when, "became a recovery"
                breach.held_s = float(hold.group(1)) if hold else (when - breach.start).total_seconds()
                st["breach"] = None
        elif m.startswith("Star lost for") and "Waiting up to" in m:
            mm = re.match(r"Star lost for (\d+)s", m)
            st["wait"] = StarWait(when - timedelta(seconds=int(mm.group(1))) if mm else when, m)
            tl.waits.append(st["wait"])
            if breach is not None:
                breach.end, breach.outcome = when, "star wait"
                breach.held_s = (when - breach.start).total_seconds()
                st["breach"] = None
        elif m.startswith("Guide star is back after waiting") or m.startswith("Guide star still missing"):
            if wait is not None:
                mm = re.search(r"waiting (\d+)s", m)
                wait.end = when
                wait.outcome = "star came back" if "is back" in m else "gave up, recovery started"
                wait.waited_s = float(mm.group(1)) if mm else (when - wait.start).total_seconds()
                st["wait"] = None
        elif m.startswith("=== RECOVERY START"):
            mm = re.match(r"=== RECOVERY START \((\w+): (.*)\) ===", m)
            kind = mm.group(1) if mm else "automatic"
            rec = RecoveryRecord(when, "retry" if st["retry"] and kind == "automatic" else kind,
                                 mm.group(2) if mm else m, attempt=st["attempt"])
            tl.recoveries.append(rec)
            st["rec"] = rec
            st["attempt"], st["retry"] = "", False
            if breach is not None:
                breach.end, breach.outcome = when, "became a recovery"
                breach.held_s = (when - breach.start).total_seconds()
                st["breach"] = None
        elif m.startswith("Cannot reach NINA API"):
            outage.setdefault("NINA API", when)
        elif m.startswith("Connected to NINA API"):
            if "NINA API" in outage:
                tl.outages.append(("NINA API", outage.pop("NINA API"), when))
        elif m.startswith("Cannot reach PHD2 server"):
            outage.setdefault("PHD2 server", when)
        elif m.startswith("Connected to PHD2 server"):
            if "PHD2 server" in outage:
                tl.outages.append(("PHD2 server", outage.pop("PHD2 server"), when))
        elif m.startswith("Recenter target - "):
            name = m[len("Recenter target - "):].split(":", 1)[0]
            if not tl.targets or tl.targets[-1][1] != name:
                tl.targets.append((when, name))
        elif m.startswith("Setting changed:"):
            tl.setting_changes.append((when, m))
        elif m.startswith("Sequence has not been running"):
            tl.sequence.append((when, m))
        else:
            cur = st["rec"]
            if cur is None and st["last_rec"] is not None and st["last_rec"].end \
                    and (when - st["last_rec"].end).total_seconds() < 900:
                cur = st["last_rec"]                              # restore / retry lines after a failed recovery
            if (cur is None or not recovery_line(cur, m, when, tag)) and tag == "ERROR" \
                    and not m.startswith("SAFETY STOP"):
                tl.errors.append((when, m))
    for what, start in outage.items():
        tl.outages.append((what, start, None))
    if st["arm"] is not None:
        st["arm"].end, st["arm"].ended_by = None, "still armed at the end of the log"
    return tl


def logged_rule(tl: WatchdogTimeline, base: Settings | None = None) -> tuple[Settings, str, list[str]]:
    """The rule that was in force: the CONFIG line in effect at the first recovery (else the first arming, else the
    last). Settings that older versions did not have are taken as off. Returns (settings, where it came from, notes)."""
    if not tl.configs:
        return base or replay_settings(), "app defaults (the debug log has no CONFIG line)", []
    anchor = (tl.recoveries[0].start if tl.recoveries else tl.arms[0].start if tl.arms else None)
    cfg_when, cfg = tl.configs[-1]
    if anchor is not None:
        before = [c for c in tl.configs if c[0] <= anchor]
        if before:
            cfg_when, cfg = before[-1]
    notes = []
    obsolete = [k for k in ("trigger_on_lost_lock",) if cfg.get(k)]
    if obsolete:
        notes.append("The logged rule also used an option that no longer exists (" + ", ".join(obsolete)
                     + "), so the replay of it is approximate.")
    if len({json.dumps(c, sort_keys=True) for _, c in tl.configs}) > 1:
        notes.append(f"The settings changed during the log ({len(tl.configs)} CONFIG lines); the rule shown is the one "
                     f"in force at {cfg_when:%H:%M:%S}.")
    d = Settings()
    over = {k: cfg[k] for k in LOGGED_RULE_KEYS if k in cfg}
    for k, off in LOGGED_RULE_OFF.items():
        over.setdefault(k, off)
    over.setdefault("unit", d.unit)
    try:
        s = replay_settings(**over)
    except TypeError:
        s = replay_settings()
    return s, f"the debug log (CONFIG at {cfg_when:%Y-%m-%d %H:%M:%S})", notes


# ----- guiding quality around events ---------------------------------------------------------
class GuideSeries:
    """Per-frame data of a PHD2 guide log (settling frames left out of the RMS), for statistics over a time window."""

    def __init__(self, segs: list[ReplaySegment]):
        self._segs = []
        for seg in segs:
            t0 = seg.start.timestamp()
            times, ra, dec, drops = [], [], [], []
            settling = False
            for t, ev in seg.events:
                e = ev.get("Event")
                if e == "GuideStep":
                    if not settling:
                        times.append(t0 + t)
                        ra.append(ev["RADistanceRaw"])
                        dec.append(ev["DECDistanceRaw"])
                elif e == "StarLost":
                    drops.append(t0 + t)
                elif e == "SettleBegin":
                    settling = True
                elif e == "SettleDone":
                    settling = False
            self._segs.append((seg.scale, times, ra, dec, drops))

    def stats(self, a: datetime, b: datetime, arcsec: bool = True) -> dict | None:
        ta, tb = a.timestamp(), b.timestamp()
        n, ss, drops, scaled = 0, 0.0, 0, False
        for scale, times, ra, dec, dr in self._segs:
            if not times and not dr:
                continue
            lo, hi = bisect.bisect_left(times, ta), bisect.bisect_right(times, tb)
            k = (scale or 1.0) if arcsec and scale else 1.0
            scaled = scaled or (arcsec and bool(scale))
            for i in range(lo, hi):
                ss += (ra[i] * ra[i] + dec[i] * dec[i]) * k * k
            n += hi - lo
            drops += bisect.bisect_right(dr, tb) - bisect.bisect_left(dr, ta)
        if n < 5:
            return None
        return {"rms": math.sqrt(ss / n), "steps": n, "drops": drops, "lost_pct": 100.0 * drops / (n + drops),
                "unit": "arcsec" if scaled else "px"}


@dataclass
class RecoveryQuality:
    rec: RecoveryRecord
    before: dict | None = None
    after: dict | None = None
    hfr_before: float | None = None
    hfr_after: float | None = None

    @property
    def improved(self) -> bool | None:
        """True when the RMS after the recovery is clearly lower (or far fewer frames are lost) than before it."""
        if self.before is None or self.after is None or self.before["unit"] != self.after["unit"]:
            return None
        return self.after["rms"] <= 0.9 * self.before["rms"] or self.after["lost_pct"] + 5 <= self.before["lost_pct"]


@dataclass
class SessionAnalysis:
    index: int
    covered_s: float = 0.0                                # part of the session inside the debug log's time span
    armed_s: float = 0.0                                  # ... during which the watchdog was armed
    recoveries: list = field(default_factory=list)
    breaches: list = field(default_factory=list)          # only those >= SHORT_BREACH_S or that led somewhere
    short_breaches: int = 0
    waits: list = field(default_factory=list)
    unarmed_triggers: list = field(default_factory=list)  # [(when, reason)] the rule fires, the watchdog was not armed
    missed_triggers: list = field(default_factory=list)   # [(when, reason)] armed, the rule fires, nothing was done


@dataclass
class SessionReport:
    result: ReplayResult
    timeline: WatchdogTimeline | None = None
    guide_log: str = ""
    rule_source: str = ""
    rule_notes: list = field(default_factory=list)
    sessions: list = field(default_factory=list)          # SessionAnalysis per guiding session
    qualities: list = field(default_factory=list)
    unmatched_recoveries: list = field(default_factory=list)   # not reproduced by the replayed rule
    edge_recoveries: list = field(default_factory=list)        # began as guiding stopped: the log ends before the hold time
    outside_recoveries: list = field(default_factory=list)
    findings: list = field(default_factory=list)


def _dur(seconds: float) -> str:
    sec = int(round(seconds))
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s" if m else f"{s}s"


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def analyze_session(result: ReplayResult, segs: list[ReplaySegment], tl: WatchdogTimeline | None,
                    guide_log: str = "", rule_source: str = "", rule_notes: list | None = None) -> SessionReport:
    rep = SessionReport(result, tl, guide_log, rule_source, list(rule_notes or []))
    sess_list = result.sessions
    spans = [(s.start, s.start + timedelta(seconds=s.seconds)) for s in sess_list]
    rep.sessions = [SessionAnalysis(s.index) for s in sess_list]
    if tl is None:
        return rep
    series = GuideSeries(segs)
    arcsec = result.settings.unit != "pixel"

    def owner(when: datetime, slack_s: float = 300.0) -> int | None:
        """Index of the guiding session that was running (or had just ended) at `when`."""
        best = None
        for i, (a, b) in enumerate(spans):
            if a <= when:
                best = i
        if best is not None and when <= spans[best][1] + timedelta(seconds=slack_s):
            return best
        return None

    log_lo, log_hi = tl.first, tl.last
    for i, (a, b) in enumerate(spans):
        if log_lo and log_hi:
            lo, hi = max(a, log_lo), min(b, log_hi)
            rep.sessions[i].covered_s = max(0.0, (hi - lo).total_seconds())
            rep.sessions[i].armed_s = tl.armed_seconds(lo, hi) if hi > lo else 0.0
    for r in tl.recoveries:
        i = owner(r.start)
        (rep.sessions[i].recoveries if i is not None else rep.outside_recoveries).append(r)
    for br in tl.breaches:
        i = owner(br.start, 60.0)
        if i is None:
            continue
        if br.outcome == "cleared by itself" and br.held_s < SHORT_BREACH_S:
            rep.sessions[i].short_breaches += 1
        else:
            rep.sessions[i].breaches.append(br)
    for w in tl.waits:
        i = owner(w.start, 60.0)
        if i is not None:
            rep.sessions[i].waits.append(w)

    # replayed triggers against what the watchdog really did
    tol = timedelta(seconds=MATCH_TOLERANCE_S)
    matched: set[int] = set()
    for i, sess in enumerate(sess_list):
        for when, why in sess.recoveries:
            hit = next((j for j, r in enumerate(tl.recoveries) if abs(r.start - when) <= tol), None)
            if hit is not None:
                matched.add(hit)
            elif not (log_lo and log_hi and log_lo <= when <= log_hi):
                continue                                      # before / after the debug log: nothing is known
            elif not tl.armed_at(when):
                rep.sessions[i].unarmed_triggers.append((when, why))
            else:
                rep.sessions[i].missed_triggers.append((when, why))
    edge = timedelta(seconds=15)
    for j, r in enumerate(tl.recoveries):
        if j in matched or r.kind != "automatic":             # manual triggers and retries are not the rule's doing
            continue
        i = owner(r.start)
        if i is not None and r.start <= spans[i][1] + edge:
            rep.edge_recoveries.append(r)                     # the recovery itself ended the guide-log session
        else:
            rep.unmatched_recoveries.append(r)

    # did each recovery help?
    for r in tl.recoveries:
        q = RecoveryQuality(r)
        q.before = series.stats(r.start - timedelta(seconds=QUALITY_BEFORE_S), r.start, arcsec)
        resumed = r.resumed_at
        if resumed is not None and r.outcome == "complete":
            q.after = series.stats(resumed, resumed + timedelta(seconds=QUALITY_AFTER_S), arcsec)
        if tl.images:
            pre = [im["hfr"] for im in tl.images if im.get("hfr") and r.start - timedelta(minutes=10) <= im["when"] <= r.start]
            post = [im["hfr"] for im in tl.images if im.get("hfr") and resumed and resumed <= im["when"] <= resumed + timedelta(minutes=15)]
            q.hfr_before, q.hfr_after = _median(pre), _median(post)
        rep.qualities.append(q)

    # findings
    f = rep.findings
    guiding_s = sum(s.seconds for s in sess_list)
    covered_s = sum(a.covered_s for a in rep.sessions)
    armed_s = sum(a.armed_s for a in rep.sessions)
    if covered_s > 0:
        pct = 100.0 * armed_s / covered_s
        if pct < 98:
            f.append(f"The watchdog was armed for {pct:.0f}% of the {_dur(covered_s)} of guiding that the debug log "
                     f"covers; {_dur(covered_s - armed_s)} had no watchdog.")
        else:
            f.append(f"The watchdog was armed for {pct:.0f}% of the guiding time the debug log covers.")
    if guiding_s - covered_s > 60:
        f.append(f"{_dur(guiding_s - covered_s)} of the guiding is outside the debug log's time span, so nothing is "
                 f"known about the watchdog then.")
    for a in rep.sessions:
        for when, why in a.unarmed_triggers:
            f.append(f"Session {a.index}, {when:%H:%M:%S}: the rule would have started a recovery ({why}), but the "
                     f"watchdog was not armed.")
        for when, why in a.missed_triggers:
            f.append(f"Session {a.index}, {when:%H:%M:%S}: the replayed rule starts a recovery ({why}) but the watchdog "
                     f"did nothing - settings or RMS source differed, or it was in cooldown / waiting.")
    for r in rep.unmatched_recoveries:
        f.append(f"The recovery at {r.start:%H:%M:%S} ({r.reason}) is not reproduced by the replayed rule - the rule "
                 f"in force used different settings or a different RMS source.")
    for q in rep.qualities:
        r = q.rec
        if r.outcome != "complete":
            f.append(f"The recovery at {r.start:%H:%M:%S} did not complete ({r.outcome}"
                     + (f": {r.error}" if r.error else "") + ")"
                     + (", the session was restored." if r.restored else
                        ", the session was NOT fully restored." if r.restored is False else "."))
        elif q.improved is False and q.before and q.after and (
                q.before["rms"] > s.threshold or q.before["lost_pct"] >= s.lost_pct):
            f.append(f"The recovery at {r.start:%H:%M:%S} did not improve guiding: RMS {q.before['rms']:.2f} -> "
                     f"{q.after['rms']:.2f} {q.after['unit']}, lost frames {q.before['lost_pct']:.0f}% -> "
                     f"{q.after['lost_pct']:.0f}%.")
        if r.duration_s > 300:
            f.append(f"The recovery at {r.start:%H:%M:%S} took {_dur(r.duration_s)} (imaging was interrupted that long).")
    for when, msg in tl.halts:
        f.append(f"Safety stop at {when:%H:%M:%S}: {msg}")
    gave_up = sum(1 for w in tl.waits if w.outcome.startswith("gave up"))
    if tl.waits:
        f.append(f"{len(tl.waits)} wait(s) for a cloud-lost star: {sum(1 for w in tl.waits if w.outcome == 'star came back')} "
                 f"ended with the star back, {gave_up} ended in a recovery.")
    for what, start, end in tl.outages:
        dur = ((end or tl.last or start) - start).total_seconds()
        if dur >= 30:
            f.append(f"{what} was unreachable from {start:%H:%M:%S} for {_dur(dur)}"
                     + ("" if end else " (until the end of the log)") + " - the watchdog was blind during that time.")
    if tl.errors:
        f.append(f"{len(tl.errors)} unexpected error(s) were logged (first at {tl.errors[0][0]:%H:%M:%S}): "
                 f"{tl.errors[0][1][:120]}")
    if not tl.recoveries and not tl.halts:
        f.append("The watchdog took no recovery action in this log.")
    return rep


def format_session_report(rep: SessionReport, full_replay: bool = True) -> str:
    """The full report as plain text."""
    res, tl = rep.result, rep.timeline
    s = res.settings
    unit = s.unit
    out: list[str] = []
    add = out.append

    def head(title: str) -> None:
        add("")
        add(title)
        add("-" * len(title))

    add("GUIDE GUARDIAN - SESSION REPORT")
    add("=" * 27)
    add(f"PHD2 guide log : {rep.guide_log or '(not named)'}")
    if res.sessions:
        add(f"                 {len(res.sessions)} guiding sessions, {res.sessions[0].start:%Y-%m-%d %H:%M:%S} to "
            f"{res.sessions[-1].start + timedelta(seconds=res.sessions[-1].seconds):%Y-%m-%d %H:%M:%S}")
    if tl is not None:
        add(f"Debug log      : {len(tl.files)} file(s), {tl.first:%Y-%m-%d %H:%M:%S} to {tl.last:%Y-%m-%d %H:%M:%S}"
            if tl.first and tl.last else "Debug log      : (no usable lines)")
        for p in tl.files:
            add(f"                 {p}")
        if tl.app_info:
            env = tl.app_info.split(" | file ")[0].split(" debug log | ", 1)[-1]      # Python / Windows versions
            add(f"Environment    : {env}")
    else:
        add("Debug log      : not given - this is the rule replay only (what the watchdog would have done)")
    add(f"Rule replayed  : {describe_rule(s)}")
    add(f"                 (from {rep.rule_source or 'the replay window'}; hourly limit lifted, cooldown assumed after each)")
    for n in rep.rule_notes:
        add(f"                 note: {n}")

    # ---- summary
    head("SUMMARY")
    sessions = res.sessions
    guiding_s = sum(x.seconds for x in sessions)
    steps = sum(x.steps for x in sessions)
    drops = sum(x.drops for x in sessions)
    add(f"Guiding        : {len(sessions)} sessions, {_dur(guiding_s)}, {steps} steps, {drops} dropped frames "
        f"({100.0 * drops / max(1, steps + drops):.1f}%)")
    meds = [x.rms_median for x in sessions if x.evaluated_s]
    if meds:
        add(f"Rolling RMS    : session medians {min(meds):.2f} - {max(meds):.2f} {unit}; worst moment "
            f"{max(x.rms_max for x in sessions):.2f} {unit}")
    if tl is not None:
        armed_s = sum(a.armed_s for a in rep.sessions)
        covered_s = sum(a.covered_s for a in rep.sessions)
        auto = sum(1 for a in tl.arms if a.auto)
        add(f"Watchdog       : armed {_dur(armed_s)} of the {_dur(covered_s)} of guiding the debug log covers "
            f"({100.0 * armed_s / covered_s:.0f}%)" if covered_s else "Watchdog       : no guiding inside the debug log's span")
        add(f"                 {len(tl.arms)} arm period(s), {auto} by auto-arm, {len(tl.arms) - auto} by hand; "
            f"{len(tl.halts)} safety stop(s)")
        short = sum(a.short_breaches for a in rep.sessions)
        long_ = sum(len(a.breaches) for a in rep.sessions)
        add(f"Breaches       : {short} shorter than {SHORT_BREACH_S:.0f} s (cleared by themselves), {long_} longer")
        done = [r for r in tl.recoveries if r.outcome == "complete"]
        bad = [r for r in tl.recoveries if r.outcome != "complete"]
        kinds = {k: sum(1 for r in tl.recoveries if r.kind == k) for k in ("automatic", "retry", "manual")}
        add(f"Recoveries     : {len(tl.recoveries)} ({kinds['automatic']} automatic, {kinds['retry']} retries, "
            f"{kinds['manual']} manual) - {len(done)} complete, {len(bad)} not complete")
        if done:
            add(f"                 imaging interrupted {_dur(sum(r.duration_s for r in done))} in total, "
                f"{_dur(statistics.mean(r.duration_s for r in done))} on average")
        if tl.waits:
            back = sum(1 for w in tl.waits if w.outcome == "star came back")
            add(f"Star waits     : {len(tl.waits)} ({back} ended with the star back, "
                f"{sum(1 for w in tl.waits if w.outcome.startswith('gave up'))} ended in a recovery), "
                f"{_dur(sum(w.waited_s for w in tl.waits))} waiting")
        helped = [q for q in rep.qualities if q.improved is True]
        judged = [q for q in rep.qualities if q.improved is not None]
        if judged:
            add(f"Effect         : guiding improved after {len(helped)} of {len(judged)} recoveries "
                f"(RMS in the {QUALITY_AFTER_S / 60:.0f} min after vs the {QUALITY_BEFORE_S / 60:.0f} min before)")
        if tl.images:
            hfr = [im["hfr"] for im in tl.images if im.get("hfr")]
            if hfr:
                add(f"Images         : {len(tl.images)} logged, HFR median {statistics.median(hfr):.2f}, worst {max(hfr):.2f}")
    if tl is None:
        add(f"Rule check     : the replayed rule would start {res.recoveries} recovery(ies)")
    else:
        by_rule = sum(1 for r in tl.recoveries if r.kind == "automatic")
        add(f"Rule check     : the replayed rule would start {res.recoveries} recovery(ies); the watchdog started "
            f"{by_rule} on its own rule")
        if rep.edge_recoveries:
            add(f"                 {len(rep.edge_recoveries)} of the watchdog's recoveries began as guiding was stopped: the guide "
                f"log ends before the rule's hold time has passed,")
            add("                 so the replay cannot confirm them (the breach itself is visible in the log just before).")

    # ---- findings
    head("FINDINGS")
    if rep.findings:
        for line in rep.findings:
            add(f"* {line}")
    else:
        add("Nothing notable.")

    # ---- timeline
    head("TIMELINE")
    rows: list[tuple[datetime, str]] = []
    for sess in sessions:
        end = sess.start + timedelta(seconds=sess.seconds)
        rows.append((sess.start, f"PHD2 guiding session {sess.index} begins"))
        rows.append((end, f"PHD2 guiding session {sess.index} ends ({_dur(sess.seconds)}, {sess.steps} steps, "
                          f"{sess.drops} dropped, RMS median {sess.rms_median:.2f} {unit})"))
        rows += [(when, f"  replay: the rule would start a recovery - {why}") for when, why in sess.recoveries]
    if tl is not None:
        for a in tl.arms:
            rows.append((a.start, f"WATCHDOG ARMED ({'auto-arm' if a.auto else 'by hand'})"))
            if a.end:
                rows.append((a.end, f"WATCHDOG DISARMED ({a.ended_by})"))
        for br in tl.breaches:
            if br.outcome == "cleared by itself" and br.held_s < SHORT_BREACH_S:
                continue
            rows.append((br.start, f"breach: {br.reason} -> {br.outcome} (after {_dur(br.held_s)})"))
        for w in tl.waits:
            rows.append((w.start, f"star lost, cloud-like: waiting for it -> {w.outcome} ({_dur(w.waited_s)})"))
        for r in tl.recoveries:
            tail = f"{r.outcome}, {_dur(r.duration_s)}" if r.end else r.outcome
            rows.append((r.start, f"RECOVERY ({r.kind}) {r.reason} -> {tail}"))
        rows += [(when, f"SAFETY STOP: {msg}") for when, msg in tl.halts]
        rows += [(when, f"target: {name}") for when, name in tl.targets]
        rows += [(when, msg) for when, msg in tl.sequence]
        rows += [(when, msg) for when, msg in tl.setting_changes]
        for what, start, end in tl.outages:
            rows.append((start, f"{what} unreachable" + ("" if end else " (to the end of the log)")))
            if end:
                rows.append((end, f"{what} reachable again"))
        rows += [(when, f"ERROR: {msg[:150]}") for when, msg in tl.errors]
    for when, text in sorted(rows, key=lambda r: r[0]):
        add(f"{when:%m-%d %H:%M:%S}  {text}")

    # ---- per guiding session
    head("GUIDING SESSIONS")
    hdr = f"{'#':>2} {'start':<16}{'length':>8}{'steps':>7}{'drops':>7}{'RMS p50':>9}{'p95':>7}{'max':>7}"
    if tl is not None:
        hdr += f"{'armed':>7}  watchdog / replay"
    else:
        hdr += "  replay"
    add(hdr)
    for sess, an in zip(sessions, rep.sessions):
        line = (f"{sess.index:>2} {sess.start:%m-%d %H:%M:%S}{_dur(sess.seconds):>9}{sess.steps:>7}{sess.drops:>7}"
                f"{sess.rms_median:>9.2f}{sess.rms_p95:>7.2f}{sess.rms_max:>7.2f}")
        notes = []
        if tl is not None:
            if an.covered_s <= 0:
                line += f"{'n/a':>7}"
                notes.append("before the debug log" if sess.start < (tl.first or sess.start) else "after the debug log")
            else:
                line += f"{100.0 * an.armed_s / an.covered_s:>6.0f}%"
            if an.recoveries:
                notes.append(f"{len(an.recoveries)} recovery(ies) by the watchdog")
            if an.waits:
                notes.append(f"{len(an.waits)} star wait(s)")
            if an.breaches:
                notes.append(f"{len(an.breaches)} breach(es) >= {SHORT_BREACH_S:.0f}s")
            if an.short_breaches:
                notes.append(f"{an.short_breaches} short breach(es)")
            if an.unarmed_triggers:
                notes.append(f"NOT ARMED while the rule fires x{len(an.unarmed_triggers)}")
            if an.missed_triggers:
                notes.append(f"rule fires, watchdog silent x{len(an.missed_triggers)}")
        if sess.recoveries and not (tl is not None and an.recoveries):
            notes.append(f"replay: {len(sess.recoveries)} recovery(ies)")
        add(line + "  " + ("; ".join(notes) if notes else "-"))

    # ---- recoveries in detail
    if tl is not None and tl.recoveries:
        head("RECOVERIES")
        for n, q in enumerate(rep.qualities, 1):
            r = q.rec
            add(f"#{n}  {r.start:%Y-%m-%d %H:%M:%S}  {r.kind}  -> {r.outcome.upper()}"
                + (f" in {_dur(r.duration_s)}" if r.end else ""))
            add(f"    reason   : {r.reason}" + (f"  (attempt {r.attempt})" if r.attempt else ""))
            if r.target:
                add(f"    target   : {r.target}")
            if r.steps:
                names = {2: "interrupt exposure", 3: "stop guiding", 4: "slew / plate-solve", 5: "restart guiding",
                         6: "resume imaging", 7: "cooldown"}
                order = sorted(r.steps)
                parts = []
                for i, k in enumerate(order):
                    nxt = r.steps[order[i + 1]] if i + 1 < len(order) else (r.sequence_restarted_at or r.end)
                    d = f" ({_dur((nxt - r.steps[k]).total_seconds())})" if nxt else ""
                    parts.append(f"{names.get(k, 'step ' + str(k))}{d}")
                add("    steps    : " + " > ".join(parts))
            if r.center_offset_arcmin is not None:
                add(f"    centering: NINA reported a failed center, accepted at {r.center_offset_arcmin:.1f} arcmin from the target")
            if r.old_star or r.new_star:
                same = "" if r.star_changed is None else (" - a different star" if r.star_changed else " - the same star")
                fmt = lambda p: f"({p[0]:.0f}, {p[1]:.0f})" if p else "?"
                add(f"    guide star: {fmt(r.old_star)} -> {fmt(r.new_star)}{same}")
            if q.before:
                add(f"    before   : RMS {q.before['rms']:.2f} {q.before['unit']}, {q.before['lost_pct']:.0f}% frames lost "
                    f"({QUALITY_BEFORE_S / 60:.0f} min before)")
            if q.after:
                add(f"    after    : RMS {q.after['rms']:.2f} {q.after['unit']}, {q.after['lost_pct']:.0f}% frames lost "
                    f"({QUALITY_AFTER_S / 60:.0f} min after guiding resumed)"
                    + {True: "  [improved]", False: "  [not improved]", None: ""}[q.improved])
            elif r.outcome == "complete":
                add("    after    : no guide-log frames in the window after guiding resumed")
            if q.hfr_before or q.hfr_after:
                fmt_h = lambda v: f"{v:.2f}" if v else "n/a"
                add(f"    HFR      : {fmt_h(q.hfr_before)} before -> {fmt_h(q.hfr_after)} after (median of logged images)")
            if r.restored is not None:
                add(f"    restore  : {'session restored' if r.restored else 'NOT fully restored'}")
            if r.retry_info:
                add(f"    retry    : {r.retry_info}")
            if r.error:
                add(f"    error    : {r.error}")
            for note in r.notes[:8]:
                add(f"    note     : {note}")
            add("")

    # ---- star waits / long breaches
    if tl is not None and (tl.waits or any(a.breaches for a in rep.sessions)):
        head("STAR WAITS AND BREACHES")
        for w in tl.waits:
            add(f"{w.start:%m-%d %H:%M:%S}  waited {_dur(w.waited_s)}: {w.outcome}")
            add(f"    {w.detail[:200]}")
        for a in rep.sessions:
            for br in a.breaches:
                add(f"{br.start:%m-%d %H:%M:%S}  breach {_dur(br.held_s)} (session {a.index}): {br.reason} -> {br.outcome}")

    # ---- the plain replay
    head("RULE REPLAY (what the rule above would do on this PHD2 log)")
    add(format_replay(res, full=full_replay))
    return "\n".join(out)


def _file_span(path: str, kind: str) -> tuple[datetime, datetime] | None:
    """(start, end) of a guide log or a debug log, from its file name and its last-write time."""
    name = os.path.basename(path)
    pat = (r"PHD2_GuideLog_(\d{4}-\d\d-\d\d)_(\d{6})" if kind == "guide"
           else r"(?:guideguardian|phd2guard)_debug_(\d{8})_(\d{6})")      # 1.0.0 named its debug logs phd2guard_*
    m = re.match(pat, name)
    if not m:
        return None
    try:
        start = datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H%M%S" if kind == "guide" else "%Y%m%d%H%M%S")
        end = datetime.fromtimestamp(os.path.getmtime(path))
    except (ValueError, OSError):
        return None
    return (start, end) if end >= start else None


def guess_counterpart(path: str, want: str) -> str:
    """Find the other half of a guide-log / debug-log pair: the file of kind `want` ("guide" or "debug") in the same
    folder, its parent or Documents\\PHD2 whose time span overlaps the given file's the most. "" when there is none."""
    have = "debug" if want == "guide" else "guide"
    base = re.sub(r"\.\d+$", "", path) if re.search(r"\.log\.\d+$", path, re.I) else path
    mine = _file_span(base, have)
    if mine is None:
        return ""
    folder = os.path.dirname(os.path.abspath(path))
    patterns = ("PHD2_GuideLog*.txt",) if want == "guide" else ("guideguardian_debug_*.log", "phd2guard_debug_*.log")
    found: dict[str, float] = {}
    for d in dict.fromkeys([folder, os.path.dirname(folder), default_guide_log_dir()]):
        for cand in (c for pattern in patterns for c in glob.glob(os.path.join(glob.escape(d), pattern))):
            span = _file_span(cand, want)
            if span is None:
                continue
            overlap = (min(span[1], mine[1]) - max(span[0], mine[0])).total_seconds()
            if overlap > 60:
                found[cand] = max(found.get(cand, 0.0), overlap)
    return max(found, key=found.get) if found else ""


def run_session_report(guide_log: str, s: Settings, debug_log: str = "", use_logged_rule: bool = True,
                       poll: float = 1.0, progress=None, cancel: threading.Event | None = None,
                       overrides: dict | None = None) -> SessionReport:
    """The whole job: read the PHD2 guide log (and the watchdog debug log if given), replay the rule through the real
    trigger code and line everything up. `progress(fraction, text)`; `cancel` aborts with Canceled."""

    def step(lo: float, hi: float, text: str):
        if progress is None:
            return None
        return lambda frac, *_: progress(lo + (hi - lo) * frac, text)

    if progress is not None:
        progress(0.0, "Reading the guide log...")
    segs = parse_guide_log(guide_log, cancel)
    if not segs:
        raise ValueError("No guiding sessions were found in this file. Is it a PHD2 guide log "
                         "(PHD2_GuideLog_<date>_<time>.txt)?")
    tl, rule_source, notes = None, "the replay window", []
    if debug_log:
        files = debug_log_files(debug_log)
        if not files:
            raise OSError(f"Cannot find the debug log: {debug_log}")
        events = parse_debug_log(files, step(0.05, 0.4, "Reading the debug log..."), cancel)
        if not events:
            raise ValueError("No usable lines were found in the debug log. Is it a Guide Guardian verbose debug log "
                             "(guideguardian_debug_<date>_<time>.log)?")
        tl = build_timeline(events, files)
        if use_logged_rule and tl.configs:
            s, rule_source, notes = logged_rule(tl)
            if overrides:
                s = replace(s, **overrides)
                rule_source += " with " + ", ".join(f"{k}={v}" for k, v in overrides.items()) + " given on the command line"
        else:
            rule_source = "the settings in the replay window"
    result = run_replay(segs, s, poll, step(0.4, 0.95, "Replaying the rule..."), cancel)
    if progress is not None:
        progress(0.97, "Building the report...")
    return analyze_session(result, segs, tl, guide_log, rule_source, notes)


# --------------------------------------------------------------------------- web interface glue
def lan_urls(port: int) -> list[str]:
    """Addresses this PC answers on (for showing the user where to point a browser)."""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))                  # no packet is sent; selects the interface with a route
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return [f"http://{ip}:{port}/" for ip in ips if ip and ip != "0.0.0.0"]


class WebBridge(QObject):
    """Keeps the web interface's snapshot current from the worker's signals; exposes the three remote commands."""
    STATUS_KEYS = ("state", "ra", "dec", "total", "recent", "unit", "settle", "phd2_settling", "exposing", "target",
                   "activity", "exposure_s", "seq_running", "samples", "window", "lost", "guider_connected", "dithering")

    def __init__(self, worker: "GuardWorker", parent=None):
        super().__init__(parent)
        self.worker = worker
        self.state = web_ui.WebState()
        worker.log.connect(self._on_log)
        worker.status.connect(self._on_status)
        worker.phase.connect(self._on_phase)
        worker.connection.connect(self._on_nina)
        worker.phd2_connection.connect(self._on_phd2)
        worker.armed_changed.connect(self._on_armed)
        worker.attempts_changed.connect(self._on_attempts)
        worker.reference_changed.connect(self._on_reference)
        worker.alert.connect(self._on_alert)

    def _on_log(self, ts: str, level: str, msg: str) -> None:
        if level != "API":                                # call-by-call noise stays in the desktop log / debug log
            self.state.add_log(ts, level, msg)

    def _on_status(self, d: dict) -> None:
        self.state.update(status={k: d.get(k) for k in self.STATUS_KEYS}, nina_connected=bool(d.get("connected")))

    def _on_phase(self, kind: str, text: str) -> None:
        self.state.update(phase={"kind": kind, "text": text})

    def _on_nina(self, ok: bool, detail: str) -> None:
        self.state.update(nina_connected=ok)

    def _on_phd2(self, ok: bool, detail: str) -> None:
        self.state.update(phd2_connected=ok, phd2_detail=detail)

    def _on_armed(self, armed: bool) -> None:
        self.state.update(armed=armed, **({"alert": ""} if armed else {}))

    def _on_attempts(self, n: int) -> None:
        self.state.update(attempts=n)

    def _on_reference(self, text: str) -> None:
        self.state.update(reference=text)

    def _on_alert(self, msg: str) -> None:
        self.state.update(alert=msg)

    def set_settings(self, s: Settings) -> None:          # read-only view of the rule for the page
        self.state.update(settings={k: getattr(s, k) for k in (
            "threshold", "unit", "sustain_s", "recent_steps", "lost_pct_enabled", "lost_pct", "lost_window",
            "wait_for_star_s", "cooldown_s", "max_attempts_per_hour", "rms_source", "reselect_star")})
        if s.rms_source != "phd2":
            self.state.update(phd2_connected=None)

    def commands(self) -> dict:
        return {"arm": lambda: self.worker.command("arm"), "disarm": lambda: self.worker.command("disarm"),
                "manual": lambda: self.worker.command("manual")}


class WebController:
    """Starts / stops / re-targets the web server to match the settings."""

    def __init__(self, bridge: WebBridge, host: str = "0.0.0.0"):
        self.bridge, self.host = bridge, host
        self.server: web_ui.WebServer | None = None
        self.urls: list[str] = []
        self.status = "Web interface off"
        self._key: tuple | None = None

    def apply(self, s: Settings) -> str:
        self.bridge.set_settings(s)
        want = s.web_enabled and bool(s.web_token) and 1 <= s.web_port <= 65535
        key = (s.web_port, s.web_token, s.web_view_needs_token, self.host)
        if self.server is not None and (not want or key != self._key):
            self.stop()
        if want and self.server is None:
            srv = web_ui.WebServer(self.bridge.state, self.bridge.commands(), self.host, s.web_port, s.web_token,
                                   s.web_view_needs_token, APP_NAME, tagline=APP_TAGLINE, version=APP_VERSION,
                                   icons=web_icon_files())
            try:
                srv.start()
            except OSError as exc:
                self._key = None
                self.status = f"Cannot listen on port {s.web_port}: {exc}"
                return self.status
            self.server, self._key = srv, key
            self.urls = lan_urls(s.web_port) if self.host == "0.0.0.0" else [f"http://{self.host}:{s.web_port}/"]
            self.status = "Listening on " + (", ".join(self.urls) if self.urls else f"port {s.web_port}")
        elif not want:
            self.status = "Web interface off" if not s.web_enabled else "Generate an access token to start it"
        return self.status

    def stop(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server, self.urls = None, []


# --------------------------------------------------------------------------- icon
ICON_BG, ICON_TEAL, ICON_STAR = "#0f1b33", "#2ec4b6", "#ffd166"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_icon(size: int) -> QImage:
    """The app icon: a guide star in a teal reticle ring on a navy tile, with a 'recenter' arrow round the ring.

    Drawn in a 100-unit design space. Three variants keep it legible: full (>= 64 px: arrow + tick marks),
    mid (40-63 px: arrow only) and small (< 40 px: plain ring and a bolder star).
    """
    ss = 4 if size <= 64 else 1                           # supersample small sizes for clean edges
    px = size * ss
    img = QImage(px, px, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(px / 100.0, px / 100.0)
    tile = QPainterPath()
    tile.addRoundedRect(QRectF(0, 0, 100, 100), 20, 20)
    p.fillPath(tile, QColor(ICON_BG))
    teal = QColor(ICON_TEAL)

    def star(offset: float, scale: float) -> None:
        s = QPainterPath()
        s.moveTo(50, 14)
        s.quadTo(54, 46, 86, 50)
        s.quadTo(54, 54, 50, 86)
        s.quadTo(46, 54, 14, 50)
        s.quadTo(46, 46, 50, 14)
        s.closeSubpath()
        p.fillPath(QTransform().translate(offset, offset).scale(scale, scale).map(s), QColor(ICON_STAR))

    p.setBrush(Qt.BrushStyle.NoBrush)
    if size >= 40:
        full = size >= 64
        pen = QPen(teal, 5 if full else 6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        arc = QPainterPath()                               # clockwise from 12 o'clock round to the upper left
        arc.moveTo(50, 16)
        arc.arcTo(QRectF(16, 16, 68, 68), 90, -315)
        p.drawPath(arc)
        head = QPainterPath()
        head.moveTo(31, 21)
        head.lineTo(29.5, 29.5)
        head.lineTo(22.5, 22.5)
        head.closeSubpath()
        p.setBrush(teal)
        pen.setWidthF(2)
        p.setPen(pen)
        p.drawPath(head)
        if full:
            pen.setWidthF(4)
            p.setPen(pen)
            for a, b in (((50, 3), (50, 10)), ((50, 90), (50, 97)), ((3, 50), (10, 50)), ((90, 50), (97, 50))):
                p.drawLine(QPointF(*a), QPointF(*b))
            star(19, 0.62)
        else:
            star(17, 0.66)
    else:
        pen = QPen(teal, {16: 10, 24: 9}.get(size, 8))
        p.setPen(pen)
        p.drawEllipse(QPointF(50, 50), 36, 36)
        star({16: 10}.get(size, 12), {16: 0.80, 24: 0.78}.get(size, 0.76))
    p.end()
    if ss == 1:
        return img
    return img.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)


def app_icon() -> QIcon:
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(QPixmap.fromImage(render_icon(size)))
    return icon


_WEB_ICONS: dict | None = None


def web_icon_files() -> dict:
    """The app icon as files for the web page's favicon: "ico" (16/32/48 px, PNG entries) and "apple" (180 px PNG).
    Best effort and drawn once: an empty dict just means the page falls back to its SVG favicon."""
    global _WEB_ICONS
    if _WEB_ICONS is None:
        def png(img: QImage) -> bytes:
            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "PNG")
            return bytes(buf.data())

        try:
            sizes = (16, 32, 48)
            parts = [(s, png(render_icon(s))) for s in sizes]
            out = struct.pack("<HHH", 0, 1, len(parts))
            offset = 6 + 16 * len(parts)
            body = b""
            for s, data in parts:
                out += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(data), offset + len(body))
                body += data
            _WEB_ICONS = {"ico": out + body, "apple": png(render_icon(180))}
        except Exception:                                  # noqa: BLE001 - cosmetic only
            _WEB_ICONS = {}
    return _WEB_ICONS


# --------------------------------------------------------------------------- GUI
class _WheelOnlyWhenFocused:
    """Mixin: ignore the mouse wheel unless the widget has keyboard focus, so scrolling the settings panel
    never changes a value by accident (the event then propagates to the scroll area)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class SafeSpinBox(_WheelOnlyWhenFocused, QSpinBox):
    pass


class SafeDoubleSpinBox(_WheelOnlyWhenFocused, QDoubleSpinBox):
    pass


class SafeComboBox(_WheelOnlyWhenFocused, QComboBox):
    pass


def session_detail_report(rep: SessionReport, i: int) -> str:
    """One guiding session: the replay details plus, with a debug log, what the watchdog was doing."""
    sess, an, tl = rep.result.sessions[i], rep.sessions[i], rep.timeline
    text = session_detail(sess, rep.result.settings.unit)
    if tl is None:
        return text
    lines = [text, ""]
    if an.covered_s <= 0:
        lines.append("Watchdog: this session is outside the time span of the debug log - nothing is known about it.")
        return "\n".join(lines)
    lines.append(f"Watchdog: armed for {_dur(an.armed_s)} of the {_dur(an.covered_s)} covered by the debug log "
                 f"({100.0 * an.armed_s / an.covered_s:.0f}%).")
    for r in an.recoveries:
        q = next((x for x in rep.qualities if x.rec is r), None)
        line = f"  {r.start:%H:%M:%S}  RECOVERY ({r.kind}) -> {r.outcome}" + (f" in {_dur(r.duration_s)}" if r.end else "")
        lines.append(line)
        lines.append(f"            reason: {r.reason}")
        if r.old_star or r.new_star:
            fmt = lambda p: f"({p[0]:.0f}, {p[1]:.0f})" if p else "?"
            lines.append(f"            guide star {fmt(r.old_star)} -> {fmt(r.new_star)}")
        if q and q.before:
            lines.append(f"            RMS before {q.before['rms']:.2f} {q.before['unit']}"
                         + (f", after {q.after['rms']:.2f} {q.after['unit']}" if q.after else ""))
        if r.error:
            lines.append(f"            error: {r.error}")
    for br in an.breaches:
        lines.append(f"  {br.start:%H:%M:%S}  breach {_dur(br.held_s)}: {br.reason} -> {br.outcome}")
    if an.short_breaches:
        lines.append(f"  {an.short_breaches} short breach(es) (< {SHORT_BREACH_S:.0f} s) cleared by themselves")
    for w in an.waits:
        lines.append(f"  {w.start:%H:%M:%S}  waited {_dur(w.waited_s)} for the star: {w.outcome}")
    for when, why in an.unarmed_triggers:
        lines.append(f"  {when:%H:%M:%S}  the rule fires ({why}) but the watchdog was NOT ARMED")
    for when, why in an.missed_triggers:
        lines.append(f"  {when:%H:%M:%S}  the replayed rule fires ({why}) but the watchdog did nothing")
    if not (an.recoveries or an.breaches or an.waits or an.short_breaches):
        lines.append("  no breaches, waits or recoveries")
    return "\n".join(lines)


class ReplayWorker(QThread):
    """Parses the logs and builds the report off the GUI thread."""
    progress = pyqtSignal(int, str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, guide_log: str, settings: Settings, debug_log: str = "", use_logged_rule: bool = True,
                 poll: float = 1.0, parent=None):
        super().__init__(parent)
        self.guide_log, self.settings, self.debug_log = guide_log, settings, debug_log
        self.use_logged_rule, self.poll = use_logged_rule, poll
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            self.done.emit(run_session_report(
                self.guide_log, self.settings, self.debug_log, self.use_logged_rule, self.poll,
                lambda frac, text: self.progress.emit(int(frac * 100), text), self._cancel))
        except Canceled:
            self.failed.emit("Replay canceled.")
        except (OSError, ValueError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:                           # noqa: BLE001 - shown to the user, details in the log file
            logging.getLogger("guideguardian").error("Replay failed: %s", traceback.format_exc())
            self.failed.emit(f"Replay failed: {exc}")


class ReplayDialog(QDialog):
    """Runs a PHD2 guide log through the real trigger code, offline, and shows when (and why) a recovery would have
    started. Given the watchdog's debug log as well, it also reports what the watchdog actually did - when it was armed,
    each breach, wait and recovery, and how guiding looked before and after. Nothing is sent to NINA or PHD2."""

    COLUMNS = ("#", "Start", "Length", "Steps", "Dropped", "RMS med", "RMS 95%", "RMS max", "Rule fires", "Star waits")
    WATCHDOG_COLUMNS = ("Armed", "Watchdog did")

    def __init__(self, live_settings, qs: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} - replay a PHD2 guide log / session report")
        self.resize(1100, 860)
        self._live_settings, self._qs = live_settings, qs
        self._worker: ReplayWorker | None = None
        self._report: SessionReport | None = None

        self.ed_path = QLineEdit()
        self.ed_path.setPlaceholderText("a PHD2 guide log, e.g. PHD2_GuideLog_2026-10-08_134943.txt")
        btn_browse = QPushButton("Browse..."); btn_browse.clicked.connect(self._browse_guide)
        self.ed_debug = QLineEdit()
        self.ed_debug.setPlaceholderText("optional: the watchdog's debug log from the same night, e.g. guideguardian_debug_20261008_212347.log")
        btn_dbrowse = QPushButton("Browse..."); btn_dbrowse.clicked.connect(self._browse_debug)
        btn_dclear = QPushButton("Clear"); btn_dclear.clicked.connect(lambda: self.ed_debug.clear())
        self.chk_logged = QCheckBox("Replay the rule that was in force (read from the debug log)")
        self.chk_logged.setChecked(True)
        self.chk_logged.setToolTip("The settings the watchdog actually ran with come from the debug log's CONFIG line, so "
                                   "the replay can be compared with what really happened. Untick it to test the rule "
                                   "set on the right instead.")
        src = QGroupBox("Logs")
        sl = QGridLayout(src)
        sl.addWidget(QLabel("PHD2 guide log"), 0, 0); sl.addWidget(self.ed_path, 0, 1); sl.addWidget(btn_browse, 0, 2)
        sl.addWidget(QLabel("Watchdog debug log"), 1, 0); sl.addWidget(self.ed_debug, 1, 1)
        row = QHBoxLayout(); row.addWidget(btn_dbrowse); row.addWidget(btn_dclear)
        sl.addLayout(row, 1, 2)
        sl.addWidget(self.chk_logged, 2, 1, 1, 2)
        hint = QLabel("PHD2 writes one guide log per night, by default in Documents\\PHD2. Add the debug log of the same night "
                      "(Debug log group on the Settings tab) for a full session report: when the watchdog was armed, every "
                      "breach, star wait and recovery, and how guiding looked before and after. Rotated parts "
                      "(.log.1, .log.2 ...) are included automatically, and the matching file is looked for next to the "
                      "one you choose.")
        hint.setWordWrap(True); hint.setStyleSheet("color:#8a8a8a")
        sl.addWidget(hint, 3, 0, 1, 3)
        sl.setColumnStretch(1, 1)
        sl.setRowStretch(4, 1)                                # spare height goes below the hint, not above it
        self.ed_path.editingFinished.connect(lambda: self._autofill("guide"))
        self.ed_debug.editingFinished.connect(lambda: self._autofill("debug"))
        self.ed_debug.textChanged.connect(lambda t: self.chk_logged.setEnabled(bool(t.strip())))

        self.sp_thr = SafeDoubleSpinBox(); self.sp_thr.setRange(0.05, 50); self.sp_thr.setDecimals(2); self.sp_thr.setSingleStep(0.1)
        self.cb_unit = SafeComboBox(); self.cb_unit.addItems(["arcsec", "pixel"])
        self.sp_sustain = SafeSpinBox(); self.sp_sustain.setRange(1, 600); self.sp_sustain.setSuffix(" s")
        self.sp_win = SafeSpinBox(); self.sp_win.setRange(PHD2_MIN_SAMPLES, 500); self.sp_win.setSuffix(" steps")
        self.sp_recent = SafeSpinBox(); self.sp_recent.setRange(0, 40); self.sp_recent.setSuffix(" steps")
        self.sp_recent.setSpecialValueText("off")
        self.chk_lost = QCheckBox("Trigger on star-loss rate")
        self.sp_lostpct = SafeSpinBox(); self.sp_lostpct.setRange(5, 100); self.sp_lostpct.setSuffix(" %")
        self.sp_lostwin = SafeSpinBox(); self.sp_lostwin.setRange(PHD2_MIN_SAMPLES, 200); self.sp_lostwin.setSuffix(" frames")
        self.sp_wait = SafeSpinBox(); self.sp_wait.setRange(0, 3600); self.sp_wait.setSingleStep(60)
        self.sp_wait.setSuffix(" s"); self.sp_wait.setSpecialValueText("off")
        self.sp_cool = SafeSpinBox(); self.sp_cool.setRange(0, 7200); self.sp_cool.setSuffix(" s")
        btn_live = QPushButton("Use current settings"); btn_live.clicked.connect(lambda: self._fill(self._live_settings()))
        btn_def = QPushButton("App defaults"); btn_def.clicked.connect(lambda: self._fill(Settings()))
        rule = QGroupBox("Trigger rule to test")
        rl = QFormLayout(rule)
        row = QHBoxLayout(); row.addWidget(self.sp_thr); row.addWidget(self.cb_unit)
        rl.addRow("Critical Total RMS", row)
        rl.addRow("Guiding bad for", self.sp_sustain)
        rl.addRow("Rolling RMS window", self.sp_win)
        rl.addRow("Last steps must agree", self.sp_recent)
        rl.addRow(self.chk_lost)
        rl.addRow("   Frames lost at least", self.sp_lostpct)
        rl.addRow("   Over the last", self.sp_lostwin)
        rl.addRow("Wait for star (clouds)", self.sp_wait)
        rl.addRow("Cooldown after recovery", self.sp_cool)
        rb = QHBoxLayout(); rb.addWidget(btn_live); rb.addWidget(btn_def); rb.addStretch(1)
        rl.addRow(rb)
        self.chk_lost.toggled.connect(lambda on: (self.sp_lostpct.setEnabled(on), self.sp_lostwin.setEnabled(on)))

        self.btn_run = QPushButton("Run replay / build report"); self.btn_run.setMinimumHeight(34)
        self.btn_run.clicked.connect(self._run)
        self.btn_cancel = QPushButton("Cancel"); self.btn_cancel.setMinimumHeight(34); self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        self.bar = QProgressBar(); self.bar.setRange(0, 100); self.bar.setTextVisible(True)
        run_row = QHBoxLayout()
        run_row.addWidget(self.btn_run); run_row.addWidget(self.btn_cancel); run_row.addWidget(self.bar, 1)

        self.lbl_summary = QLabel("Choose a guide log (and, for a full session report, the debug log) and run.")
        self.lbl_summary.setWordWrap(True)
        bold = QFont(); bold.setBold(True); self.lbl_summary.setFont(bold)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._show_detail)
        mono = "background:#1e1e1e;color:#d4d4d4;font-family:Consolas,monospace;font-size:9pt"
        self.detail = QTextEdit(); self.detail.setReadOnly(True); self.detail.setStyleSheet(mono)
        self.detail.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.report_view = QTextEdit(); self.report_view.setReadOnly(True); self.report_view.setStyleSheet(mono)
        self.report_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.detail, "Selected session")
        self.tabs.addTab(self.report_view, "Full session report")
        self.btn_copy = QPushButton("Copy report"); self.btn_copy.clicked.connect(self._copy)
        self.btn_save = QPushButton("Save report...")
        self.btn_save.clicked.connect(self._save)
        btn_close = QPushButton("Close"); btn_close.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addWidget(self.btn_copy); bottom.addWidget(self.btn_save); bottom.addStretch(1); bottom.addWidget(btn_close)

        top = QHBoxLayout()
        top.addWidget(src, 1); top.addWidget(rule, 0)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(run_row)
        lay.addWidget(self.lbl_summary)
        lay.addWidget(self.table, 2)
        lay.addWidget(self.tabs, 3)
        lay.addLayout(bottom)

        self._inputs = [self.ed_path, btn_browse, self.ed_debug, btn_dbrowse, btn_dclear, self.chk_logged, self.sp_thr,
                        self.cb_unit, self.sp_sustain, self.sp_win, self.sp_recent, self.chk_lost, self.sp_lostpct,
                        self.sp_lostwin, self.sp_wait, self.sp_cool, btn_live, btn_def]
        self._fill(self._live_settings())
        self.chk_logged.setEnabled(False)
        self._set_result(None)

    # ----- form -----------------------------------------------------------------------
    def _fill(self, s: Settings) -> None:
        self.sp_thr.setValue(s.threshold); self.cb_unit.setCurrentText(s.unit)
        self.sp_sustain.setValue(s.sustain_s); self.sp_win.setValue(s.phd2_window); self.sp_recent.setValue(s.recent_steps)
        self.chk_lost.setChecked(s.lost_pct_enabled)
        self.sp_lostpct.setValue(s.lost_pct); self.sp_lostwin.setValue(s.lost_window)
        self.sp_lostpct.setEnabled(s.lost_pct_enabled); self.sp_lostwin.setEnabled(s.lost_pct_enabled)
        self.sp_wait.setValue(s.wait_for_star_s); self.sp_cool.setValue(s.cooldown_s)

    def settings(self) -> Settings:
        return replay_settings(
            threshold=self.sp_thr.value(), unit=self.cb_unit.currentText(), sustain_s=self.sp_sustain.value(),
            phd2_window=self.sp_win.value(), recent_steps=self.sp_recent.value(),
            lost_pct_enabled=self.chk_lost.isChecked(), lost_pct=self.sp_lostpct.value(),
            lost_window=self.sp_lostwin.value(), wait_for_star_s=self.sp_wait.value(), cooldown_s=self.sp_cool.value())

    def _start_dir(self, field: QLineEdit) -> str:
        for text in (field.text().strip(), self.ed_path.text().strip(), self.ed_debug.text().strip()):
            d = os.path.dirname(text) if text else ""
            if d and os.path.isdir(d):
                return d
        d = str(self._qs.value("replay_dir", "") or "")
        return d if d and os.path.isdir(d) else default_guide_log_dir()

    def _browse_guide(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "PHD2 guide log", self._start_dir(self.ed_path),
                                              "PHD2 guide logs (PHD2_GuideLog*.txt);;Text files (*.txt);;All files (*)")
        if path:
            self.ed_path.setText(path)
            self._qs.setValue("replay_dir", os.path.dirname(path))
            self._autofill("guide")

    def _browse_debug(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Watchdog debug log", self._start_dir(self.ed_debug),
                                              "Debug logs (guideguardian_debug_*.log* phd2guard_debug_*.log*);;Log files (*.log *.log.*);;All files (*)")
        if path:
            self.ed_debug.setText(path)
            self._qs.setValue("replay_dir", os.path.dirname(path))
            self._autofill("debug")

    def _autofill(self, changed: str) -> None:
        """Having chosen one log, offer the other half of the pair when it can be found and the field is empty."""
        if changed == "guide":
            src, dst, want = self.ed_path.text().strip().strip('"'), self.ed_debug, "debug"
        else:
            src, dst, want = self.ed_debug.text().strip().strip('"'), self.ed_path, "guide"
        if not src or dst.text().strip() or not os.path.isfile(src):
            return
        found = guess_counterpart(src, want)
        if found:
            dst.setText(found)
            self.lbl_summary.setText(f"Found the matching {'debug log' if want == 'debug' else 'guide log'}: "
                                     f"{os.path.basename(found)}")

    # ----- running --------------------------------------------------------------------
    def _busy(self, busy: bool) -> None:
        for w in self._inputs:
            w.setEnabled(not busy)
        if not busy:
            self.chk_logged.setEnabled(bool(self.ed_debug.text().strip()))
            self.sp_lostpct.setEnabled(self.chk_lost.isChecked()); self.sp_lostwin.setEnabled(self.chk_lost.isChecked())
        self.btn_run.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)

    def _run(self) -> None:
        path = self.ed_path.text().strip().strip('"')
        debug = self.ed_debug.text().strip().strip('"')
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, APP_NAME, "Choose an existing PHD2 guide log file first.")
            return
        if debug and not debug_log_files(debug):
            QMessageBox.warning(self, APP_NAME, "The debug log file was not found. Clear the field to replay the "
                                                "guide log on its own.")
            return
        self._set_result(None)
        self._busy(True)
        self.bar.setValue(0)
        self.lbl_summary.setText("Working...")
        self._worker = ReplayWorker(path, self.settings(), debug, self.chk_logged.isChecked(), 1.0, self)
        self._worker.progress.connect(lambda pct, text: (self.bar.setValue(pct), self.lbl_summary.setText(text)))
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def _finish_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.wait(3000)
            worker.deleteLater()
        self._busy(False)

    def _on_done(self, report: SessionReport) -> None:
        self._finish_worker()
        self.bar.setValue(100)
        self._set_result(report)

    def _on_failed(self, message: str) -> None:
        self._finish_worker()
        self.bar.setValue(0)
        self.lbl_summary.setText(message)

    def shutdown(self) -> None:
        """Stop a running replay (the window is closing)."""
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(5000)

    def closeEvent(self, event) -> None:
        self.shutdown()
        event.accept()

    # ----- results --------------------------------------------------------------------
    def _set_result(self, report: SessionReport | None) -> None:
        self._report = report
        self.table.setRowCount(0)
        self.detail.clear()
        self.report_view.clear()
        self.btn_copy.setEnabled(report is not None)
        self.btn_save.setEnabled(report is not None)
        if report is None:
            self.table.setColumnCount(len(self.COLUMNS))
            self.table.setHorizontalHeaderLabels(self.COLUMNS)
            return
        result, tl = report.result, report.timeline
        unit = result.settings.unit
        cols = list(self.COLUMNS) + (list(self.WATCHDOG_COLUMNS) if tl is not None else [])
        self.table.setColumnCount(len(cols))
        sym = "″" if unit == "arcsec" else "px"
        self.table.setHorizontalHeaderLabels([c if not c.startswith("RMS") else f"{c} ({sym})" for c in cols])
        self.table.horizontalHeaderItem(8).setToolTip("Recoveries the replayed rule would start in this session")
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        dark = QBrush(QColor("#000000"))
        for sess, an in zip(result.sessions, report.sessions):
            cells = [str(sess.index), f"{sess.start:%m-%d %H:%M:%S}",
                     f"{int(sess.seconds // 3600)}:{int(sess.seconds % 3600 // 60):02d}",
                     str(sess.steps), str(sess.drops), f"{sess.rms_median:.2f}", f"{sess.rms_p95:.2f}", f"{sess.rms_max:.2f}",
                     str(len(sess.recoveries)), str(len(sess.waits))]
            color = None
            if tl is not None:
                did = []
                if an.recoveries:
                    k = len(an.recoveries)
                    did.append("1 recovery" if k == 1 else f"{k} recoveries")
                if an.waits:
                    did.append(f"{len(an.waits)} wait(s)")
                if an.unarmed_triggers:
                    did.append("NOT ARMED while the rule fires")
                if an.missed_triggers:
                    did.append("rule fires, no action")
                cells.append("n/a" if an.covered_s <= 0 else f"{100.0 * an.armed_s / an.covered_s:.0f}%")
                cells.append(", ".join(did) if did else ("-" if an.covered_s > 0 else "before/after the debug log"))
                if an.unarmed_triggers or an.missed_triggers:
                    color = "#ffc9c9"
                elif an.recoveries:
                    color = "#cfe9d0"
            if color is None:
                color = "#ffe2a8" if sess.recoveries else "#e2eefc" if sess.waits else None
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                elif tl is not None and c == len(cols) - 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                else:
                    item.setTextAlignment(right)
                if color:
                    item.setBackground(QBrush(QColor(color))); item.setForeground(dark)
                self.table.setItem(r, c, item)
        n, k = len(result.sessions), result.recoveries
        verdict = ("no recovery would have been triggered by the replayed rule" if k == 0
                   else f"the replayed rule would start {k} recover{'y' if k == 1 else 'ies'}")
        text = f"{n} guiding session{'s' if n != 1 else ''}: {verdict}"
        if tl is not None:
            text += (f"; the watchdog actually ran {len(tl.recoveries)} recover{'y' if len(tl.recoveries) == 1 else 'ies'}"
                     f" and {len(tl.waits)} star wait{'' if len(tl.waits) == 1 else 's'}"
                     + (f", {len(tl.halts)} safety stop(s)" if tl.halts else ""))
        self.lbl_summary.setText(text + ".")
        self.report_view.setPlainText(format_session_report(report))
        self.tabs.setCurrentIndex(0)
        self.detail.setPlainText("Select a session in the table for its details. The full report (summary, findings, "
                                 "timeline, recoveries) is on the next tab.")

    def _show_detail(self) -> None:
        if self._report is None:
            return
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self.tabs.setCurrentIndex(0)
            self.detail.setPlainText(session_detail_report(self._report, rows[0].row()))

    def _report_text(self) -> str:
        return format_session_report(self._report) if self._report else ""

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._report_text())

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save report", "session_report.txt", "Text files (*.txt)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(self._report_text() + "\n")
            except OSError as exc:
                QMessageBox.warning(self, APP_NAME, f"Cannot save the report: {exc}")


LOG_COLORS = {"DEBUG": "#888888", "INFO": "#d4d4d4", "API": "#6fb3d9", "OK": "#6ec06e",
              "WARN": "#e6a23c", "ERROR": "#f06060"}
PHASE_COLORS = {"idle": "#8a8a8a", "monitoring": "#2e9e44", "pending": "#d98a00",
                "recovering": "#2f7fd0", "cooldown": "#8a55c8", "waiting": "#c9a400", "retry": "#b05a00",
                "halted": "#d03030"}


class MainWindow(QMainWindow):
    def __init__(self, settings_file: str | None = None):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} - {APP_TAGLINE}")
        self.resize(1100, 780)
        self._armed = False
        self._phase_kind = "idle"
        self._connected = False
        self._replay_dlg: ReplayDialog | None = None
        self._qs = open_qsettings(settings_file)
        self._build_ui()
        self._load_settings()
        self.worker = GuardWorker(self.collect_settings(), self)
        self.web_bridge = WebBridge(self.worker, self)
        self.web_ctl = WebController(self.web_bridge)
        self.worker.log.connect(self.append_log)
        self.worker.status.connect(self.on_status)
        self.worker.phase.connect(self.on_phase)
        self.worker.connection.connect(self.on_connection)
        self.worker.phd2_connection.connect(self.on_phd2_connection)
        self.worker.debug_log_state.connect(self.on_debug_log_state)
        self.worker.armed_changed.connect(self.on_armed)
        self.worker.attempts_changed.connect(lambda n: self.lbl_attempts.setText(f"{n} in last hour"))
        self.worker.reference_changed.connect(self.lbl_ref.setText)
        self.worker.alert.connect(self.on_alert)
        self.worker.settings_applied.connect(self.on_settings_applied)
        self._settings_timer = QTimer(self)                   # applies edits shortly after the last one
        self._settings_timer.setSingleShot(True)
        self._settings_timer.setInterval(SETTINGS_APPLY_DELAY_MS)
        self._settings_timer.timeout.connect(self._commit_settings)
        for w in self._setting_widgets():
            sig = getattr(w, "textChanged", None) or getattr(w, "valueChanged", None) \
                or getattr(w, "currentIndexChanged", None) or w.toggled
            sig.connect(self._push_settings)
        self.worker.start()
        self._apply_web()
        if self._migrated:
            self.append_log(datetime.now().strftime("%H:%M:%S"), "INFO",
                            "Settings reset to the new recommended defaults: " + "; ".join(self._migrated))
        self._refresh_buttons()

    # ----- construction ---------------------------------------------------------------
    def _build_ui(self) -> None:
        self.ed_url = QLineEdit()
        self.btn_test = QPushButton("Test Connection")
        self.lbl_conn = QLabel("● Disconnected")
        self.lbl_conn.setStyleSheet("color:#d03030;font-weight:bold")
        conn = QGroupBox("NINA connection")
        cl = QGridLayout(conn)
        cl.addWidget(QLabel("API base URL"), 0, 0)
        cl.addWidget(self.ed_url, 0, 1, 1, 2)
        cl.addWidget(self.btn_test, 1, 1)
        cl.addWidget(self.lbl_conn, 1, 2)

        self.sp_thr = SafeDoubleSpinBox(); self.sp_thr.setRange(0.05, 50); self.sp_thr.setDecimals(2); self.sp_thr.setSingleStep(0.1)
        self.cb_unit = SafeComboBox(); self.cb_unit.addItems(["arcsec", "pixel"])
        self.sp_sustain = SafeSpinBox(); self.sp_sustain.setRange(1, 600); self.sp_sustain.setSuffix(" s")
        self.sp_cool = SafeSpinBox(); self.sp_cool.setRange(0, 7200); self.sp_cool.setSuffix(" s")
        self.sp_max = SafeSpinBox(); self.sp_max.setRange(1, 50); self.sp_max.setSuffix(" / hour")
        self.sp_recent = SafeSpinBox(); self.sp_recent.setRange(0, 40); self.sp_recent.setSuffix(" steps")
        self.sp_recent.setSpecialValueText("off")
        self.sp_recent.setToolTip("PHD2 source: the newest N steps must also exceed the threshold, so a burst that has "
                                  "already passed (but still sits in the rolling window) does not trigger a recovery.")
        self.sp_sustain.setToolTip("Guiding must stay 'bad' (RMS and, with PHD2, the newest steps above the threshold, or "
                                   "too many frames lost) this long before a recovery starts. Short dropouts such as thin "
                                   "cloud clear by themselves; 120 s ignores them.")
        self.chk_lostpct = QCheckBox("Trigger on star-loss rate (PHD2 source only)")
        self.sp_lostpct = SafeSpinBox(); self.sp_lostpct.setRange(5, 100); self.sp_lostpct.setSuffix(" %")
        self.sp_lostwin = SafeSpinBox(); self.sp_lostwin.setRange(PHD2_MIN_SAMPLES, 200); self.sp_lostwin.setSuffix(" frames")
        self.chk_seqend = QCheckBox("Disarm when the sequence finishes or is stopped")
        self.chk_seqend.setToolTip("After the sequence has stayed stopped for 60 s. Nothing is judged until it has "
                                   "been seen running, and the app's own recovery restart does not count.")
        self.chk_auto = QCheckBox("Auto-arm when the sequence is running and PHD2 is guiding")
        self.chk_auto.setToolTip("Edge-triggered: Stop Guarding or a safety stop stays in force until the sequence "
                                 "or guiding stops and starts again.")
        self.cb_source = SafeComboBox()
        self.cb_source.addItem("NINA Advanced API (guider/info)", "nina")
        self.cb_source.addItem("PHD2 event server", "phd2")
        self.ed_phd2_host = QLineEdit(); self.ed_phd2_host.setPlaceholderText("blank = same host as NINA")
        self.sp_phd2_port = SafeSpinBox(); self.sp_phd2_port.setRange(1, 65535)
        self.sp_phd2_win = SafeSpinBox(); self.sp_phd2_win.setRange(PHD2_MIN_SAMPLES, 500); self.sp_phd2_win.setSuffix(" steps")
        self.lbl_phd2 = QLabel("● not used"); self.lbl_phd2.setStyleSheet("color:#8a8a8a;font-weight:bold")
        src = QGroupBox("RMS source")
        sl = QFormLayout(src)
        sl.addRow("Read RMS from", self.cb_source)
        sl.addRow("PHD2 host", self.ed_phd2_host)
        sl.addRow("PHD2 port", self.sp_phd2_port)
        sl.addRow("Rolling RMS window", self.sp_phd2_win)
        sl.addRow("PHD2 server", self.lbl_phd2)

        self.chk_debug = QCheckBox("Save verbose debug log")
        self.ed_debug_dir = QLineEdit(); self.ed_debug_dir.setPlaceholderText("folder for the debug log files")
        btn_dbg = QPushButton("Browse...")
        btn_dbg.clicked.connect(self._browse_debug_dir)
        self.lbl_debug = QLabel("not recording"); self.lbl_debug.setWordWrap(True)
        self.lbl_debug.setStyleSheet("color:#8a8a8a")
        self.lbl_debug.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        dbg = QGroupBox("Debug log")
        dl_ = QGridLayout(dbg)
        dl_.addWidget(self.chk_debug, 0, 0, 1, 2)
        dl_.addWidget(self.ed_debug_dir, 1, 0)
        dl_.addWidget(btn_dbg, 1, 1)
        dl_.addWidget(self.lbl_debug, 2, 0, 1, 2)

        self.chk_web = QCheckBox("Enable web interface (local network)")
        self.sp_web_port = SafeSpinBox(); self.sp_web_port.setRange(1024, 65535)
        self.ed_web_token = QLineEdit(); self.ed_web_token.setReadOnly(True)
        self.ed_web_token.setPlaceholderText("access token for the controls")
        btn_wtoken = QPushButton("New token"); btn_wtoken.clicked.connect(self._new_web_token)
        btn_wcopy = QPushButton("Copy"); btn_wcopy.clicked.connect(self._copy_web_token)
        self.chk_web_view = QCheckBox("Require the token to view as well")
        self.lbl_web = QLabel("Web interface off"); self.lbl_web.setWordWrap(True)
        self.lbl_web.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_web.setStyleSheet("color:#8a8a8a")
        webg = QGroupBox("Web interface")
        wl = QGridLayout(webg)
        wl.addWidget(self.chk_web, 0, 0, 1, 3)
        wl.addWidget(QLabel("Port"), 1, 0); wl.addWidget(self.sp_web_port, 1, 1, 1, 2)
        wl.addWidget(self.ed_web_token, 2, 0, 1, 1); wl.addWidget(btn_wtoken, 2, 1); wl.addWidget(btn_wcopy, 2, 2)
        wl.addWidget(self.chk_web_view, 3, 0, 1, 3)
        wl.addWidget(self.lbl_web, 4, 0, 1, 3)
        self.chk_web.toggled.connect(self._on_web_toggled)

        thr = QGroupBox("Thresholds")
        tl = QFormLayout(thr)
        row = QHBoxLayout(); row.addWidget(self.sp_thr); row.addWidget(self.cb_unit)
        tl.addRow("Critical Total RMS", row)
        tl.addRow("Guiding bad for", self.sp_sustain)
        tl.addRow("Last steps must agree", self.sp_recent)
        tl.addRow("Cooldown after recovery", self.sp_cool)
        tl.addRow("Max recoveries", self.sp_max)
        tl.addRow(self.chk_lostpct)
        tl.addRow("   Frames lost at least", self.sp_lostpct)
        tl.addRow("   Over the last", self.sp_lostwin)
        tl.addRow(self.chk_auto)
        tl.addRow(self.chk_seqend)

        self.sp_wait = SafeSpinBox(); self.sp_wait.setRange(0, 3600); self.sp_wait.setSingleStep(60)
        self.sp_wait.setSuffix(" s"); self.sp_wait.setSpecialValueText("off")
        self.sp_wait.setToolTip("PHD2 source: when the star is lost with a 'low SNR' pattern (thin cloud), wait this long for it "
                                "to return before attempting a recovery. Imaging and guiding are left alone meanwhile.")
        self.cb_reselect = SafeComboBox()
        self.cb_reselect.addItem("Only after star loss", "star_loss")
        self.cb_reselect.addItem("Every recovery", "always")
        self.cb_reselect.addItem("Never", "never")
        self.cb_reselect.setToolTip("Uses PHD2's server (host/port above): loops on the re-centered field, then asks "
                                    "PHD2 to select a star outside the area of the old guide star.")
        self.chk_manual = QCheckBox("Use fixed coordinates instead of the running target (J2000 deg)")
        self.sp_ra = SafeDoubleSpinBox(); self.sp_ra.setRange(0, 360); self.sp_ra.setDecimals(5)
        self.sp_dec = SafeDoubleSpinBox(); self.sp_dec.setRange(-90, 90); self.sp_dec.setDecimals(5)
        self.sp_poll = SafeDoubleSpinBox(); self.sp_poll.setRange(0.5, 30); self.sp_poll.setSuffix(" s")
        self.sp_recenter = SafeSpinBox(); self.sp_recenter.setRange(60, 3600); self.sp_recenter.setSuffix(" s")
        self.sp_hold = SafeSpinBox(); self.sp_hold.setRange(1, 300); self.sp_hold.setSuffix(" s")
        self.sp_settle_to = SafeSpinBox(); self.sp_settle_to.setRange(30, 1200); self.sp_settle_to.setSuffix(" s")
        rec = QGroupBox("Recovery options")
        rl = QFormLayout(rec)
        rl.addRow("Re-select guide star", self.cb_reselect)
        rl.addRow("Wait for star (clouds)", self.sp_wait)
        rl.addRow(self.chk_manual)
        rl.addRow("   RA (deg)", self.sp_ra)
        rl.addRow("   Dec (deg)", self.sp_dec)
        rl.addRow("Poll interval", self.sp_poll)
        rl.addRow("Centering timeout", self.sp_recenter)
        rl.addRow("Settled = Guiding for", self.sp_hold)
        rl.addRow("Settle timeout", self.sp_settle_to)

        big = QFont(); big.setPointSize(11); big.setBold(True)
        self.lbl_phase = QLabel("Idle - not armed"); self.lbl_phase.setFont(big)
        self.lbl_gstate = QLabel("-"); self.lbl_ra = QLabel("-"); self.lbl_dec = QLabel("-")
        self.lbl_total = QLabel("-"); self.lbl_total.setFont(big)
        self.lbl_settle = QLabel("-"); self.lbl_lost = QLabel("-"); self.lbl_dither = QLabel("-")
        self.lbl_target = QLabel("-"); self.lbl_activity = QLabel("-"); self.lbl_frame = QLabel("-")
        self.lbl_attempts = QLabel("0 in last hour")
        self.lbl_nina_dash = QLabel("● Disconnected"); self.lbl_nina_dash.setStyleSheet("color:#d03030;font-weight:bold")
        self.lbl_phd2_dash = QLabel("● not used"); self.lbl_phd2_dash.setStyleSheet("color:#8a8a8a;font-weight:bold")
        self.lbl_ref = QLabel("waiting for a running target"); self.lbl_ref.setWordWrap(True)
        dash = QGroupBox("Monitoring dashboard")
        dl = QFormLayout(dash)
        dl.addRow("Automation", self.lbl_phase)
        dl.addRow("NINA API", self.lbl_nina_dash)
        dl.addRow("PHD2 server", self.lbl_phd2_dash)
        dl.addRow("PHD2 state", self.lbl_gstate)
        dl.addRow("RA RMS", self.lbl_ra)
        dl.addRow("Dec RMS", self.lbl_dec)
        dl.addRow("Total RMS", self.lbl_total)
        dl.addRow("Settling", self.lbl_settle)
        dl.addRow("Dithering", self.lbl_dither)
        dl.addRow("Frames lost", self.lbl_lost)
        dl.addRow("NINA target", self.lbl_target)
        dl.addRow("NINA activity", self.lbl_activity)
        dl.addRow("Active frame", self.lbl_frame)
        dl.addRow("Recoveries", self.lbl_attempts)
        dl.addRow("Recenter target", self.lbl_ref)

        self.btn_start = QPushButton("Start Guarding")
        self.btn_start.setToolTip("Arm the watchdog: starts evaluating RMS (it does not start PHD2 guiding)")
        self.btn_stop = QPushButton("Stop Guarding")
        self.btn_stop.setToolTip("Disarm the watchdog (aborts a recovery in progress). Does not stop PHD2 guiding.")
        self.btn_manual = QPushButton("Manual Recovery Trigger")
        btns = QHBoxLayout()
        for b in (self.btn_start, self.btn_stop, self.btn_manual):
            b.setMinimumHeight(34); btns.addWidget(b)

        self.log_view = QTextEdit(); self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(5000)
        self.log_view.setStyleSheet("background:#1e1e1e;color:#d4d4d4;font-family:Consolas,monospace;font-size:9pt")
        btn_clear = QPushButton("Clear log"); btn_clear.clicked.connect(self.log_view.clear)
        self.btn_replay = QPushButton("Replay guide log...")
        self.btn_replay.setToolTip("Run a PHD2 guide log through the trigger logic offline, to see when (and why) a recovery "
                                   "would have started with these settings. Nothing is sent to NINA or PHD2.")
        self.btn_replay.clicked.connect(self.on_replay)

        # --- Dashboard tab: monitoring dashboard, controls and the log
        dash_tab = QWidget()
        dv = QVBoxLayout(dash_tab)
        dv.addWidget(dash)
        dv.addLayout(btns)
        dv.addWidget(QLabel("Log"))
        dv.addWidget(self.log_view, 1)
        foot = QHBoxLayout()
        foot.addWidget(self.btn_replay); foot.addStretch(1); foot.addWidget(btn_clear)
        dv.addLayout(foot)

        # --- Settings tab: the setting groups in two columns, scrollable on a small window
        self.lbl_applied = QLabel("")                         # feedback line: shown only once a change has been applied
        self.lbl_applied.setStyleSheet("color:#2e9e44"); self.lbl_applied.setWordWrap(True)
        self.lbl_applied.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_applied.setVisible(False)
        col_a, col_b = QVBoxLayout(), QVBoxLayout()
        for w in (conn, src, thr):
            col_a.addWidget(w)
        for w in (rec, dbg, webg):
            col_b.addWidget(w)
        col_a.addStretch(1); col_b.addStretch(1)
        cols = QHBoxLayout()
        cols.addLayout(col_a, 1); cols.addLayout(col_b, 1)
        settings_w = QWidget()
        sv = QVBoxLayout(settings_w)
        sv.addWidget(self.lbl_applied)
        sv.addLayout(cols, 1)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(settings_w)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        self.tabs = QTabWidget()
        self.tabs.addTab(dash_tab, "Dashboard")
        self.tabs.addTab(scroll, "Settings")
        self.tabs.addTab(self._build_about_tab(), "About")
        self.tabs.setCurrentIndex(0)
        self.setCentralWidget(self.tabs)

        self.btn_test.clicked.connect(lambda: (self._flush_settings(), self.worker.command("test")))
        self.btn_start.clicked.connect(lambda: (self._flush_settings(), self.worker.command("arm")))
        self.btn_stop.clicked.connect(lambda: self.worker.command("disarm"))
        self.btn_manual.clicked.connect(self.on_manual)
        self.chk_manual.toggled.connect(self._refresh_buttons)
        self.cb_source.currentIndexChanged.connect(self._refresh_buttons)
        self.cb_reselect.currentIndexChanged.connect(self._refresh_buttons)

    def _build_about_tab(self) -> QWidget:
        """Logo, name and the copyright / license information."""
        logo = QLabel()
        pix = QPixmap.fromImage(render_icon(256))
        pix.setDevicePixelRatio(2.0)                          # drawn at 256 px, shown at 128 logical px: crisp on HiDPI
        logo.setPixmap(pix)
        logo.setFixedSize(128, 128)
        title = QLabel("Guide Guardian")
        title.setStyleSheet("color:#ffffff;font-size:34px;font-weight:700;letter-spacing:1px")
        tagline = QLabel(APP_TAGLINE)
        tagline.setStyleSheet(f"color:{ICON_TEAL};font-size:15px")
        version = QLabel(f"Version {APP_VERSION}")
        version.setStyleSheet("color:#aebbd0;font-size:13px")
        rule = QFrame(); rule.setFixedSize(64, 3); rule.setStyleSheet(f"background:{ICON_STAR};border-radius:1px")
        words = QVBoxLayout()
        words.addStretch(1); words.addWidget(title); words.addWidget(rule); words.addWidget(tagline)
        words.addWidget(version); words.addStretch(1)
        hero = QFrame(); hero.setObjectName("hero")
        hero.setStyleSheet(f"QFrame#hero{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {ICON_BG},stop:1 #1b3a63);"
                           f"border-radius:14px}}")
        hl = QHBoxLayout(hero)
        hl.setContentsMargins(36, 28, 36, 28); hl.setSpacing(28)
        hl.addWidget(logo); hl.addLayout(words, 1)

        gpl = "https://www.gnu.org/licenses/gpl-3.0.html"
        body = QLabel(
            f"<p style='font-size:15px'><b>Copyright &copy; 2026 Loran Hughes</b></p>"
            f"<p><b>Project:</b> <a href='{PROJECT_URL}'>{PROJECT_URL.removeprefix('https://')}</a> "
            f"(source code, releases and issue tracker)</p>"
            f"<p>Guide Guardian is free software: you can redistribute it and/or modify it under the terms of the "
            f"<a href='{gpl}'>GNU General Public License, version 3</a> (<code>GPL-3.0-only</code>). It is distributed in the "
            f"hope that it will be useful, but <b>without any warranty</b>; without even the implied warranty of "
            f"merchantability or fitness for a particular purpose. The corresponding source code is the project repository "
            f"this program was built from.</p>"
            f"<p><b>AI attribution:</b> this software was developed with AI assistance. It was written in collaboration "
            f"with Claude Sonnet 5.5, an AI model made by Anthropic, using Claude Code.</p>"
            f"<p style='color:#8a8a8a'>Built with Python {platform.python_version()}, PyQt6 {PYQT_VERSION_STR} and "
            f"Qt {QT_VERSION_STR} (PyQt6 is GPL v3 software). NINA, its Advanced API plugin and PHD2 are the work of their "
            f"respective authors; this project is independent and is not affiliated with or endorsed by them.</p>")
        body.setWordWrap(True)
        body.setOpenExternalLinks(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        card = QFrame()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(8, 4, 8, 4)
        cl.addWidget(body)

        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(24, 24, 24, 24); pl.setSpacing(20)
        pl.addWidget(hero)
        pl.addWidget(card)
        pl.addStretch(1)
        wrap = QScrollArea(); wrap.setWidgetResizable(True); wrap.setWidget(page)
        wrap.setFrameShape(QScrollArea.Shape.NoFrame)
        return wrap

    # ----- settings plumbing ----------------------------------------------------------
    def _setting_widgets(self):
        return [self.ed_url, self.cb_source, self.ed_phd2_host, self.sp_phd2_port, self.sp_phd2_win,
                self.sp_thr, self.cb_unit, self.sp_sustain, self.sp_cool, self.sp_max, self.chk_lostpct, self.sp_lostpct, self.sp_lostwin, self.sp_recent, self.chk_auto, self.chk_seqend,
                self.cb_reselect, self.sp_wait, self.chk_debug, self.ed_debug_dir, self.chk_web, self.sp_web_port,
                self.ed_web_token, self.chk_web_view, self.chk_manual, self.sp_ra, self.sp_dec, self.sp_poll, self.sp_recenter,
                self.sp_hold, self.sp_settle_to]

    def collect_settings(self) -> Settings:
        return Settings(
            base_url=self.ed_url.text().strip() or DEFAULT_URL, threshold=self.sp_thr.value(),
            unit=self.cb_unit.currentText(), rms_source=self.cb_source.currentData(),
            phd2_host=self.ed_phd2_host.text().strip(), phd2_port=self.sp_phd2_port.value(),
            phd2_window=self.sp_phd2_win.value(), sustain_s=self.sp_sustain.value(), cooldown_s=self.sp_cool.value(),
            max_attempts_per_hour=self.sp_max.value(), auto_arm=self.chk_auto.isChecked(), disarm_on_seq_end=self.chk_seqend.isChecked(),
            lost_pct_enabled=self.chk_lostpct.isChecked(), lost_pct=self.sp_lostpct.value(),
            lost_window=self.sp_lostwin.value(), recent_steps=self.sp_recent.value(),
            reselect_star=self.cb_reselect.currentData(), wait_for_star_s=self.sp_wait.value(),
            debug_log_enabled=self.chk_debug.isChecked(), debug_log_dir=self.ed_debug_dir.text().strip(),
            web_enabled=self.chk_web.isChecked(), web_port=self.sp_web_port.value(), web_token=self.ed_web_token.text().strip(),
            web_view_needs_token=self.chk_web_view.isChecked(), use_manual_target=self.chk_manual.isChecked(),
            manual_ra_deg=self.sp_ra.value(), manual_dec_deg=self.sp_dec.value(), poll_s=self.sp_poll.value(),
            recenter_timeout_s=self.sp_recenter.value(), settle_hold_s=self.sp_hold.value(),
            settle_timeout_s=self.sp_settle_to.value())

    def _push_settings(self, *_):
        """A setting was edited. Applying waits for a short pause, so a value typed digit by digit never takes effect
        half-typed (a sustain of "3" on the way to "30" would start a recovery at once)."""
        self._settings_timer.start()

    def _commit_settings(self) -> None:
        """Hand the settings to the running watchdog (monitoring continues), save them at once and refresh the web view."""
        self._settings_timer.stop()
        s = self.collect_settings()
        self.worker.update_settings(s, "Settings tab")
        self._apply_web()
        self._save_settings()
        self._qs.sync()

    def _flush_settings(self) -> None:
        """Apply a pending edit right now (before arming, testing the connection or a manual recovery)."""
        if self._settings_timer.isActive():
            self._commit_settings()

    def on_settings_applied(self, summary: str) -> None:
        self.lbl_applied.setText(f"Applied {datetime.now():%H:%M:%S}: {summary}")
        self.lbl_applied.setVisible(True)

    def _apply_web(self) -> None:
        self.lbl_web.setText(self.web_ctl.apply(self.collect_settings()))
        ok = self.web_ctl.server is not None
        self.lbl_web.setStyleSheet("color:#2e9e44" if ok else "color:#8a8a8a")

    def _new_web_token(self) -> None:
        self.ed_web_token.setText(secrets.token_urlsafe(12))

    def _copy_web_token(self) -> None:
        QApplication.clipboard().setText(self.ed_web_token.text())

    def _on_web_toggled(self, on: bool) -> None:
        if on and not self.ed_web_token.text().strip():
            self._new_web_token()

    def _load_settings(self) -> None:
        d = Settings()
        self._migrated = migrate_saved_settings(self._qs)
        def val(name):
            default = getattr(d, name)
            return self._qs.value(name, default, type=type(default))
        self.ed_url.setText(val("base_url")); self.sp_thr.setValue(val("threshold"))
        self.cb_unit.setCurrentText(val("unit"))
        self.cb_source.setCurrentIndex(max(0, self.cb_source.findData(val("rms_source"))))
        self.ed_phd2_host.setText(val("phd2_host")); self.sp_phd2_port.setValue(val("phd2_port"))
        self.sp_phd2_win.setValue(val("phd2_window"))
        self.sp_sustain.setValue(val("sustain_s"))
        self.sp_cool.setValue(val("cooldown_s")); self.sp_max.setValue(val("max_attempts_per_hour"))
        self.chk_auto.setChecked(val("auto_arm")); self.chk_seqend.setChecked(val("disarm_on_seq_end"))
        self.chk_lostpct.setChecked(val("lost_pct_enabled")); self.sp_lostpct.setValue(val("lost_pct"))
        self.sp_lostwin.setValue(val("lost_window")); self.sp_recent.setValue(val("recent_steps"))
        self.cb_reselect.setCurrentIndex(max(0, self.cb_reselect.findData(val("reselect_star"))))
        self.sp_wait.setValue(val("wait_for_star_s"))
        self.chk_debug.setChecked(val("debug_log_enabled")); self.ed_debug_dir.setText(val("debug_log_dir"))
        self.chk_web.setChecked(val("web_enabled")); self.sp_web_port.setValue(val("web_port"))
        self.ed_web_token.setText(val("web_token")); self.chk_web_view.setChecked(val("web_view_needs_token"))
        self.chk_manual.setChecked(val("use_manual_target")); self.sp_ra.setValue(val("manual_ra_deg"))
        self.sp_dec.setValue(val("manual_dec_deg")); self.sp_poll.setValue(val("poll_s"))
        self.sp_recenter.setValue(val("recenter_timeout_s")); self.sp_hold.setValue(val("settle_hold_s"))
        self.sp_settle_to.setValue(val("settle_timeout_s"))

    def _save_settings(self) -> None:
        for k, v in asdict(self.collect_settings()).items():
            self._qs.setValue(k, v)

    # ----- slots ----------------------------------------------------------------------
    def append_log(self, ts: str, level: str, msg: str) -> None:
        color = LOG_COLORS.get(level, "#d4d4d4")
        weight = "bold" if level in ("ERROR", "WARN") else "normal"
        self.log_view.append(
            f'<span style="color:#777">{ts}</span> <span style="color:{color};font-weight:{weight}">'
            f'[{level}] {escape(msg)}</span>')

    def on_status(self, s: dict) -> None:
        if not s.get("connected"):
            self.lbl_gstate.setText("NINA unreachable")
            return
        self.lbl_gstate.setText(s["state"] + ("" if s["guider_connected"] else " (guider not connected)"))
        unit = "″" if s["unit"] == "arcsec" else " px"
        fmt = lambda v: "-" if v is None else f"{v:.2f}{unit}"
        self.lbl_ra.setText(fmt(s["ra"])); self.lbl_dec.setText(fmt(s["dec"])); self.lbl_total.setText(
            fmt(s["total"]) + (f"   ({s['samples']}/{s['window']} steps)" if s["samples"] is not None else ""))
        hot = s["total"] is not None and s["state"] == "Guiding" and s["total"] > self.sp_thr.value()
        self.lbl_total.setStyleSheet("color:#d03030" if hot else "")
        self.lbl_settle.setText(s["settle"])
        dith = bool(s.get("dithering"))
        self.lbl_dither.setText("DITHERING" if dith else "-")
        self.lbl_dither.setStyleSheet("color:#d98a00;font-weight:bold" if dith else "")
        lost = s["lost"]
        self.lbl_lost.setText("-" if not lost else f"{lost['count']}/{lost['frames']}"
                              + ("" if lost["pct"] is None else f"  ({lost['pct']:.0f}%)"))
        self.lbl_target.setText(s["target"] or "-")
        self.lbl_activity.setText(s["activity"] or "-")
        frame = "-" if s["exposure_s"] is None else f"{s['exposure_s']:g} s"
        if s["exposing"] is not None:
            frame += "  (camera exposing)" if s["exposing"] else "  (camera idle)"
        self.lbl_frame.setText(frame)

    def on_phase(self, kind: str, text: str) -> None:
        self._phase_kind = kind
        self.lbl_phase.setText(text)
        self.lbl_phase.setStyleSheet(f"color:{PHASE_COLORS.get(kind, '#888')}")
        self._refresh_buttons()

    def on_connection(self, ok: bool, detail: str) -> None:
        self._connected = ok
        for lbl in (self.lbl_conn, self.lbl_nina_dash):
            lbl.setText("● Connected" if ok else "● Disconnected")
            lbl.setStyleSheet(f"color:{'#2e9e44' if ok else '#d03030'};font-weight:bold")
            lbl.setToolTip(detail)
        self._refresh_buttons()

    def _browse_debug_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder for the debug log", self.ed_debug_dir.text() or "")
        if folder:
            self.ed_debug_dir.setText(folder)

    def on_debug_log_state(self, path: str, error: str) -> None:
        if path:
            self.lbl_debug.setText(f"recording: {path}")
            self.lbl_debug.setStyleSheet("color:#2e9e44")
        elif error:
            self.lbl_debug.setText(f"cannot write: {error}")
            self.lbl_debug.setStyleSheet("color:#d03030")
        else:
            self.lbl_debug.setText("not recording")
            self.lbl_debug.setStyleSheet("color:#8a8a8a")

    def on_phd2_connection(self, ok: bool, detail: str) -> None:
        for lbl in (self.lbl_phd2, self.lbl_phd2_dash):
            lbl.setText("● Connected" if ok else "● Unreachable")
            lbl.setStyleSheet(f"color:{'#2e9e44' if ok else '#d03030'};font-weight:bold")
            lbl.setToolTip(detail)

    def on_armed(self, armed: bool) -> None:
        self._armed = armed
        self._refresh_buttons()

    def on_alert(self, msg: str) -> None:
        self.tabs.setCurrentIndex(0)                          # show the dashboard and log behind the alert
        QApplication.beep()
        QMessageBox.critical(self, f"{APP_NAME} - safety stop", msg)

    def on_manual(self) -> None:
        if QMessageBox.question(
                self, "Manual recovery",
                "Stop the exposure and guiding, slew/plate-solve to re-center the target, restart guiding and "
                "resume imaging now?") == QMessageBox.StandardButton.Yes:
            self._flush_settings()
            self.worker.command("manual")

    def on_replay(self) -> None:
        if self._replay_dlg is None:
            self._replay_dlg = ReplayDialog(self.collect_settings, self._qs, self)
        self._replay_dlg.show()
        self._replay_dlg.raise_()
        self._replay_dlg.activateWindow()

    def _refresh_buttons(self) -> None:
        busy = self._phase_kind == "recovering"
        self.btn_start.setEnabled(not self._armed and not busy)
        self.btn_stop.setEnabled(self._armed or busy)
        self.btn_manual.setEnabled(self._connected and not busy)
        manual = self.chk_manual.isChecked()
        self.sp_ra.setEnabled(manual); self.sp_dec.setEnabled(manual)
        phd2 = self.cb_source.currentData() == "phd2"
        for w in (self.sp_phd2_win, self.chk_lostpct, self.sp_lostpct, self.sp_lostwin, self.sp_recent, self.sp_wait):
            w.setEnabled(phd2)
        for w in (self.ed_phd2_host, self.sp_phd2_port):        # also used by the guide-star re-select RPC
            w.setEnabled(phd2 or self.cb_reselect.currentData() != "never")
        if not phd2:
            for lbl in (self.lbl_phd2, self.lbl_phd2_dash):
                lbl.setText("● not used"); lbl.setStyleSheet("color:#8a8a8a;font-weight:bold")

    def closeEvent(self, event) -> None:
        if self._armed or self._phase_kind == "recovering":
            if QMessageBox.question(self, "Quit", "Guarding is active. Stop it and quit?") != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._flush_settings()
        self._save_settings()
        if self._replay_dlg is not None:
            self._replay_dlg.shutdown()
            self._replay_dlg.close()
        self.web_ctl.stop()
        self.worker.shutdown()
        if not self.worker.wait(8000):
            self.worker.terminate()
        event.accept()


def setup_file_logging() -> None:
    folder = app_data_dir()
    handler = logging.FileHandler(os.path.join(folder, "guide_guardian.log"), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger = logging.getLogger("guideguardian")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)


def app_data_dir() -> str:
    """Per-user folder for the log and the headless instance's pid / stop files (GUIDEGUARDIAN_DATA_DIR overrides)."""
    folder = os.environ.get("GUIDEGUARDIAN_DATA_DIR") or os.environ.get("PHD2GUARD_DATA_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "GuideGuardian")
    os.makedirs(folder, exist_ok=True)
    return folder


LOCK_OFFSET = 0           # headless.lock holds nothing but the lock; the pid text lives in headless.pid (readable)


def _lock_byte(fh) -> None:
    """Take an exclusive, non-blocking lock on one byte of the (binary) file; raises OSError if somebody holds it."""
    if sys.platform == "win32":
        import msvcrt
        fh.seek(LOCK_OFFSET)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_byte(fh) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def acquire_instance_lock(path: str):
    """Try to become *the* headless instance. Returns the open, locked file (keep it open) or None if another one holds it.

    A lock is released by the OS when its process ends, however it ends, so - unlike a bare pid file - it can never
    be mistaken for a live instance after a crash, a kill or Windows reusing the pid.
    """
    fh = open(path, "a+b")
    try:
        _lock_byte(fh)
    except OSError:
        fh.close()
        return None
    return fh


def release_instance_lock(fh) -> None:
    _unlock_byte(fh)
    fh.close()


def _read_pid(path: str) -> int:
    try:
        with open(path, "rb") as fh:
            return int(fh.read(32).decode("ascii", "ignore").strip() or 0)
    except (OSError, ValueError):
        return 0


def stop_headless(timeout_s: float = 20.0) -> int:
    """`--stop`: ask the running headless instance (on this PC, same user) to shut down cleanly.

    No port or token: the instance holds a lock on headless.lock in the app data folder (the OS drops it when the process
    ends, however it ends) and watches for headless.stop there; headless.pid only tells which pid to name in messages.
    Exit code 0 = stopped, 1 = no instance running, 2 = it did not exit in time.
    """
    folder = app_data_dir()
    pidfile, stopfile = os.path.join(folder, "headless.pid"), os.path.join(folder, "headless.stop")
    lockfile = os.path.join(folder, "headless.lock")
    probe = acquire_instance_lock(lockfile)
    if probe is not None:                                  # nobody holds the lock: whatever is left over is stale
        release_instance_lock(probe)
        for stale in (pidfile, stopfile, lockfile):
            try:
                os.remove(stale)
            except OSError:
                pass
        print("No headless instance is running.", flush=True)
        return 1
    pid = _read_pid(pidfile)
    with open(stopfile, "w", encoding="utf-8") as fh:
        fh.write("stop\n")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        again = acquire_instance_lock(lockfile)
        if again is not None:                              # the instance has exited and released the lock
            release_instance_lock(again)
            for stale in (pidfile, stopfile, lockfile):
                try:
                    os.remove(stale)
                except OSError:
                    pass
            print(f"Headless instance (pid {pid or '?'}) stopped.", flush=True)
            return 0
        time.sleep(0.2)
    print(f"The headless instance (pid {pid or '?'}) did not exit within {timeout_s:.0f} s; end it in Task Manager "
          f"(taskkill /IM GuideGuardian.exe /F).", flush=True)
    return 2


def run_headless(args) -> int:
    """No window: the same engine and the web interface, for a PC beside the telescope. Configure it once in the GUI
    (same saved settings) or with --settings-file; stop it with Ctrl+C."""
    import signal
    folder = app_data_dir()
    pidfile, stopfile = os.path.join(folder, "headless.pid"), os.path.join(folder, "headless.stop")
    lockfile = os.path.join(folder, "headless.lock")
    instance_lock = acquire_instance_lock(lockfile)
    if instance_lock is None:
        print(f"A headless instance is already running (pid {_read_pid(pidfile) or '?'}); stop it with --stop first.",
              flush=True)
        return 3
    qs = open_qsettings(args.settings_file)
    migrate_saved_settings(qs)
    s = load_saved_settings(qs)
    token = args.token or s.web_token
    if not token:
        token = secrets.token_urlsafe(12)
        qs.setValue("web_token", token)
        qs.sync()
    def headless_view(saved: Settings, current_token: str) -> Settings:
        """The saved settings as this instance runs them: web interface always on, command-line overrides win."""
        return replace(saved, web_enabled=True, web_token=args.token or saved.web_token or current_token,
                       web_port=args.port or saved.web_port, base_url=args.nina_url or saved.base_url)

    s = headless_view(s, token)
    app = QCoreApplication([sys.argv[0]])
    worker = GuardWorker(s)
    bridge = WebBridge(worker)
    ctl = WebController(bridge, host=args.bind)
    worker.log.connect(lambda ts, lv, msg: print(f"{ts} [{lv}] {msg}", flush=True) if lv != "API" else None)
    worker.alert.connect(lambda msg: print(f"*** SAFETY STOP: {msg}", flush=True))
    print(f"{APP_NAME} headless - {ctl.apply(s)}", flush=True)
    print(f"Access token for the controls: {token}", flush=True)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    with open(pidfile, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    try:
        os.remove(stopfile)                                # a leftover request must not stop this fresh instance
    except OSError:
        pass

    def watch() -> None:                                   # also lets Python handle Ctrl+C inside the Qt loop
        if os.path.exists(stopfile):
            print("Stop requested (--stop).", flush=True)
            try:
                os.remove(stopfile)
            except OSError:
                pass
            app.quit()

    tick = QTimer()
    tick.start(400)
    tick.timeout.connect(watch)
    running = {"s": s}

    def reload_settings() -> None:
        """Pick up changes made to the saved settings (for example in the desktop app, which saves as you edit) and
        apply them to the running watchdog; the worker logs every change and keeps monitoring."""
        try:
            qs.sync()                                      # re-read the store if another process changed it
            fresh = headless_view(load_saved_settings(qs), running["s"].web_token)
        except Exception as exc:                           # noqa: BLE001 - a bad read must never stop the watchdog
            print(f"Could not re-read the saved settings: {exc}", flush=True)
            return
        if fresh == running["s"]:
            return
        web_changed = (fresh.web_port, fresh.web_token, fresh.web_view_needs_token) != (
            running["s"].web_port, running["s"].web_token, running["s"].web_view_needs_token)
        running["s"] = fresh
        worker.update_settings(fresh, "reloaded from the saved settings")
        status = ctl.apply(fresh)
        if web_changed:
            print(f"Web interface: {status}", flush=True)

    reloader = QTimer()
    reloader.start(int(SETTINGS_RELOAD_EVERY_S * 1000))
    reloader.timeout.connect(reload_settings)
    app.aboutToQuit.connect(lambda: (ctl.stop(), worker.shutdown(), worker.wait(8000)))
    worker.start()
    try:
        return app.exec()
    finally:
        release_instance_lock(instance_lock)
        for leftover in (pidfile, lockfile):
            try:
                os.remove(leftover)
            except OSError:
                pass


def run_replay_cli(args) -> int:
    """`--replay LOG`: the replay window's engine without a window. With --debug-log it prints the full session report
    (the rule in force comes from the debug log unless options are given); without it, the plain rule replay."""
    app = QCoreApplication([sys.argv[0]])
    given = {k: v for k, v in dict(
        threshold=args.threshold, unit=args.unit, sustain_s=args.sustain, phd2_window=args.window,
        cooldown_s=args.cooldown, recent_steps=args.recent, wait_for_star_s=args.wait, lost_pct=args.lost_pct,
        lost_window=args.lost_window).items() if v is not None}
    if args.no_lost_pct:
        given["lost_pct_enabled"] = False
    try:
        if args.debug_log:
            report = run_session_report(args.replay, replay_settings(**given), args.debug_log, True, args.poll,
                                        overrides=given)
            text = format_session_report(report)
        else:
            segs = parse_guide_log(args.replay)
            if not segs:
                print("No guiding sessions found in the log.", flush=True)
                return 1
            text = format_replay(run_replay(segs, replay_settings(**given), args.poll), quiet=args.quiet)
    except (OSError, ValueError) as exc:
        print(str(exc), flush=True)
        return 2
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text, flush=True)
    app.quit()
    return 0


def main() -> int:
    for name in ("stdout", "stderr"):                      # a windowed exe has no console streams
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))
    ap = argparse.ArgumentParser(description="Guide Guardian - NINA/PHD2 guiding watchdog")
    ap.add_argument("--headless", action="store_true", help="no window: run the engine and the web interface only")
    ap.add_argument("--port", type=int, help="web interface port (headless; default: the saved setting)")
    ap.add_argument("--token", help="web interface access token (headless; default: the saved token)")
    ap.add_argument("--bind", default="0.0.0.0", help="address the headless web interface listens on")
    ap.add_argument("--nina-url", help="NINA API base URL override (headless)")
    ap.add_argument("--settings-file", help="read/write settings from this .ini file instead of the registry")
    ap.add_argument("--stop", action="store_true", help="shut down a running --headless instance on this PC and exit")
    rp = ap.add_argument_group("guide-log replay (--replay; unset options use the app defaults, or the logged rule "
                               "with --debug-log)")
    rp.add_argument("--replay", metavar="LOG", help="replay a PHD2 guide log through the trigger logic, print the result and exit")
    rp.add_argument("--debug-log", metavar="FILE", help="with --replay: the watchdog's debug log of the same night "
                    "(rotated parts are included) for a full session report; the rule in force is read from it")
    rp.add_argument("--threshold", type=float)
    rp.add_argument("--unit", choices=["arcsec", "pixel"])
    rp.add_argument("--sustain", type=int, help="seconds guiding must stay bad")
    rp.add_argument("--window", type=int, help="rolling RMS window, steps")
    rp.add_argument("--recent", type=int, help="newest N steps must also exceed the threshold (0 = off)")
    rp.add_argument("--cooldown", type=int)
    rp.add_argument("--lost-pct", type=int, help="star-loss trigger: percent of recent frames lost")
    rp.add_argument("--no-lost-pct", action="store_true", help="disable the star-loss-rate trigger")
    rp.add_argument("--lost-window", type=int)
    rp.add_argument("--wait", type=int, help="seconds to wait for a cloud-lost star (0 = off)")
    rp.add_argument("--poll", type=float, default=1.0, help="simulated poll interval, seconds")
    rp.add_argument("--quiet", action="store_true", help="only the first recovery of each session")
    rp.add_argument("--output", metavar="FILE", help="also write the report to this file (the windowed exe has no console)")
    args, qt_args = ap.parse_known_args()
    if args.stop:
        return stop_headless()
    if args.replay:
        return run_replay_cli(args)
    setup_file_logging()
    if args.headless:
        return run_headless(args)
    if sys.platform == "win32":                           # own taskbar identity (and icon) even when run via python
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GuideGuardian.Watchdog")
        except Exception:                                  # noqa: BLE001 - cosmetic only
            pass
    app = QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon())
    win = MainWindow(args.settings_file)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
