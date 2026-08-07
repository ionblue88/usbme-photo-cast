#!/usr/bin/env python3
#
# USBme Photo Cast — cast photos to a 6-colour e-ink frame over USB.
# Copyright (C) 2026 ionblue88  <https://github.com/ionblue88>
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE.  See the GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License along
# with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Original author: ionblue88.  Please keep this notice and the headers in forks.
"""
USBme Photo Cast — cast photos to a 6-colour e-ink frame over USB.

Choose a photo, crop it to the frame's panel, and either send it now or add it to
a Photo Pool. With "Auto-send on connect" ticked, a random photo from the pool is
pushed to the frame each time you plug it in.

USB protocol (6-colour e-ink frame over a CH340 serial link):
  image -> 4bpp indexed .bin (2px/byte); serial 115200 8N1;
  framing AC 5A <cmd> <len2> <data> <xor>;
  transfer: 0x26 header(size+sum) -> "OK" -> "EARSE_OK" -> 0x27 chunk[idx] -> "<idx>_OK".

Deps: Pillow, pyserial (numpy optional, speeds up packing). Standard-library tkinter GUI.

Author: ionblue88 · https://github.com/ionblue88/usbme-photo-cast
"""

__author__ = "ionblue88"
__copyright__ = "Copyright (C) 2026 ionblue88"
__license__ = "AGPL-3.0-or-later"

import os, sys, json, time, random, threading, queue, uuid, re, math, hashlib
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk, ImageEnhance, ImageOps
try:
    import pillow_heif; pillow_heif.register_heif_opener()   # HEIC/HEIF (iPhone)
except Exception:
    pass
try:
    import pillow_avif  # noqa: F401  -- registers AVIF (Google/web images)
except Exception:
    pass
import serial
from serial.tools import list_ports
try:
    import numpy as np
except ImportError:
    np = None

# ---------------- config ----------------
# panel resolution per model (detected from the connected frame's serial number)
PANEL_SIZES = {"F7 Pro": (1024, 600), "F7": (800, 480), "F13": (1200, 1600), "F8": (1200, 1600)}
PANEL_W, PANEL_H = 1024, 600          # current panel; updated live from the connected frame
ASPECT = PANEL_W / PANEL_H
CHUNK = 1024

def set_panel(model):
    global PANEL_W, PANEL_H, ASPECT
    PANEL_W, PANEL_H = PANEL_SIZES.get(model, (1024, 600))
    ASPECT = PANEL_W / PANEL_H
BAUD = 115200
CH340_VIDPID = (0x1A86, 0x7523)
APP_DIR = os.path.join(os.path.expanduser("~"), "USBmePhotoCast")
POOL_DIR = os.path.join(APP_DIR, "pool")
POOL_JSON = os.path.join(APP_DIR, "pool.json")
# gentle preprocessing for 6-colour e-ink (approximates the app's PhotoConvert)
ENH_BRIGHTNESS, ENH_CONTRAST, ENH_COLOR, ENH_SHARPNESS = 1.0, 1.08, 1.05, 1.4
# "Optimise for e-ink" pass (per-photo checkbox): reclaim the tonal range and boost colour
# so flat, low-saturation photos don't look dull on the limited 6-colour panel.
OPT_CUTOFF, OPT_COLOR, OPT_CONTRAST, OPT_SHARPNESS = 1, 1.35, 1.12, 1.5

PALETTE_PURE = [(0,0,0),(255,255,255),(255,0,0),(0,255,0),(0,0,255),(255,255,0)]
DEVICE_CODE  = [0, 1, 3, 6, 5, 2]     # palette slot -> frame nibble code
# provenance: usbme-photo-cast — ionblue88 (original author); retain under AGPL-3.0

os.makedirs(POOL_DIR, exist_ok=True)

# ---------------- encoder ----------------
def _palette_image():
    pal = Image.new("P", (16, 16))
    flat = []
    for c in PALETTE_PURE: flat += list(c)
    flat += [0, 0, 0] * (256 - len(PALETTE_PURE))
    pal.putpalette(flat)
    return pal

def encode_to_panel(img, pw, ph, optimise=False):
    """RGB image (already the panel's aspect) -> (bin_bytes = pw*ph/2, dithered preview) at pw x ph."""
    img = img.convert("RGB").resize((pw, ph), Image.LANCZOS)
    if optimise:
        img = ImageOps.autocontrast(img, cutoff=OPT_CUTOFF, preserve_tone=True)  # reclaim range
        img = ImageEnhance.Color(img).enhance(OPT_COLOR)                          # boost saturation
        img = ImageEnhance.Contrast(img).enhance(OPT_CONTRAST)
        img = ImageEnhance.Sharpness(img).enhance(OPT_SHARPNESS)
    else:
        if ENH_BRIGHTNESS != 1.0: img = ImageEnhance.Brightness(img).enhance(ENH_BRIGHTNESS)
        if ENH_CONTRAST   != 1.0: img = ImageEnhance.Contrast(img).enhance(ENH_CONTRAST)
        if ENH_COLOR      != 1.0: img = ImageEnhance.Color(img).enhance(ENH_COLOR)
        if ENH_SHARPNESS  != 1.0: img = ImageEnhance.Sharpness(img).enhance(ENH_SHARPNESS)
    q = img.quantize(palette=_palette_image(), dither=Image.Dither.FLOYDSTEINBERG)
    idx = q.tobytes()                                  # 1 byte/pixel palette index 0..5
    if np is not None:
        lut = np.zeros(256, dtype=np.uint8); lut[:len(DEVICE_CODE)] = DEVICE_CODE
        codes = lut[np.frombuffer(idx, dtype=np.uint8)]
        out = ((codes[0::2] << 4) | codes[1::2]).astype(np.uint8).tobytes()
    else:
        out = bytearray((pw // 2) * ph); j = 0
        for i in range(0, len(idx), 2):
            out[j] = ((DEVICE_CODE[idx[i]] & 15) << 4) | (DEVICE_CODE[idx[i+1]] & 15); j += 1
        out = bytes(out)
    return out, q.convert("RGB")

# ---------------- sender (confirmed handshake) ----------------
def find_frame_port():
    for p in list_ports.comports():
        if (p.vid, p.pid) == CH340_VIDPID:
            return p.device
    return None

def _xor(d):
    x = 0
    for b in d: x ^= b
    return x
def _q(cmd, payload):
    n = len(payload); body = bytes([0xAC,0x5A,cmd,(n>>8)&255,n&255]) + payload
    return body + bytes([_xor(body)])
def _hdr(total, s):
    body = bytes([0xAC,0x5A,0x26,0x00,0x05,(total>>24)&255,(total>>16)&255,(total>>8)&255,total&255,s&255])
    return body + bytes([_xor(body)])
def _chunk(c, i):
    n = len(c)+2; body = bytes([0xAC,0x5A,0x27,(n>>8)&255,n&255,(i>>8)&255,i&255]) + c
    return body + bytes([_xor(body)])
def _txt(b): return "".join(chr(c) if 32<=c<127 else "." for c in b)
def _read_for(ser, wants, timeout):
    end = time.monotonic()+timeout; buf = bytearray()
    while time.monotonic() < end:
        n = ser.in_waiting
        if n:
            buf += ser.read(n)
            t = _txt(buf)
            for w in wants:
                if w in t: return w, bytes(buf)
        else:
            time.sleep(0.02)
    return None, bytes(buf)

def send_bin(port, data, on_progress=None, on_log=None):
    """Send a 307200-byte .bin to the frame. Returns True on success. Only emits
    read queries (0x23) and image opcodes 0x26/0x27 - never 0x13/0x14."""
    def log(m):
        if on_log: on_log(m)
    if len(data) != (PANEL_W // 2) * PANEL_H:
        log(f"bin is {len(data)} bytes, expected {(PANEL_W//2)*PANEL_H}"); return False
    s = sum(data) & 0xFF
    ser = serial.Serial(port, BAUD, bytesize=8, parity="N", stopbits=1, timeout=0.2)
    try:
        time.sleep(0.3); ser.reset_input_buffer()
        ser.write(_q(0x23, b"SN")); m, raw = _read_for(ser, ["SN:", "VR:"], 2.0)
        if not m: log("Frame not responding — wake it (power button) and retry."); return False
        log("Preparing…")
        ser.reset_input_buffer(); ser.write(_hdr(len(data), s))
        m, raw = _read_for(ser, ["OK", "ok", "FAIL", "fail"], 4.0)
        if (not m) or ("FAIL" in _txt(raw).upper()):
            log("Frame rejected the transfer."); return False
        # the frame's "OK" and "EARSE_OK" may arrive together or a moment apart
        if "EARSE_OK" not in _txt(raw).upper():
            log("erasing…")
            m2, r2 = _read_for(ser, ["EARSE_OK", "earse_ok", "erase_ok"], 12.0)
            if not m2: log("no erase-complete ack"); return False
        ser.reset_input_buffer()          # clear any residue before chunking
        total = (len(data) + CHUNK - 1) // CHUNK
        for i in range(total):
            pkt = _chunk(data[i*CHUNK:(i+1)*CHUNK], i); ok = False
            for _ in range(3):
                ser.write(pkt); m, _r = _read_for(ser, [f"{i}_OK", f"{i}_ok"], 2.0)
                if m: ok = True; break
            if not ok: log(f"chunk {i} not acked - aborted"); return False
            if on_progress and (i % 6 == 0 or i == total-1): on_progress((i+1)/total)
        log("done - image sent."); return True
    finally:
        ser.close()

# ---------------- wifi config over USB (0x14) ----------------
def send_wifi(port, ssid, password, phone=""):
    """Push Wi-Fi credentials to the frame over USB (opcode 0x14, K:ssid;V:pwd;P:phone;).
    Returns (sent_ok, reply_text)."""
    ser = serial.Serial(port, BAUD, bytesize=8, parity="N", stopbits=1, timeout=0.2)
    try:
        time.sleep(0.2); ser.reset_input_buffer()
        payload = ("K:%s;V:%s;P:%s;" % (ssid, password, phone)).encode("ascii", "ignore")
        ser.write(_q(0x14, payload))
        time.sleep(0.6)
        resp = ser.read(ser.in_waiting or 1)
        return True, _txt(resp).strip()
    finally:
        ser.close()

# ---------------- frame info (read-only queries) ----------------
def query_frame_info(port):
    """Best-effort SN / firmware / battery voltage. Read-only opcodes only."""
    info = {}
    try:
        ser = serial.Serial(port, BAUD, bytesize=8, parity="N", stopbits=1, timeout=0.2)
    except Exception:
        return info
    try:
        time.sleep(0.2); ser.reset_input_buffer()
        ser.write(_q(0x23, b"SN")); _m, raw = _read_for(ser, ["VR:"], 1.2); t = _txt(raw)
        sn = re.search(r"SN:([^;]+);", t)
        if not sn: return info                 # asleep / no reply — skip the rest
        info["sn"] = sn.group(1)
        vr = re.search(r"VR:([^;]+);", t)
        if vr: info["fw"] = vr.group(1)
        ser.reset_input_buffer()
        ser.write(_q(0x25, b"VL")); _m, raw = _read_for(ser, ["V:"], 1.2)
        v = re.search(r"V:(\d+)", _txt(raw))
        if v: info["mv"] = int(v.group(1))
    except Exception:
        pass
    finally:
        ser.close()
    return info

def battery_level(mv):
    for th, lvl in ((3650,0),(3750,1),(3850,2),(4000,3)):
        if mv < th: return lvl
    return 4

def model_name(sn):
    u = (sn or "").upper()
    if u.startswith("F7PRO"): return "F7 Pro"
    if u.startswith("F13"):   return "F13"
    if u.startswith("F8"):    return "F8"
    if u.startswith("F7"):    return "F7"
    return "Frame"

# ---------------- aspect / model helpers ----------------
def aspect_key(w, h):
    """Orientation-free reduced ratio 'long:short' — e.g. 1024x600 -> '128:75'. This is the key a
    crop is stored under; frames that share it (F13/F8) share a crop, and the frame can be rotated."""
    lo, hi = sorted((int(w), int(h)))
    g = math.gcd(hi, lo) or 1
    return f"{hi // g}:{lo // g}"

def panel_aspect_key():
    return aspect_key(PANEL_W, PANEL_H)

def aspect_choices():
    """Distinct crop aspects with model labels: [(label, akey, (w, h)), ...]."""
    groups = {}
    for m, wh in PANEL_SIZES.items():
        k = aspect_key(*wh)
        groups.setdefault(k, {"models": [], "wh": wh})
        groups[k]["models"].append(m)
    return [(" / ".join(v["models"]) + f"  ({v['wh'][0]}×{v['wh'][1]})", k, v["wh"])
            for k, v in groups.items()]

def wh_for_aspect(akey):
    for _, k, wh in aspect_choices():
        if k == akey: return wh
    return (PANEL_W, PANEL_H)

def sha256_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""): h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""

# ---------------- settings ----------------
SETTINGS_JSON = os.path.join(APP_DIR, "settings.json")
def load_settings():
    try:
        with open(SETTINGS_JSON, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {}
def save_settings(d):
    try:
        with open(SETTINGS_JSON, "w", encoding="utf-8") as f: json.dump(d, f)
    except Exception: pass

# ---------------- pool ----------------
def load_pool():
    if os.path.exists(POOL_JSON):
        try:
            with open(POOL_JSON, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: return []
    return []
def save_pool(items):
    with open(POOL_JSON, "w", encoding="utf-8") as f: json.dump(items, f, indent=1)
def _src_path(pid):
    jpg = os.path.join(POOL_DIR, pid + "_src.jpg")
    if os.path.exists(jpg): return jpg
    png = os.path.join(POOL_DIR, pid + "_src.png")           # legacy master
    return png if os.path.exists(png) else jpg

def save_source(pid, img, keep_original):
    """Store the source photo. keep_original -> full-res JPEG q95; else q92 capped at 3000px long edge."""
    img = img.convert("RGB")
    if not keep_original:
        longest = max(img.width, img.height)
        if longest > 3000:
            s = 3000.0 / longest
            img = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)
    path = os.path.join(POOL_DIR, pid + "_src.jpg")
    img.save(path, "JPEG", quality=(95 if keep_original else 92))
    legacy = os.path.join(POOL_DIR, pid + "_src.png")
    if os.path.exists(legacy):
        try: os.remove(legacy)
        except OSError: pass
    return path

def _clamp_box(nbox):
    x0, y0, x1, y1 = nbox
    x0, x1 = sorted((min(max(x0, 0.0), 1.0), min(max(x1, 0.0), 1.0)))
    y0, y1 = sorted((min(max(y0, 0.0), 1.0), min(max(y1, 0.0), 1.0)))
    return [x0, y0, x1, y1]

def render_crop(pid, crop, pw, ph):
    """source + normalized crop box -> (bin_bytes, display preview) fitted to pw x ph. The cropped
    region is rotated to fill the panel buffer if its orientation differs (the frame is rotated to
    match); the returned preview is kept in the crop's own orientation."""
    src = Image.open(_src_path(pid)).convert("RGB")
    W, H = src.size
    x0, y0, x1, y1 = _clamp_box(crop["box"])
    region = src.crop((round(x0 * W), round(y0 * H), round(x1 * W), round(y1 * H)))
    if region.width < 2 or region.height < 2: region = src
    rotated = (region.width >= region.height) != (pw >= ph)
    panel_src = region.rotate(90, expand=True) if rotated else region
    bin_bytes, prev = encode_to_panel(panel_src, pw, ph, crop.get("optimise", True))
    disp = prev.rotate(-90, expand=True) if rotated else prev
    return bin_bytes, disp

def crop_for_panel(item):
    """The crop stored for the connected panel's aspect, if any."""
    return item.get("crops", {}).get(panel_aspect_key())

def _thumb_target(item):
    """(crop, pw, ph) for this item's pool thumbnail: the connected panel's crop if present,
    else the first crop rendered at its own model's resolution."""
    crops = item.get("crops", {})
    if not crops: return None, 0, 0
    ak = panel_aspect_key()
    if ak in crops: return crops[ak], PANEL_W, PANEL_H
    first = next(iter(crops)); return (crops[first], *wh_for_aspect(first))

def write_thumbnail(item):
    """Cache <pid>.png = dithered preview of the item's display crop."""
    pid = item["id"]; crop, pw, ph = _thumb_target(item)
    path = os.path.join(POOL_DIR, pid + ".png")
    if not crop:
        if os.path.exists(path):
            try: os.remove(path)
            except OSError: pass
        return
    try:
        _, disp = render_crop(pid, crop, pw, ph)
        disp.save(path)
    except Exception:
        pass

def add_to_pool(name, src_img, akey, nbox, optimise, keep_original):
    items = load_pool(); pid = uuid.uuid4().hex[:12]
    save_source(pid, src_img, keep_original)
    it = {"id": pid, "name": name, "added": int(time.time()),
          "src_hash": sha256_file(_src_path(pid)),
          "crops": {akey: {"box": _clamp_box(nbox), "optimise": bool(optimise)}}}
    items.append(it); save_pool(items)
    write_thumbnail(it)
    return pid

def set_crop(pid, akey, nbox, optimise):
    """Add or replace one aspect's crop on an existing item; refresh its thumbnail."""
    items = load_pool(); it = next((x for x in items if x["id"] == pid), None)
    if not it: return
    it.setdefault("crops", {})[akey] = {"box": _clamp_box(nbox), "optimise": bool(optimise)}
    save_pool(items); write_thumbnail(it)

def bin_for_pool_item(pid, pw=None, ph=None):
    """Encode the crop matching the (connected) panel -> bin_bytes, or None if there's no crop
    stored for that aspect."""
    if pw is None: pw, ph = PANEL_W, PANEL_H
    it = next((x for x in load_pool() if x["id"] == pid), None)
    if not it: return None
    crop = it.get("crops", {}).get(aspect_key(pw, ph))
    if not crop: return None
    bin_bytes, _ = render_crop(pid, crop, pw, ph)
    return bin_bytes

def delete_from_pool(pid):
    items = [it for it in load_pool() if it["id"] != pid]; save_pool(items)
    for f in os.listdir(POOL_DIR):
        if f.startswith(pid):
            try: os.remove(os.path.join(POOL_DIR, f))
            except OSError: pass

def migrate_pool():
    """One-time upgrade of legacy items (panel-sized _src.png master + portrait/optimise fields)
    to the new model: a source photo + per-aspect normalized crops + a source hash."""
    items = load_pool(); changed = False
    for it in items:
        if "crops" in it: continue
        changed = True; pid = it["id"]
        old_png = os.path.join(POOL_DIR, pid + "_src.png")
        if os.path.exists(old_png):
            img = Image.open(old_png).convert("RGB")
            save_source(pid, img, keep_original=True)          # already panel-sized; don't shrink again
            ak = aspect_key(img.width, img.height)
            it["crops"] = {ak: {"box": [0.0, 0.0, 1.0, 1.0], "optimise": bool(it.get("optimise", False))}}
            it["src_hash"] = sha256_file(_src_path(pid))
        else:
            it["crops"] = {}; it["src_hash"] = ""
        it.pop("portrait", None); it.pop("optimise", None)
        stale = os.path.join(POOL_DIR, pid + ".bin")
        if os.path.exists(stale):
            try: os.remove(stale)
            except OSError: pass
    if changed:
        save_pool(items)
        for it in items: write_thumbnail(it)

def revalidate_source(pid):
    """If a pool item's source file changed on disk (hash mismatch), its crops no longer match
    the image — drop them and re-baseline the hash so the user re-crops. Returns True if reset."""
    items = load_pool(); it = next((x for x in items if x["id"] == pid), None)
    if not it: return False
    cur = sha256_file(_src_path(pid))
    if not cur or cur == it.get("src_hash"): return False
    it["crops"] = {}; it["src_hash"] = cur
    save_pool(items); write_thumbnail(it)          # no crops -> clears the cached thumbnail
    return True

def revalidate_all():
    """Check every item's source once (e.g. at startup). Returns names of items whose crops
    were reset because their source changed."""
    items = load_pool(); changed = []; dirty = False
    for it in items:
        cur = sha256_file(_src_path(it["id"]))
        if cur and cur != it.get("src_hash"):
            it["crops"] = {}; it["src_hash"] = cur; changed.append(it); dirty = True
    if dirty:
        save_pool(items)
        for it in changed: write_thumbnail(it)
    return [it["name"] for it in changed]

# =================================================================
#                              GUI
# =================================================================
class _Tooltip:
    """Minimal hover tooltip for a widget."""
    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _=None):
        if self.tip or not self.text: return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget); self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, wraplength=300,
                 font=("Segoe UI", 9), padx=6, pady=4).pack()

    def _hide(self, _=None):
        if self.tip: self.tip.destroy(); self.tip = None


class CropDialog(tk.Toplevel):
    """Aspect-locked crop with a live 6-colour e-ink preview beside it. Drag inside the crop
    box to move, drag a corner to resize; the preview reflows as you resize the window."""
    MAXW, MAXH, HANDLE = 720, 520, 9

    def __init__(self, master, img, name, on_done, mode="add", crops=None,
                 start_aspect=None, lock_aspect=None):
        super().__init__(master)
        self.transient(master); self.grab_set()
        self.on_done = on_done
        self.src = img            # already-loaded, EXIF-corrected RGB image
        self.name = name
        self.mode = mode
        self.saved_crops = crops or {}
        self.tkimg = None; self._prev_tk = None
        self.drag_mode = None; self.anchor = None
        self.optimise = tk.BooleanVar(value=True)            # boost colour/contrast for e-ink
        self.keep_orig = tk.BooleanVar(value=False)          # store the full-res original
        self._resize_after = None
        self._choices = aspect_choices()                     # [(label, akey, (w,h)), ...]
        init_akey = lock_aspect or start_aspect or panel_aspect_key()
        self.cur_akey, self.cur_wh = init_akey, wh_for_aspect(init_akey)
        self.aspect = self.cur_wh[0] / self.cur_wh[1]        # crop-box aspect (device native)

        main = ttk.Frame(self, padding=10); main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=0)                    # crop area — natural size
        main.columnconfigure(1, weight=1)                    # preview — takes the extra room
        main.rowconfigure(0, weight=1)

        crop_box = ttk.LabelFrame(main, text="Crop", padding=6)
        crop_box.grid(row=0, column=0, sticky="n", padx=(0,10))
        self.canvas = tk.Canvas(crop_box, background="#222", highlightthickness=0)
        self.canvas.pack()

        prev_box = ttk.LabelFrame(main, text="Frame preview — 6-colour e-ink", padding=6)
        prev_box.grid(row=0, column=1, sticky="nsew")
        prev_box.rowconfigure(0, weight=1); prev_box.columnconfigure(0, weight=1)
        self.preview_lbl = ttk.Label(prev_box, anchor="center")
        self.preview_lbl.grid(row=0, column=0, sticky="nsew")
        prev_box.bind("<Configure>", self._on_prev_resize)

        opts = ttk.Frame(self, padding=(10,4)); opts.pack(fill="x")
        ttk.Label(opts, text="Crop for:").pack(side="left")
        self.aspect_cb = ttk.Combobox(opts, state="readonly", width=24,
                                      values=[c[0] for c in self._choices])
        self.aspect_cb.pack(side="left", padx=(4,0))
        self.aspect_cb.bind("<<ComboboxSelected>>", self._on_aspect_pick)
        if mode == "add":
            so = ttk.Checkbutton(opts, text="Store original image", variable=self.keep_orig)
            so.pack(side="left", padx=(16,0))
            _Tooltip(so, "Keep the full-resolution photo (large files). Leave off to store a "
                         "space-saving ~3000px copy — plenty for any 6-colour frame.")

        bar = ttk.Frame(self, padding=(10,0,10,10)); bar.pack(fill="x")
        ttk.Button(bar, text="↻ Rotate image", command=self.rotate_image).pack(side="left")
        ttk.Button(bar, text="Rotate crop box", command=self.toggle_orient).pack(side="left", padx=(8,0))
        self.orient_lbl = ttk.Label(bar, text="landscape" if self.aspect >= 1 else "portrait")
        self.orient_lbl.pack(side="left", padx=(6,0))
        chk = ttk.Checkbutton(bar, text="Optimise for e-ink", variable=self.optimise,
                              command=self._update_preview)
        chk.pack(side="left", padx=(16,0))
        _Tooltip(chk, "Boost colour and contrast before sending, so dull, low-saturation photos "
                      "look less flat on the frame's 6-colour e-ink panel. "
                      "Compare on vs off in the preview beside the crop.")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Use this crop", command=self.confirm).pack(side="right", padx=6)

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>",
                         lambda e: (setattr(self, "drag_mode", None), self._update_preview()))
        self._fit()
        self._select_aspect(init_akey)                       # set combobox + load any saved crop
        if lock_aspect:
            self.aspect_cb.configure(state="disabled")       # send-flow: fixed to the connected frame
        self.minsize(self.dw + 300, self.dh + 150)           # room for crop + preview + option rows

    def _fit(self):
        iw, ih = self.src.size
        self.scale = min(self.MAXW/iw, self.MAXH/ih, 1.0)
        self.dw, self.dh = int(iw*self.scale), int(ih*self.scale)
        self.disp = self.src.resize((self.dw, self.dh), Image.LANCZOS)
        self.tkimg = ImageTk.PhotoImage(self.disp)
        self.canvas.config(width=self.dw, height=self.dh)
        self._reset_box()
        self.redraw()
        self._update_preview()

    def _reset_box(self):
        # largest box of the current aspect, centred in the image
        if self.dw / self.dh >= self.aspect:
            bh = self.dh; bw = bh * self.aspect
        else:
            bw = self.dw; bh = bw / self.aspect
        self.box = [(self.dw-bw)/2, (self.dh-bh)/2, (self.dw+bw)/2, (self.dh+bh)/2]

    def toggle_orient(self):
        self.aspect = 1.0 / self.aspect
        self.orient_lbl.config(text="landscape" if self.aspect >= 1 else "portrait")
        self._reset_box(); self.redraw(); self._update_preview()

    def rotate_image(self):
        self.src = self.src.rotate(-90, expand=True)   # 90° CW; corrects a sideways source photo
        self._fit()

    def redraw(self):
        c = self.canvas; c.delete("all")
        c.create_image(0, 0, anchor="nw", image=self.tkimg)
        x0, y0, x1, y1 = self.box
        # dim outside the crop
        for rx0, ry0, rx1, ry1 in [(0,0,self.dw,y0),(0,y1,self.dw,self.dh),
                                    (0,y0,x0,y1),(x1,y0,self.dw,y1)]:
            c.create_rectangle(rx0, ry0, rx1, ry1, fill="#000", stipple="gray50", outline="")
        c.create_rectangle(x0, y0, x1, y1, outline="#37c", width=2)
        h = self.HANDLE
        for hx, hy in [(x0,y0),(x1,y0),(x0,y1),(x1,y1)]:
            c.create_rectangle(hx-h, hy-h, hx+h, hy+h, fill="#37c", outline="#fff")

    def _update_preview(self):
        """Render the current crop through the e-ink encoder and show it, scaled to fit its box."""
        if not getattr(self, "preview_lbl", None) or not getattr(self, "scale", None): return
        try:
            s = self.scale
            region = self.src.crop((int(self.box[0]/s), int(self.box[1]/s),
                                    int(self.box[2]/s), int(self.box[3]/s))).convert("RGB")
            if region.width < 2 or region.height < 2: return
            pw, ph = self.cur_wh
            rotated = (region.width >= region.height) != (pw >= ph)
            panel_src = region.rotate(90, expand=True) if rotated else region
            _, prev = encode_to_panel(panel_src, pw, ph, self.optimise.get())
            disp = prev.rotate(-90, expand=True) if rotated else prev
            aw, ah = self.preview_lbl.winfo_width(), self.preview_lbl.winfo_height()
            if aw < 60 or ah < 60: aw, ah = 380, 430          # before the first layout pass
            sc = max(0.02, min((aw-4)/disp.width, (ah-4)/disp.height, 1.5))
            thumb = disp.resize((max(1, int(disp.width*sc)), max(1, int(disp.height*sc))), Image.LANCZOS)
            self._prev_tk = ImageTk.PhotoImage(thumb)
            self.preview_lbl.config(image=self._prev_tk)
        except Exception:
            pass

    def _on_prev_resize(self, event):
        if self._resize_after:
            try: self.after_cancel(self._resize_after)
            except Exception: pass
        self._resize_after = self.after(120, self._update_preview)

    def _on_aspect_pick(self, _=None):
        label = self.aspect_cb.get()
        ch = next((c for c in self._choices if c[0] == label), None)
        if ch: self._select_aspect(ch[1])

    def _select_aspect(self, akey):
        """Switch the crop target to an aspect; load its saved crop if one exists, else reset."""
        ch = next((c for c in self._choices if c[1] == akey), self._choices[0])
        self.cur_akey, self.cur_wh = ch[1], ch[2]
        self.aspect_cb.set(ch[0]); self.title(f"Crop for {ch[0]}")
        saved = self.saved_crops.get(self.cur_akey)
        if saved:
            W, H = self.src.size; b = saved["box"]
            self.box = [b[0]*W*self.scale, b[1]*H*self.scale, b[2]*W*self.scale, b[3]*H*self.scale]
            bh = (b[3]-b[1])*H
            self.aspect = ((b[2]-b[0])*W / bh) if bh else self.cur_wh[0]/self.cur_wh[1]
            self.optimise.set(bool(saved.get("optimise", True)))
        else:
            self.aspect = self.cur_wh[0] / self.cur_wh[1]
            self._reset_box()
        self.orient_lbl.config(text="landscape" if self.aspect >= 1 else "portrait")
        self.redraw(); self._update_preview()

    def _corner(self, x, y):
        h = self.HANDLE + 4
        for idx, (cx, cy) in enumerate([(self.box[0],self.box[1]),(self.box[2],self.box[1]),
                                        (self.box[0],self.box[3]),(self.box[2],self.box[3])]):
            if abs(x-cx) <= h and abs(y-cy) <= h: return idx
        return None

    def on_press(self, e):
        corner = self._corner(e.x, e.y)
        if corner is not None:
            self.drag_mode = ("resize", corner)
        elif self.box[0] <= e.x <= self.box[2] and self.box[1] <= e.y <= self.box[3]:
            self.drag_mode = ("move", (e.x-self.box[0], e.y-self.box[1]))
        else:
            self.drag_mode = None
        self.last = (e.x, e.y)

    def on_drag(self, e):
        if not self.drag_mode: return
        kind, info = self.drag_mode
        x = max(0, min(self.dw, e.x)); y = max(0, min(self.dh, e.y))
        if kind == "move":
            offx, offy = info; bw = self.box[2]-self.box[0]; bh = self.box[3]-self.box[1]
            nx0 = max(0, min(self.dw-bw, x-offx)); ny0 = max(0, min(self.dh-bh, y-offy))
            self.box = [nx0, ny0, nx0+bw, ny0+bh]
        else:
            # resize: opposite corner stays fixed, keep aspect
            ax = self.box[2] if info in (0,2) else self.box[0]
            ay = self.box[3] if info in (0,1) else self.box[1]
            w = abs(x-ax); h = w/self.aspect
            if h > abs(y-ay): h = abs(y-ay); w = h*self.aspect
            if w < 40: w, h = 40, 40/self.aspect
            nx0, nx1 = (ax-w, ax) if info in (0,2) else (ax, ax+w)
            ny0, ny1 = (ay-h, ay) if info in (0,1) else (ay, ay+h)
            if nx0 < 0 or ny0 < 0 or nx1 > self.dw or ny1 > self.dh: return
            self.box = [nx0, ny0, nx1, ny1]
        self.redraw()

    def confirm(self):
        s = self.scale; W, H = self.src.size
        nbox = [(self.box[0]/s)/W, (self.box[1]/s)/H, (self.box[2]/s)/W, (self.box[3]/s)/H]
        self.destroy()
        self.on_done(self.name, self.src, self.cur_akey, nbox,
                     self.optimise.get(), self.keep_orig.get())

class WifiDialog(tk.Toplevel):
    def __init__(self, master, on_send):
        super().__init__(master)
        self.title("Configure Wi-Fi over USB")
        self.transient(master); self.grab_set(); self.on_send = on_send
        f = ttk.Frame(self, padding=16); f.pack(fill="both", expand=True)
        ttk.Label(f, text="Send Wi-Fi credentials to the frame over the USB cable.\n"
                          "Only 2.4 GHz networks are supported.",
                  justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0,12))
        ttk.Label(f, text="Network (SSID)").grid(row=1, column=0, sticky="w", pady=3)
        self.e_ssid = ttk.Entry(f, width=34); self.e_ssid.grid(row=1, column=1, pady=3)
        ttk.Label(f, text="Password").grid(row=2, column=0, sticky="w", pady=3)
        self.e_pw = ttk.Entry(f, width=34, show="•"); self.e_pw.grid(row=2, column=1, pady=3)
        self.show = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="show password", variable=self.show,
                        command=lambda: self.e_pw.config(show="" if self.show.get() else "•")
                        ).grid(row=3, column=1, sticky="w")
        bar = ttk.Frame(f); bar.grid(row=4, column=0, columnspan=2, sticky="e", pady=(14,0))
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Send to frame", command=self._go).pack(side="right", padx=6)
        self.e_ssid.focus_set()

    def _go(self):
        ssid = self.e_ssid.get().strip(); pw = self.e_pw.get()
        if not ssid:
            messagebox.showwarning("Wi-Fi", "Enter a network name (SSID)."); return
        self.destroy(); self.on_send(ssid, pw, "")

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("USBme Photo Cast — © 2026 ionblue88 · AGPL-3.0")
        self.geometry("780x640"); self.minsize(720, 480)   # >= 3 thumbnails wide
        self.q = queue.Queue()
        self.serial_lock = threading.Lock()
        self.sending = False
        self._settings = load_settings()
        self._auto_enabled = bool(self._settings.get("auto_send", False))   # thread-safe copy for the poller
        self.auto_send = tk.BooleanVar(value=self._auto_enabled)
        self.auto_send.trace_add("write", lambda *a: self._save_auto())
        self.prev_present = False
        self._auto_fired = False      # auto-send fires once per connection, only once the frame is awake
        self._frame_ready = False     # frame connected AND awake (SN answering) — gates Send / Wi-Fi
        self._send_btns = []
        self.frame_info = {}
        self.thumb_refs = {}
        self._auto = None; self._auto_after = None

        top = ttk.Frame(self, padding=10); top.pack(fill="x")
        self.status_dot = ttk.Label(top, text="●", font=("Segoe UI", 12, "bold"), foreground="#9aa0a6")
        self.status_dot.pack(side="left")
        self.status = ttk.Label(top, text="Looking for frame…", font=("Segoe UI", 10, "bold"))
        self.status.pack(side="left", padx=(6,0))
        ttk.Checkbutton(top, text="Auto-send random photo when frame connects",
                        variable=self.auto_send).pack(side="right")

        self.auto_bar = ttk.Frame(self); self.auto_bar.pack(fill="x", padx=10)
        self.auto_lbl = ttk.Label(self.auto_bar, text="", foreground="#c0392b",
                                  font=("Segoe UI", 10, "bold")); self.auto_lbl.pack(side="left", pady=2)
        self.auto_stop = ttk.Button(self.auto_bar, text="Stop auto-send", command=self.cancel_auto)

        tools = ttk.Frame(self, padding=(10,0)); tools.pack(fill="x")
        ttk.Button(tools, text="➕  Add photo…", command=self.add_photo).pack(side="left")
        self.wifi_btn = ttk.Button(tools, text="📶  Configure Wi-Fi…", command=self.open_wifi)
        self.wifi_btn.pack(side="left", padx=(6,0))
        ttk.Label(tools, text="   Photo pool:").pack(side="left", padx=(12,0))
        self.count_lbl = ttk.Label(tools, text=""); self.count_lbl.pack(side="left")
        ttk.Button(tools, text="About", command=self.show_about).pack(side="right")

        # scrollable pool
        mid = ttk.Frame(self, padding=10); mid.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(mid, highlightthickness=0)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.pool_frame = ttk.Frame(self.canvas)
        self.pool_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._pool_window = self.canvas.create_window((0,0), window=self.pool_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.bind_all("<MouseWheel>", self._on_wheel)     # scroll the pool with the wheel

        bottom = ttk.Frame(self, padding=10); bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=1.0)
        self.progress.pack(fill="x")
        info_row = ttk.Frame(bottom); info_row.pack(fill="x", pady=(4,0))
        self.log_lbl = ttk.Label(info_row, text="Ready.", foreground="#666")
        self.log_lbl.pack(side="left")
        det = ttk.Frame(info_row); det.pack(side="right")
        self.sn_lbl = ttk.Label(det, text="", foreground="#888", font=("Consolas", 9))
        self.sn_lbl.pack(side="left", padx=(0,10))
        self.batt_canvas = tk.Canvas(det, width=40, height=16, highlightthickness=0)
        self.batt_canvas.pack(side="left")

        try:
            migrate_pool()                    # one-time upgrade of legacy pool items
        except Exception as e:
            print("pool migration:", e)
        try:
            changed = revalidate_all()        # reset crops for any source edited on disk
        except Exception as e:
            changed = []; print("revalidate:", e)
        self.refresh_pool()
        if changed:
            self.log(f"{len(changed)} photo(s) changed on disk — re-crop them.")
        threading.Thread(target=self._poll_connection, daemon=True).start()
        self.after(100, self._drain_queue)

    # ---- thread-safe UI plumbing ----
    def _post(self, fn): self.q.put(fn)
    def _drain_queue(self):
        try:
            while True: self.q.get_nowait()()
        except queue.Empty: pass
        self.after(100, self._drain_queue)
    def log(self, msg): self._post(lambda: self.log_lbl.config(text=msg))
    def set_progress(self, v): self._post(lambda: self.progress.config(value=v))
    def _on_wheel(self, e):
        try: self.canvas.yview_scroll(int(-e.delta/120), "units")
        except Exception: pass
    def _save_auto(self):
        self._auto_enabled = self.auto_send.get()
        self._settings["auto_send"] = self._auto_enabled; save_settings(self._settings)

    # ---- pool UI ----
    def refresh_pool(self):
        for w in self.pool_frame.winfo_children(): w.destroy()
        self.thumb_refs.clear()
        self._send_btns = []
        items = load_pool()
        self.count_lbl.config(text=f"{len(items)} photo(s)")
        if not items:
            ttk.Label(self.pool_frame, text="No photos yet. Click “Add photo…” to start.",
                      foreground="#888").grid(row=0, column=0, padx=8, pady=20)
            return
        self.pool_cells = []
        for it in items:
            cell = ttk.Frame(self.pool_frame, relief="solid", borderwidth=1, padding=6)
            png = os.path.join(POOL_DIR, it["id"] + ".png")
            holder = ttk.Frame(cell, width=200, height=152); holder.pack(); holder.pack_propagate(False)
            if not it.get("crops"):
                ttk.Label(holder, text="⚠ photo changed\nre-crop it\n(Send, or ⋯ → Crop…)",
                          foreground="#c0392b", justify="center").place(relx=0.5, rely=0.5, anchor="center")
            else:
                try:
                    im = Image.open(png)
                    sc = min(196/im.width, 150/im.height, 1.0)
                    thumb = im.resize((max(1,int(im.width*sc)), max(1,int(im.height*sc))), Image.LANCZOS)
                    ph = ImageTk.PhotoImage(thumb); self.thumb_refs[it["id"]] = ph
                    ttk.Label(holder, image=ph).place(relx=0.5, rely=0.5, anchor="center")
                except Exception:
                    ttk.Label(holder, text="(preview missing)").place(relx=0.5, rely=0.5, anchor="center")
            ttk.Label(cell, text=it["name"][:26], font=("Segoe UI", 8)).pack(pady=(4,2))
            row = ttk.Frame(cell); row.pack()
            send_btn = ttk.Button(row, text="Send", width=6,
                                  command=lambda pid=it["id"]: self.send_pool_item(pid))
            send_btn.pack(side="left", padx=1)
            send_btn.configure(state=("normal" if self._frame_ready else "disabled"))
            self._send_btns.append(send_btn)
            more = ttk.Button(row, text="⋯", width=2)
            more.configure(command=lambda pid=it["id"], nm=it["name"], w=more: self.show_item_menu(pid, nm, w))
            more.pack(side="left", padx=1)
            self.pool_cells.append(cell)
        self._relayout()

    def _relayout(self, event=None):
        cells = getattr(self, "pool_cells", None)
        if not cells: return
        cols = max(3, (self.canvas.winfo_width() - 12) // 224)   # >=3 tiles, more on wider windows
        for i, cell in enumerate(cells):
            cell.grid(row=i // cols, column=i % cols, padx=6, pady=6, sticky="n")

    def _on_canvas_resize(self, e):
        self.canvas.itemconfig(self._pool_window, width=e.width)
        if getattr(self, "_relayout_after", None):
            try: self.after_cancel(self._relayout_after)
            except Exception: pass
        self._relayout_after = self.after(60, self._relayout)

    def delete_item(self, pid, name):
        if messagebox.askyesno("Delete", f"Remove “{name}” from the pool?"):
            delete_from_pool(pid); self.refresh_pool()

    def crop_existing(self, pid, on_saved=None, lock=False):
        if revalidate_source(pid):
            self.refresh_pool(); self.log("Photo changed on disk — starting a fresh crop.")
        item = next((it for it in load_pool() if it["id"] == pid), None)
        if not item: return
        try:
            img = Image.open(_src_path(pid)).convert("RGB")
        except Exception as e:
            self.log(f"can't open source: {e}"); return
        def done(name, src_img, akey, nbox, optimise, keep_original):
            set_crop(pid, akey, nbox, optimise)
            self._post(self.refresh_pool)
            self.log(f"Saved {akey} crop for “{item['name']}”.")
            if on_saved: self._post(on_saved)
        self._crop = CropDialog(self, img, item["name"], done, mode="edit",
                                crops=item.get("crops", {}), start_aspect=panel_aspect_key(),
                                lock_aspect=(panel_aspect_key() if lock else None))

    def show_item_menu(self, pid, name, widget):
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Crop…", command=lambda: self.crop_existing(pid))
        m.add_separator()
        m.add_command(label="Delete image", command=lambda: self.delete_item(pid, name))
        try:
            m.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
        finally:
            m.grab_release()

    # ---- add photo ----
    def add_photo(self):
        path = filedialog.askopenfilename(
            title="Choose a photo",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.webp *.gif *.heic *.tif *.tiff"),
                       ("All files", "*.*")])
        if not path: return
        try:
            img = Image.open(path)
            img.load()
            img = ImageOps.exif_transpose(img).convert("RGB")   # honor phone rotation
        except Exception as e:
            messagebox.showerror(
                "Can't open this image",
                f"Couldn't read:\n{os.path.basename(path)}\n\n{type(e).__name__}: {e}\n\n"
                "Save the picture as a JPG or PNG first, then add it.")
            return
        self._crop = CropDialog(self, img, os.path.basename(path), self._crop_done,
                                mode="add", start_aspect=panel_aspect_key())

    def _crop_done(self, name, src_img, akey, nbox, optimise, keep_original):
        self.log("Adding…")
        def work():
            try:
                add_to_pool(name, src_img, akey, nbox, optimise, keep_original)
                self._post(self.refresh_pool); self.log(f"Added “{name}” to the pool.")
            except Exception as e:
                self.log(f"add failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    # ---- sending ----
    def send_pool_item(self, pid):
        port = find_frame_port()
        if not port:
            messagebox.showwarning("No frame", "Frame not detected. Plug in the USB cable and wake the frame."); return
        if revalidate_source(pid):
            self.refresh_pool(); self.log("This photo changed on disk — crop it again.")
        item = next((it for it in load_pool() if it["id"] == pid), None)
        if not item: return
        if panel_aspect_key() not in item.get("crops", {}):     # not cropped for this frame yet
            self.log("This photo isn't cropped for the connected frame — crop it now.")
            self.crop_existing(pid, on_saved=lambda: self.send_pool_item(pid), lock=True)
            return
        try:
            data = bin_for_pool_item(pid)      # encode this frame's crop at its resolution
        except Exception as e:
            self.log(f"encode failed: {e}"); return
        if not data:
            self.log("No crop for this frame."); return
        self._start_send(port, data, item["name"])

    def _start_send(self, port, data, label):
        if self.sending:
            self.log("busy sending…"); return
        self.sending = True; self.set_progress(0)
        def work():
            with self.serial_lock:
                try:
                    ok = send_bin(port, data, on_progress=self.set_progress, on_log=self.log)
                    self.log(("Sent " if ok else "Failed: ") + label)
                except Exception as e:
                    self.log(f"send error: {e}")
                finally:
                    self.set_progress(0); self.sending = False
        threading.Thread(target=work, daemon=True).start()

    # ---- auto-send countdown ----
    def start_auto_countdown(self, pid, name, port, seconds=5):
        if self.sending or self._auto: return
        self._auto = {"pid": pid, "name": name, "port": port, "left": seconds}
        self.auto_stop.pack(side="left", padx=10)
        self._auto_tick()

    def _auto_tick(self):
        a = self._auto
        if not a: return
        if find_frame_port() != a["port"]:
            self._clear_auto("Auto-send cancelled (frame unplugged)."); return
        if a["left"] <= 0:
            pid, name, port = a["pid"], a["name"], a["port"]; self._clear_auto(None)
            data = bin_for_pool_item(pid)
            if not data:
                self.log(f"Auto-send skipped — “{name}” isn't cropped for this frame."); return
            try:
                self._start_send(port, data, "(auto) " + name)
            except Exception as e:
                self.log(f"auto-send failed: {e}")
            return
        self.auto_lbl.config(text=f"Auto-sending “{a['name']}” in {a['left']}s…")
        a["left"] -= 1
        self._auto_after = self.after(1000, self._auto_tick)

    def cancel_auto(self):
        if self._auto: self._clear_auto("Auto-send stopped.")

    def _clear_auto(self, msg):
        if self._auto_after:
            try: self.after_cancel(self._auto_after)
            except Exception: pass
            self._auto_after = None
        self._auto = None
        self.auto_lbl.config(text=""); self.auto_stop.pack_forget()
        if msg: self.log(msg)

    # ---- wifi config ----
    def show_about(self):
        win = tk.Toplevel(self)
        win.title("About USBme Photo Cast")
        win.resizable(False, False)
        win.transient(self)
        frm = ttk.Frame(win, padding=20); frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="USBme Photo Cast", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        body = (
            "Cast photos to a 6-colour e-ink frame over USB.\n\n"
            "Copyright © 2026 ionblue88\n"
            "https://github.com/ionblue88/usbme-photo-cast\n\n"
            "This program comes with ABSOLUTELY NO WARRANTY. It is free software: "
            "you may redistribute it under the terms of the GNU Affero General "
            "Public License v3.0 (AGPL-3.0).\n\n"
            "See the LICENSE file, or https://www.gnu.org/licenses/agpl-3.0.html"
        )
        ttk.Label(frm, text=body, wraplength=390, justify="left").pack(anchor="w", pady=(6, 14), fill="x")
        ttk.Button(frm, text="OK", command=win.destroy).pack(anchor="e")
        win.bind("<Return>", lambda e: win.destroy())
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + max(0, (self.winfo_height() - win.winfo_height()) // 3)
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.grab_set(); win.focus_set()

    def open_wifi(self):
        port = find_frame_port()
        if not port:
            messagebox.showwarning("No frame", "Plug in the frame over USB first (and wake it)."); return
        WifiDialog(self, lambda ssid, pw, phone: self._send_wifi(port, ssid, pw, phone))

    def _send_wifi(self, port, ssid, pw, phone):
        if self.sending:
            self.log("busy…"); return
        self.sending = True
        def work():
            with self.serial_lock:
                try:
                    ok, resp = send_wifi(port, ssid, pw, phone)
                    ack = "".join(c for c in resp if c.isalnum() or c == "_")   # strip framing
                    if "OK" in ack.upper():
                        self.log(f"Wi-Fi accepted by frame ✓  (“{ssid}”, reply {ack})")
                    else:
                        self.log(f"Wi-Fi sent to “{ssid}” — reply: {ack or 'none'}")
                except Exception as e:
                    self.log(f"wifi error: {e}")
                finally:
                    self.sending = False
        threading.Thread(target=work, daemon=True).start()

    # ---- connection poller + auto-send ----
    def _update_status(self, port, info):
        ready = bool(port and info.get("sn"))
        if ready:
            self.status_dot.config(foreground="#22a447")   # green = awake / ready
            self.status.config(text=f"{model_name(info.get('sn'))} connected")
        elif port:
            self.status_dot.config(foreground="#e0902a")   # amber = cable in, but frame asleep
            self.status.config(text="Frame asleep — press its power button")
        else:
            self.status_dot.config(foreground="#9aa0a6")   # grey = not connected
            self.status.config(text="Not connected")
        self._update_details(info if port else {})
        self._set_frame_controls(ready)

    def _set_frame_controls(self, ready):
        """Send / Configure Wi-Fi only work on an awake frame — grey them out otherwise."""
        self._frame_ready = ready
        state = "normal" if ready else "disabled"
        try: self.wifi_btn.configure(state=state)
        except Exception: pass
        for b in self._send_btns:
            try: b.configure(state=state)
            except Exception: pass

    def _update_details(self, info):
        sn = info.get("sn", "")
        fw = info.get("fw", "")
        self.sn_lbl.config(text=(f"SN {sn}" + (f"  ·  fw {fw}" if fw else "")) if sn else "")
        self._draw_battery(info.get("mv"))

    def _draw_battery(self, mv):
        c = self.batt_canvas; c.delete("all")
        if not mv: return
        lvl = battery_level(mv)                                       # 0..4
        c.create_rectangle(2, 3, 30, 14, outline="#888", width=1)     # body
        c.create_rectangle(30, 6, 33, 11, fill="#888", outline="")    # terminal
        col = "#2e9e4f" if lvl >= 2 else ("#e0a800" if lvl == 1 else "#d0392b")
        if lvl > 0:
            c.create_rectangle(4, 5, 4 + 24 * (lvl / 4.0), 12, fill=col, outline="")

    def _poll_connection(self):
        last_try = 0.0; miss = 0
        while True:
            port = find_frame_port(); present = port is not None
            have = bool(self.frame_info.get("sn"))               # we currently believe it's awake
            now = time.monotonic()
            # Query on connect, retry every 5s while asleep, and re-verify every 6s while awake —
            # so a frame that sleeps *after* connecting is noticed, not shown as connected forever.
            due = present and not self.sending and (
                not self.prev_present or now - last_try > (6 if have else 5))
            if due:
                last_try = now
                with self.serial_lock:
                    inf = query_frame_info(port)               # read-only
                if inf.get("sn"):
                    self.frame_info = inf; miss = 0
                    set_panel(model_name(inf["sn"]))
                elif have:
                    miss += 1
                    if miss >= 2:                              # two misses running → it fell asleep
                        self.frame_info = {}; miss = 0
            elif not present:
                self.frame_info = {}; miss = 0
                self._auto_fired = False      # re-arm auto-send for the next connection
            self._post(lambda p=port, inf=dict(self.frame_info): self._update_status(p, inf))
            # Auto-send only once the frame is actually AWAKE (answering over serial, SN read).
            # A sleeping frame (cable in, no SN) must not trigger a send that can only fail.
            awake = present and bool(self.frame_info.get("sn"))
            if awake and self._auto_enabled and not self._auto_fired and not self.sending and not self._auto:
                items = [x for x in load_pool() if crop_for_panel(x)]   # only croppable for this frame
                if items:
                    it = random.choice(items)
                    self._auto_fired = True
                    self._post(lambda p=port, pid=it["id"], n=it["name"]:
                               self.start_auto_countdown(pid, n, p))   # 5s countdown + Stop button
            self.prev_present = present
            time.sleep(2.0)

if __name__ == "__main__":
    App().mainloop()
