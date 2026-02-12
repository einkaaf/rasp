#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import random
import threading
import math
from pathlib import Path

import cv2
from flask import Flask, Response, redirect, request, url_for, jsonify

import Raspbot_Lib

HOST = "0.0.0.0"
PORT = 5000

SAVE_DIR = Path("/picturesbot")
SAVE_DIR.mkdir(parents=True, exist_ok=True)

CAM_INDEX = 0
NUM_LEDS = 14
DEFAULT_SPEED = 120

PAN_MIN, PAN_MAX = 20, 160
TILT_MIN, TILT_MAX = 50, 110

WARMUP_GRABS = 25
FLUSH_GRABS_BEFORE_SHOT = 12
GRAB_SLEEP = 0.01
JPEG_QUALITY = 80
FPS_LIMIT = 20

LED_PRESETS = {
    "Red": 0, "Green": 1, "Blue": 2, "Yellow": 3,
    "Purple": 4, "Cyan": 5, "White": 6,
}
LIGHT_EFFECTS = ["river", "breathing", "gradient", "random_running", "starlight"]

app = Flask(__name__)
bot = Raspbot_Lib.Raspbot()

hw_lock = threading.Lock()
run_lock = threading.Lock()

state = {
    "speed": DEFAULT_SPEED,
    "pan": 90,
    "tilt": 90,
    "ultra_on": 0,
    "ir_on": 0,
    "last_distance_mm": None,
    "last_ir_byte": None,
    "status": "آماده",
    "last_led_color": None,      # stores the last preset index (0-6) or None
}

cap = None
cap_last_open_fail = 0.0

lightshow = None
lightshow_thread = None


# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def clamp_int(val, lo, hi, default):
    try:
        v = int(val)
    except Exception:
        return default
    return max(lo, min(hi, v))

def clamp_float(val, lo, hi, default):
    try:
        v = float(val)
    except Exception:
        return default
    return max(lo, min(hi, v))

def set_status(msg: str):
    state["status"] = msg

def stop_all_motors():
    for mid in (0, 1, 2, 3):
        bot.Ctrl_Muto(mid, 0)

def drive_all(speed_signed: int):
    for mid in (0, 1, 2, 3):
        bot.Ctrl_Muto(mid, speed_signed)

def spin_left(speed: int):
    for mid in (0, 1):
        bot.Ctrl_Muto(mid, -speed)
    for mid in (2, 3):
        bot.Ctrl_Muto(mid, speed)

def spin_right(speed: int):
    for mid in (0, 1):
        bot.Ctrl_Muto(mid, speed)
    for mid in (2, 3):
        bot.Ctrl_Muto(mid, -speed)

def safe_servo(id_: int, angle: int):
    if id_ == 1:
        angle = clamp_int(angle, PAN_MIN, PAN_MAX, 90)
        state["pan"] = angle
    else:
        angle = clamp_int(angle, TILT_MIN, TILT_MAX, 90)
        state["tilt"] = angle
    bot.Ctrl_Servo(id_, angle)
    return angle

def read_ultrasonic_mm():
    high = bot.read_data_array(0x1B, 1)[0]
    low = bot.read_data_array(0x1A, 1)[0]
    return (high << 8) | low

def read_ir_byte():
    data = bot.read_data_array(0x0C, 1)
    return int(data[0]) if data and len(data) else None

def _open_camera_locked():
    global cap, cap_last_open_fail
    now = time.time()
    if cap is not None:
        return True
    if now - cap_last_open_fail < 2.0:
        return False
    c = cv2.VideoCapture(CAM_INDEX, cv2.CAP_V4L2)
    if not c.isOpened():
        cap_last_open_fail = now
        return False
    try:
        c.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    for _ in range(WARMUP_GRABS):
        c.grab()
        time.sleep(GRAB_SLEEP)
    cap = c
    return True

def _get_fresh_frame_locked():
    if cap is None:
        return None
    for _ in range(FLUSH_GRABS_BEFORE_SHOT):
        cap.grab()
        time.sleep(GRAB_SLEEP)
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    return frame

def capture_picture(tag="manual"):
    with hw_lock:
        if not _open_camera_locked():
            return None
        frame = _get_fresh_frame_locked()
        if frame is None:
            return None
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"img_{ts}_{tag}_pan{state['pan']}_tilt{state['tilt']}.jpg"
        path = SAVE_DIR / fname
        cv2.putText(frame, ts, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (255, 255, 255), 2, cv2.LINE_AA)
        cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        return path

def mjpeg_generator():
    delay = (1.0 / FPS_LIMIT) if FPS_LIMIT else 0.0
    while True:
        with hw_lock:
            ok = _open_camera_locked()
            frame = _get_fresh_frame_locked() if ok else None
        if frame is None:
            msg = b"Camera not available. Check /dev/video* or CAM_INDEX."
            yield (b"--frame\r\nContent-Type: text/plain\r\n\r\n" + msg + b"\r\n")
            time.sleep(0.5)
            continue
        ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ok:
            time.sleep(0.05)
            continue
        data = jpg.tobytes()
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n"
               b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" +
               data + b"\r\n")
        if delay:
            time.sleep(delay)

def list_images():
    exts = {".jpg", ".jpeg", ".png"}
    imgs = [p for p in SAVE_DIR.iterdir() if p.is_file() and p.suffix.lower() in exts]
    imgs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return imgs

def stop_lightshow():
    global lightshow, lightshow_thread
    if lightshow is not None:
        try:
            lightshow.stop()
        except Exception:
            pass
    lightshow = None
    lightshow_thread = None

def run_lightshow(effect: str, duration: float, speed: float, color_code: int):
    global lightshow
    try:
        ls = Raspbot_Lib.LightShow()
        lightshow = ls
        ls.execute_effect(effect, duration, speed, color_code)
    finally:
        try:
            if lightshow is not None:
                lightshow.turn_off_all_lights()
        except Exception:
            pass


# ----------------------------------------------------------------------
# ULTRA MODERN UI – RTL, Persian, dual joysticks, no‑scroll
# ----------------------------------------------------------------------
def page_html():
    speed = state["speed"]
    pan = state["pan"]
    tilt = state["tilt"]
    ultra_on = state["ultra_on"]
    ir_on = state["ir_on"]
    dist = state["last_distance_mm"]
    irb = state["last_ir_byte"]
    status = state["status"]
    dist_text = f"{dist} mm" if dist is not None else "—"
    ir_text = f"{irb}" if irb is not None else "—"

    # LED preset buttons with active class
    presets_html = ""
    for name, code in LED_PRESETS.items():
        active_class = " active" if state["last_led_color"] == code else ""
        persian_name = {
            "Red": "قرمز",
            "Green": "سبز",
            "Blue": "آبی",
            "Yellow": "زرد",
            "Purple": "بنفش",
            "Cyan": "فیروزه‌ای",
            "White": "سفید",
        }.get(name, name)
        presets_html += f"""
        <form action="/api/led/preset" method="post" style="display:inline;">
          <input type="hidden" name="color" value="{code}">
          <button type="submit" class="btn-preset{active_class}">{persian_name}</button>
        </form>
        """

    effects_html = "".join([f"""
      <form action="/api/light/effect" method="post" style="display:inline;">
        <input type="hidden" name="name" value="{e}">
        <button type="submit" class="btn-effect">{e}</button>
      </form>
    """ for e in LIGHT_EFFECTS])

    return f"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=yes">
  <title>راسپبات · کنترل مینیمال</title>
  <!-- Persian font: Vazir -->
  <link href="https://cdn.fontcdn.ir/Font/Persian/Vazir/Vazir.css" rel="stylesheet" type="text/css"/>
  <style>
    @font-face {{
      font-family: 'Vazir';
      src: url('https://cdn.fontcdn.ir/Font/Persian/Vazir/Vazir.eot');
      src: url('https://cdn.fontcdn.ir/Font/Persian/Vazir/Vazir.eot?#iefix') format('embedded-opentype'),
           url('https://cdn.fontcdn.ir/Font/Persian/Vazir/Vazir.woff2') format('woff2'),
           url('https://cdn.fontcdn.ir/Font/Persian/Vazir/Vazir.woff') format('woff'),
           url('https://cdn.fontcdn.ir/Font/Persian/Vazir/Vazir.ttf') format('truetype');
      font-weight: normal;
      font-style: normal;
    }}
    /* ----- RTL CSS variables (light/dark) ----- */
    :root {{
      --bg: #f9fafc;
      --card: white;
      --border: #e6edf4;
      --text: #1e2f4e;
      --text-light: #5a6f85;
      --accent: #3b82f6;
      --accent-soft: #dbeafe;
      --shadow: 0 8px 20px rgba(0,0,0,0.02), 0 2px 6px rgba(0,20,40,0.02);
      --radius: 24px;
      --radius-sm: 16px;
      --footer-bg: #eef2f6;
    }}
    [data-theme="dark"] {{
      --bg: #0b1a2a;
      --card: #132433;
      --border: #1f3a4c;
      --text: #e1e9f0;
      --text-light: #9aaebf;
      --accent: #60a5fa;
      --accent-soft: #1e3a5a;
      --shadow: 0 8px 20px rgba(0,0,0,0.4);
      --footer-bg: #0e1e2c;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: 'Vazir', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
      line-height: 1.5;
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden; /* NO PAGE SCROLL */
    }}
    .app-container {{
      display: flex;
      flex-direction: column;
      height: 100vh;
      max-width: 1600px;
      margin: 0 auto;
      width: 100%;
      padding: 16px 20px 0 20px;
    }}
    /* fixed header – RTL */
    .status-bar {{
      background: var(--card);
      border-radius: var(--radius);
      padding: 14px 20px;
      margin-bottom: 16px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      box-shadow: var(--shadow);
      border: 1px solid var(--border);
      font-size: 0.9rem;
      flex-shrink: 0;
    }}
    /* scrollable grid area (only this scrolls) */
    .grid-wrapper {{
      flex: 1 1 auto;
      overflow-y: auto;
      padding-left: 4px; /* RTL */
      margin-bottom: 16px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
      gap: 20px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 20px;
      box-shadow: var(--shadow);
      transition: all 0.1s ease;
      display: flex;
      flex-direction: column;
    }}
    .card:hover {{
      border-color: var(--accent);
    }}
    h2 {{
      font-size: 1.1rem;
      font-weight: 600;
      letter-spacing: -0.01em;
      margin: 0 0 14px 0;
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--text);
    }}
    .row {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      margin-bottom: 8px;
    }}
    button, .btn {{
      background: transparent;
      border: 1px solid var(--border);
      padding: 8px 14px;
      border-radius: 40px;
      font-size: 0.9rem;
      font-weight: 450;
      color: var(--text);
      cursor: pointer;
      transition: all 0.15s;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      background: var(--card);
      font-family: inherit;
    }}
    button:hover {{
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }}
    .btn-preset.active {{
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }}
    .btn-danger {{
      background: #fff2f0;
      border-color: #ffccc7;
      color: #c73e3e;
    }}
    [data-theme="dark"] .btn-danger {{
      background: #3f1e1e;
      border-color: #8f4b4b;
      color: #ffb3b3;
    }}
    .btn-danger:hover {{
      background: #c73e3e;
      border-color: #c73e3e;
      color: white;
    }}
    .btn-success {{
      background: #e6f7e6;
      border-color: #b7eb8f;
      color: #2c6b2c;
    }}
    [data-theme="dark"] .btn-success {{
      background: #1e3a2a;
      border-color: #3b6e4a;
      color: #b0e5b0;
    }}
    .btn-success:hover {{
      background: #2c6b2c;
      border-color: #2c6b2c;
      color: white;
    }}
    .cam-placeholder, .cam-live {{
      width: 100%;
      border-radius: var(--radius-sm);
      background: var(--border);
      aspect-ratio: 16/9;
      object-fit: cover;
      border: 1px solid var(--border);
      margin-bottom: 12px;
    }}
    .cam-placeholder {{
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--text-light);
      font-size: 0.9rem;
    }}
    input[type=range] {{
      flex: 1;
      min-width: 160px;
      height: 6px;
      border-radius: 10px;
      background: var(--border);
      -webkit-appearance: none;
    }}
    input[type=range]::-webkit-slider-thumb {{
      -webkit-appearance: none;
      width: 18px;
      height: 18px;
      background: var(--accent);
      border-radius: 50%;
      box-shadow: 0 2px 8px rgba(59,130,246,0.3);
      cursor: pointer;
      border: 2px solid white;
    }}
    input, select {{
      background: var(--card);
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 30px;
      padding: 6px 12px;
      font-family: inherit;
    }}
    .joywrap {{
      display: flex;
      gap: 16px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .joy {{
      width: 200px;
      height: 200px;
      border-radius: 999px;
      background: radial-gradient(circle at 30% 30%, var(--card), var(--bg));
      border: 1px solid var(--border);
      position: relative;
      touch-action: none;
      box-shadow: var(--shadow);
    }}
    .joy .knob {{
      width: 64px;
      height: 64px;
      border-radius: 999px;
      background: var(--card);
      border: 1px solid var(--border);
      position: absolute;
      left: 50%;
      top: 50%;
      transform: translate(-50%, -50%);
      box-shadow: 0 6px 14px rgba(0,0,0,0.06);
    }}
    .sensor-value {{
      font-size: 1.5rem;
      font-weight: 600;
      line-height: 1.2;
    }}
    .hint {{
      color: var(--text-light);
      font-size: 0.8rem;
      margin-top: 6px;
    }}
    hr {{
      margin: 16px 0;
      border: none;
      border-top: 1px solid var(--border);
    }}
    .flex-between {{
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .led-grid {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }}
    /* footer */
    .footer {{
      flex-shrink: 0;
      text-align: center;
      padding: 14px 0;
      color: var(--text-light);
      border-top: 1px solid var(--border);
      background: var(--footer-bg);
      margin: 0 -20px;
      font-size: 0.85rem;
      letter-spacing: 0.5px;
    }}
    .settings-row {{
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .poll-input {{
      width: 70px;
      text-align: center;
    }}
  </style>
</head>
<body>
  <div class="app-container">
    <!-- Status header – RTL -->
    <div class="status-bar">
      <div class="status-item"><span>⚙️</span> <strong>{status}</strong></div>
      <div style="display: flex; gap: 16px; flex-wrap: wrap;">
        <div class="status-item"><span>📷</span> دوربین <span class="badge" id="cam-badge">خاموش</span></div>
        <div class="status-item"><span>📡</span> فراصوت <span class="badge" id="ultra-badge">{'روشن' if ultra_on else 'خاموش'}</span></div>
        <div class="status-item"><span>🎛️</span> IR <span class="badge">{'روشن' if ir_on else 'خاموش'}</span></div>
        <button id="theme-toggle" style="padding: 4px 12px;">🌙 تاریک</button>
      </div>
    </div>

    <!-- Hidden sensor state for JS -->
    <div id="sensor-state" data-ultra="{ultra_on}" style="display:none;"></div>

    <!-- Scrollable content grid -->
    <div class="grid-wrapper">
      <div class="grid">

        <!-- Camera card -->
        <div class="card">
          <h2>📸 دوربین</h2>
          <div id="camera-container">
            <img id="stream-img" class="cam-placeholder" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='225' viewBox='0 0 400 225'%3E%3Crect width='400' height='225' fill='%23eef2f6'/%3E%3Ctext x='50%25' y='50%25' font-size='14' font-family='system-ui, sans-serif' fill='%235a6f85' text-anchor='middle' dy='.3em'%3EStream stopped%3C/text%3E%3C/svg%3E" alt="camera stream">
          </div>
          <div class="row flex-between">
            <div>
              <form action="/api/camera/snap" method="post" style="display:inline;">
                <button type="submit">📸 عکس</button>
              </form>
              <a href="/gallery" style="text-decoration:none;"><button type="button">🖼 گالری</button></a>
            </div>
            <button id="toggle-stream" class="btn-success">▶ شروع ویدیو</button>
          </div>
          <div class="hint">استریم خاموش – برای کاهش بار روشن کنید</div>
        </div>

        <!-- Motor control joystick (NEW) -->
        <div class="card">
          <h2>🕹️ کنترل حرکت</h2>
          <div class="joywrap">
            <div class="joy" id="motor-joy"><div class="knob" id="motor-knob"></div></div>
            <div style="min-width:120px;">
              <div><span style="color:var(--text-light);">جهت</span> <span id="motor-dir" style="font-size:1.2rem;">—</span></div>
              <div><span style="color:var(--text-light);">سرعت</span> <span id="motor-speed" style="font-size:1.2rem;">0</span>%</div>
              <div class="row" style="margin-top:12px;">
                <button id="motor-stop" class="btn-danger">🛑 توقف</button>
                <span class="hint">سرعت {speed}</span>
              </div>
            </div>
          </div>
          <div class="hint">جویاستیک: بالا/پایین = جلو/عقب · چپ/راست = چرخش</div>
        </div>

        <!-- Pan/Tilt joystick (with Persian labels) -->
        <div class="card">
          <h2>🎯 پن و تیلت</h2>
          <div class="joywrap">
            <div class="joy" id="joy"><div class="knob" id="knob"></div></div>
            <div style="min-width:120px;">
              <div><span style="color:var(--text-light);">پن</span> <span id="panVal" class="sensor-value" style="font-size:1.2rem;">{pan}</span>°</div>
              <div><span style="color:var(--text-light);">تیلت</span> <span id="tiltVal" class="sensor-value" style="font-size:1.2rem;">{tilt}</span>°</div>
              <div class="row" style="margin-top:12px;">
                <form action="/api/servo/center" method="post"><button>🎯 مرکز</button></form>
                <form action="/api/servo/random" method="post"><button>🎲 تصادفی</button></form>
              </div>
            </div>
          </div>
        </div>

        <!-- Ultrasonic sensor with live updates & polling settings -->
        <div class="card">
          <h2>📏 حسگر فراصوت</h2>
          <div class="row flex-between">
            <span style="font-size:1.3rem; font-weight:600;" id="distance-display">{dist_text}</span>
            <span class="hint" id="ultra-timestamp"></span>
          </div>
          <div class="row">
            <form action="/api/ultra" method="post">
              <input type="hidden" name="state" value="1">
              <button type="submit" class="{'btn-success' if ultra_on else ''}">📡 روشن</button>
            </form>
            <form action="/api/ultra" method="post">
              <input type="hidden" name="state" value="0">
              <button type="submit">خاموش</button>
            </form>
            <form action="/api/ultra/read" method="post">
              <button>🔍 خواندن</button>
            </form>
          </div>
          <div class="hint">بروزرسانی خودکار هر</div>
          <div class="settings-row">
            <input type="range" id="pollSlider" min="1" max="10" value="2" step="1" style="flex: 0.7;">
            <input type="number" id="pollInput" class="poll-input" min="1" max="10" value="2">
            <span style="font-size:0.9rem;">ثانیه</span>
            <button id="savePollInterval" style="padding: 6px 14px;">تنظیم</button>
          </div>
        </div>

        <!-- IR sensor -->
        <div class="card">
          <h2>🎛️ کنترل IR</h2>
          <div class="row flex-between">
            <span>آخرین کد: <code id="ir-value">{ir_text}</code></span>
          </div>
          <div class="row">
            <form action="/api/ir" method="post"><input type="hidden" name="state" value="1"><button>📥 روشن</button></form>
            <form action="/api/ir" method="post"><input type="hidden" name="state" value="0"><button>خاموش</button></form>
            <form action="/api/ir/read" method="post"><button>🎛 خواندن</button></form>
          </div>
        </div>

        <!-- LEDs -->
        <div class="card">
          <h2>💡 LED ها</h2>
          <div class="led-grid">
            {presets_html}
            <form action="/api/led/off" method="post" style="display:inline;"><button>خاموش</button></form>
          </div>
          <hr>
          <div class="row">
            <form action="/api/led/rgb_all" method="post" class="row">
              <span>RGB همه:</span>
              <input type="number" name="r" min="0" max="255" value="255" style="width:60px;"> R
              <input type="number" name="g" min="0" max="255" value="0" style="width:60px;"> G
              <input type="number" name="b" min="0" max="255" value="0" style="width:60px;"> B
              <button type="submit">تنظیم</button>
            </form>
          </div>
          <div class="row">
            <form action="/api/led/rgb_one" method="post" class="row">
              <span>تکی:</span>
              N <input type="number" name="n" min="0" max="{NUM_LEDS}" value="1" style="width:50px;">
              R <input type="number" name="r" min="0" max="255" value="0" style="width:50px;">
              G <input type="number" name="g" min="0" max="255" value="255" style="width:50px;">
              B <input type="number" name="b" min="0" max="255" value="0" style="width:50px;">
              <button type="submit">تنظیم</button>
            </form>
          </div>
        </div>

        <!-- Buzzer -->
        <div class="card">
          <h2>🔊 بوق</h2>
          <div class="row">
            <form action="/api/buzzer" method="post"><input type="hidden" name="state" value="1"><button>🔊 روشن</button></form>
            <form action="/api/buzzer" method="post"><input type="hidden" name="state" value="0"><button>🔇 خاموش</button></form>
            <form action="/api/buzzer/pulse" method="post"><button>🔔 بوق کوتاه</button></form>
          </div>
        </div>

        <!-- Light effects -->
        <div class="card">
          <h2>✨ افکت نوری</h2>
          <div class="led-grid">
            {effects_html}
            <form action="/api/light/stop" method="post"><button class="btn-danger">توقف</button></form>
          </div>
          <div class="row" style="margin-top:12px;">
            <form action="/api/light/effect" method="post" class="row">
              <select name="name" style="padding:6px; border-radius:30px; border:1px solid var(--border); background:var(--card); color:var(--text);">
                {''.join([f'<option value="{e}">{e}</option>' for e in LIGHT_EFFECTS])}
              </select>
              <input type="number" name="duration" min="1" max="300" value="10" style="width:70px;" placeholder="ثانیه">
              <input type="number" name="speed" step="0.01" min="0.01" max="1.0" value="0.05" style="width:70px;">
              <input type="number" name="color" min="0" max="6" value="0" style="width:60px;" placeholder="رنگ">
              <button type="submit">اجرا</button>
            </form>
          </div>
        </div>

        <!-- Sequence -->
        <div class="card">
          <h2>🔁 دنباله</h2>
          <form action="/api/sequence/run" method="post">
            <button type="submit" style="width:100%;">▶ جلو ۳ث → ۳ عکس → عقب ۳ث</button>
          </form>
        </div>

      </div> <!-- grid -->
    </div> <!-- grid-wrapper -->

    <!-- Footer with emojis -->
    <div class="footer">
      Code By ❤️ with ☕ | Parsrad AI Bot
    </div>
  </div> <!-- app-container -->

<script>
  // ----- THEME / DARK MODE -----
  const themeToggle = document.getElementById('theme-toggle');
  const root = document.documentElement;
  const storedTheme = localStorage.getItem('theme') || 'light';
  if (storedTheme === 'dark') {{
    root.setAttribute('data-theme', 'dark');
    themeToggle.textContent = '☀️ روشن';
  }} else {{
    root.setAttribute('data-theme', 'light');
    themeToggle.textContent = '🌙 تاریک';
  }}
  themeToggle.addEventListener('click', () => {{
    let theme = root.getAttribute('data-theme');
    if (theme === 'light') {{
      root.setAttribute('data-theme', 'dark');
      localStorage.setItem('theme', 'dark');
      themeToggle.textContent = '☀️ روشن';
    }} else {{
      root.setAttribute('data-theme', 'light');
      localStorage.setItem('theme', 'light');
      themeToggle.textContent = '🌙 تاریک';
    }}
  }});

  // ----- PAN/TILT JOYSTICK (Persian labels, same mapping) -----
  const joy = document.getElementById("joy");
  const knob = document.getElementById("knob");
  const panVal = document.getElementById("panVal");
  const tiltVal = document.getElementById("tiltVal");

  const PAN_MIN = {PAN_MIN}, PAN_MAX = {PAN_MAX};
  const TILT_MIN = {TILT_MIN}, TILT_MAX = {TILT_MAX};

  let dragging = false;
  let center = {{x: 0, y: 0}};
  let radius = 0;
  let lastSend = 0;

  function layoutJoy() {{
    const r = joy.getBoundingClientRect();
    center = {{ x: r.left + r.width/2, y: r.top + r.height/2 }};
    radius = r.width/2 - 36;
  }}
  window.addEventListener("resize", layoutJoy);
  layoutJoy();

  function setKnob(dx, dy) {{
    knob.style.left = "50%";
    knob.style.top = "50%";
    knob.style.transform = `translate(-50%, -50%) translate(${{dx}}px, ${{dy}}px)`;
  }}

  function map(v, inMin, inMax, outMin, outMax) {{
    const t = (v - inMin) / (inMax - inMin);
    return outMin + t * (outMax - outMin);
  }}

  function sendServo(pan, tilt) {{
    const now = Date.now();
    if (now - lastSend < 80) return;
    lastSend = now;
    fetch("/api/servo/set_json", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ pan: pan, tilt: tilt }})
    }});
  }}

  function pointerMove(clientX, clientY) {{
    let dx = clientX - center.x;
    let dy = clientY - center.y;
    const dist = Math.hypot(dx, dy);
    if (dist > radius) {{
      const k = radius / dist;
      dx *= k; dy *= k;
    }}
    setKnob(dx, dy);
    const pan = Math.round(map(dx, -radius, radius, PAN_MIN, PAN_MAX));
    const tilt = Math.round(map(dy, -radius, radius, TILT_MIN, TILT_MAX));
    panVal.textContent = pan;
    tiltVal.textContent = tilt;
    sendServo(pan, tilt);
  }}

  function endDrag() {{ dragging = false; }}
  joy.addEventListener("pointerdown", (e) => {{
    dragging = true;
    joy.setPointerCapture(e.pointerId);
    layoutJoy();
    pointerMove(e.clientX, e.clientY);
  }});
  joy.addEventListener("pointermove", (e) => {{
    if (!dragging) return;
    pointerMove(e.clientX, e.clientY);
  }});
  joy.addEventListener("pointerup", endDrag);
  joy.addEventListener("pointercancel", endDrag);

  // ----- MOTOR JOYSTICK (NEW) -----
  const motorJoy = document.getElementById("motor-joy");
  const motorKnob = document.getElementById("motor-knob");
  const motorDir = document.getElementById("motor-dir");
  const motorSpeed = document.getElementById("motor-speed");
  const motorStop = document.getElementById("motor-stop");

  let motorDragging = false;
  let motorCenter = {{x: 0, y: 0}};
  let motorRadius = 0;
  let lastMotorSend = 0;

  function layoutMotorJoy() {{
    const r = motorJoy.getBoundingClientRect();
    motorCenter = {{ x: r.left + r.width/2, y: r.top + r.height/2 }};
    motorRadius = r.width/2 - 36;
  }}
  window.addEventListener("resize", layoutMotorJoy);
  layoutMotorJoy();

  function setMotorKnob(dx, dy) {{
    motorKnob.style.left = "50%";
    motorKnob.style.top = "50%";
    motorKnob.style.transform = `translate(-50%, -50%) translate(${{dx}}px, ${{dy}}px)`;
  }}

  function sendMotorCommand(nx, ny) {{
    // nx, ny normalized between -1 and 1
    const now = Date.now();
    if (now - lastMotorSend < 80) return;
    lastMotorSend = now;
    fetch("/api/motor/joystick", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ x: nx, y: ny }})
    }});
  }}

  function motorPointerMove(clientX, clientY) {{
    let dx = clientX - motorCenter.x;
    let dy = clientY - motorCenter.y;
    const dist = Math.hypot(dx, dy);
    if (dist > motorRadius) {{
      const k = motorRadius / dist;
      dx *= k; dy *= k;
    }}
    setMotorKnob(dx, dy);

    // Normalize to -1 .. 1
    let nx = dx / motorRadius;
    let ny = dy / motorRadius;
    // Clamp to -1..1 (should already be)
    nx = Math.max(-1, Math.min(1, nx));
    ny = Math.max(-1, Math.min(1, ny));

    // Determine direction text
    let dirText = "";
    if (Math.abs(ny) > 0.2) {{
      dirText += ny > 0 ? "عقب" : "جلو";
    }}
    if (Math.abs(nx) > 0.2) {{
      if (dirText) dirText += " و ";
      dirText += nx > 0 ? "راست" : "چپ";
    }}
    if (!dirText) dirText = "—";
    motorDir.textContent = dirText;

    // Speed percentage (magnitude)
    let speedPercent = Math.round(Math.hypot(nx, ny) * 100);
    motorSpeed.textContent = speedPercent;

    sendMotorCommand(nx, ny);
  }}

  function motorEndDrag() {{
    motorDragging = false;
    // Return knob to center and stop motors
    setMotorKnob(0, 0);
    motorDir.textContent = "—";
    motorSpeed.textContent = "0";
    sendMotorCommand(0, 0);
  }}

  motorJoy.addEventListener("pointerdown", (e) => {{
    motorDragging = true;
    motorJoy.setPointerCapture(e.pointerId);
    layoutMotorJoy();
    motorPointerMove(e.clientX, e.clientY);
  }});
  motorJoy.addEventListener("pointermove", (e) => {{
    if (!motorDragging) return;
    motorPointerMove(e.clientX, e.clientY);
  }});
  motorJoy.addEventListener("pointerup", motorEndDrag);
  motorJoy.addEventListener("pointercancel", motorEndDrag);

  // Stop button
  motorStop.addEventListener("click", (e) => {{
    e.preventDefault();
    motorEndDrag();
    // Also call motor stop API
    fetch("/api/motor/stop", {{ method: "POST" }});
  }});

  // ----- CAMERA STREAM TOGGLE -----
  const streamImg = document.getElementById('stream-img');
  const toggleBtn = document.getElementById('toggle-stream');
  let streamActive = false;

  function enableStream() {{
    streamImg.src = '/stream';
    streamImg.classList.remove('cam-placeholder');
    streamImg.classList.add('cam-live');
    toggleBtn.textContent = '⏹ توقف ویدیو';
    toggleBtn.classList.remove('btn-success');
    toggleBtn.classList.add('btn-danger');
    document.getElementById('cam-badge').innerText = 'live';
    streamActive = true;
  }}

  function disableStream() {{
    streamImg.src = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="400" height="225" viewBox="0 0 400 225"%3E%3Crect width="400" height="225" fill="%23eef2f6"/%3E%3Ctext x="50%25" y="50%25" font-size="14" font-family="system-ui, sans-serif" fill="%235a6f85" text-anchor="middle" dy=".3em"%3EStream stopped%3C/text%3E%3C/svg%3E';
    streamImg.classList.add('cam-placeholder');
    streamImg.classList.remove('cam-live');
    toggleBtn.textContent = '▶ شروع ویدیو';
    toggleBtn.classList.remove('btn-danger');
    toggleBtn.classList.add('btn-success');
    document.getElementById('cam-badge').innerText = 'خاموش';
    streamActive = false;
  }}

  toggleBtn.addEventListener('click', (e) => {{
    e.preventDefault();
    if (streamActive) {{
      disableStream();
    }} else {{
      enableStream();
    }}
  }});
  disableStream(); // start with stream off

  // ----- ULTRASONIC AUTO POLLING – fixed interval -----
  const ultraBadge = document.getElementById('ultra-badge');
  const distanceDisplay = document.getElementById('distance-display');
  const ultraTimestamp = document.getElementById('ultra-timestamp');
  const pollSlider = document.getElementById('pollSlider');
  const pollInput = document.getElementById('pollInput');
  const savePollBtn = document.getElementById('savePollInterval');

  let pollTimer = null;

  // Load saved interval or default 2
  let savedInterval = localStorage.getItem('pollInterval');
  let pollIntervalSec = savedInterval ? parseFloat(savedInterval) : 2;
  pollSlider.value = pollIntervalSec;
  pollInput.value = pollIntervalSec;

  function updatePollInterval(val) {{
    pollSlider.value = val;
    pollInput.value = val;
    localStorage.setItem('pollInterval', val);
    restartPolling();
  }}

  pollSlider.addEventListener('input', () => {{
    pollInput.value = pollSlider.value;
  }});
  pollSlider.addEventListener('change', () => {{
    updatePollInterval(parseFloat(pollSlider.value));
  }});
  pollInput.addEventListener('change', () => {{
    let v = parseFloat(pollInput.value);
    if (isNaN(v) || v < 1) v = 1;
    if (v > 10) v = 10;
    updatePollInterval(v);
  }});
  savePollBtn.addEventListener('click', () => {{
    let v = parseFloat(pollInput.value);
    if (isNaN(v) || v < 1) v = 1;
    if (v > 10) v = 10;
    updatePollInterval(v);
  }});

  function fetchUltraStatus() {{
    if (ultraBadge.innerText.trim() !== 'روشن') return;
    fetch('/api/ultra/status')
      .then(res => res.json())
      .then(data => {{
        if (data.distance !== null) {{
          distanceDisplay.innerText = data.distance + ' mm';
        }} else {{
          distanceDisplay.innerText = '—';
        }}
        const now = new Date();
        ultraTimestamp.innerText = 'بروزرسانی ' + now.toLocaleTimeString('fa-IR', {{ hour: '2-digit', minute: '2-digit', second: '2-digit' }});
      }})
      .catch(err => console.warn('ultra poll error', err));
  }}

  function restartPolling() {{
    if (pollTimer) clearInterval(pollTimer);
    const intervalMs = parseFloat(localStorage.getItem('pollInterval') || '2') * 1000;
    pollTimer = setInterval(fetchUltraStatus, intervalMs);
    if (ultraBadge.innerText.trim() === 'روشن') fetchUltraStatus();
  }}

  restartPolling();
  const observer = new MutationObserver(restartPolling);
  observer.observe(ultraBadge, {{ attributes: true, childList: true, subtree: true, characterData: true }});
</script>
</body>
</html>"""


# ----------------------------------------------------------------------
# NEW JSON endpoint for motor joystick
# ----------------------------------------------------------------------
@app.post("/api/motor/joystick")
def api_motor_joystick():
    data = request.get_json(force=True, silent=True) or {}
    x = clamp_float(data.get("x"), -1, 1, 0)
    y = clamp_float(data.get("y"), -1, 1, 0)
    
    speed = state["speed"]
    
    with hw_lock:
        # Deadzone
        if abs(x) < 0.1 and abs(y) < 0.1:
            stop_all_motors()
            set_status("Motors stopped (joystick center)")
        else:
            # Mixing: forward/backward (y) and left/right (x)
            # y positive = backward, y negative = forward (since screen Y+ is down)
            left_speed = y * speed - x * speed
            right_speed = y * speed + x * speed
            
            # Clamp to -255..255
            left_speed = int(max(-255, min(255, left_speed)))
            right_speed = int(max(-255, min(255, right_speed)))
            
            # Apply to motors
            bot.Ctrl_Muto(0, left_speed)   # left front
            bot.Ctrl_Muto(1, left_speed)   # left rear
            bot.Ctrl_Muto(2, right_speed)  # right front
            bot.Ctrl_Muto(3, right_speed)  # right rear
            
            set_status(f"Joystick: L={left_speed}, R={right_speed}")
    
    return jsonify({"ok": True})


# ----------------------------------------------------------------------
# JSON endpoint for ultrasonic status
# ----------------------------------------------------------------------
@app.get("/api/ultra/status")
def api_ultra_status():
    with hw_lock:
        if state["ultra_on"]:
            try:
                d = read_ultrasonic_mm()
                state["last_distance_mm"] = d
            except Exception:
                pass
        return jsonify({
            "ultra_on": state["ultra_on"],
            "distance": state["last_distance_mm"]
        })


# ----------------------------------------------------------------------
# ENHANCED GALLERY – Persian RTL
# ----------------------------------------------------------------------
@app.get("/gallery")
def gallery():
    imgs = list_images()
    cards = ""
    for p in imgs[:100]:
        name = p.name
        mod_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
        size = p.stat().st_size // 1024
        cards += f"""
        <div class="gallery-card">
          <a href="/img/{name}" target="_blank">
            <img src="/img/{name}" loading="lazy" alt="{name}">
          </a>
          <div class="gallery-info">
            <span class="gallery-name">{name[:40]}{'…' if len(name)>40 else ''}</span>
            <span class="gallery-meta">{mod_time} · {size} KB</span>
          </div>
        </div>
        """
    return f"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>راسپبات · گالری</title>
  <link href="https://cdn.fontcdn.ir/Font/Persian/Vazir/Vazir.css" rel="stylesheet">
  <style>
    :root {{
      --bg: #f9fafc;
      --card: white;
      --border: #e6edf4;
      --text: #1e2f4e;
      --text-light: #5a6f85;
      --accent: #3b82f6;
      --radius: 16px;
    }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: 'Vazir', 'Inter', sans-serif;
      margin: 0;
      padding: 20px;
    }}
    .gallery-header {{
      display: flex;
      align-items: center;
      gap: 16px;
      margin-bottom: 24px;
      flex-wrap: wrap;
    }}
    .btn {{
      background: white;
      border: 1px solid var(--border);
      padding: 10px 18px;
      border-radius: 40px;
      text-decoration: none;
      color: var(--text);
      font-size: 0.95rem;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      transition: 0.1s;
    }}
    .btn:hover {{
      border-color: var(--accent);
      background: var(--accent);
      color: white;
    }}
    .gallery-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 20px;
    }}
    .gallery-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
      box-shadow: 0 4px 12px rgba(0,0,0,0.02);
      transition: transform 0.1s;
    }}
    .gallery-card:hover {{
      transform: scale(1.01);
      border-color: var(--accent);
    }}
    .gallery-card img {{
      width: 100%;
      aspect-ratio: 16/9;
      object-fit: cover;
      display: block;
      border-bottom: 1px solid var(--border);
    }}
    .gallery-info {{
      padding: 14px;
    }}
    .gallery-name {{
      font-weight: 500;
      display: block;
      margin-bottom: 6px;
      word-break: break-word;
    }}
    .gallery-meta {{
      font-size: 0.8rem;
      color: var(--text-light);
    }}
  </style>
</head>
<body>
  <div class="gallery-header">
    <a href="/" class="btn">← بازگشت</a>
    <form action="/api/camera/snap" method="post" style="display:inline;">
      <button class="btn" type="submit">📸 عکس جدید</button>
    </form>
    <form action="/api/panic" method="post" style="display:inline;">
      <button class="btn" style="border-color:#ffb3b3; color:#c73e3e;">🧯 اورژانس</button>
    </form>
    <span style="margin-right:auto; color:var(--text-light);">{len(imgs)} عکس</span>
  </div>
  <div class="gallery-grid">
    {cards if cards else '<p style="grid-column:1/-1; text-align:center; padding:40px;">📭 هنوز عکسی گرفته نشده</p>'}
  </div>
</body>
</html>"""


@app.get("/img/<name>")
def img(name):
    path = SAVE_DIR / name
    if not path.exists():
        return ("Not found", 404)
    data = path.read_bytes()
    ct = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return Response(data, mimetype=ct)


@app.get("/stream")
def stream():
    return Response(mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")


# ----------------------------------------------------------------------
# ALL ORIGINAL ROUTES (with LED preset fix & state persistence)
# ----------------------------------------------------------------------
@app.get("/")
def index():
    return page_html()

@app.post("/api/panic")
def api_panic():
    with hw_lock:
        try:
            stop_all_motors()
        except Exception:
            pass
        try:
            bot.Ctrl_BEEP_Switch(0)
        except Exception:
            pass
        try:
            bot.Ctrl_WQ2812_ALL(0, 0)
        except Exception:
            pass
        try:
            bot.Ctrl_Ulatist_Switch(0)
            state["ultra_on"] = 0
        except Exception:
            pass
        try:
            bot.Ctrl_IR_Switch(0)
            state["ir_on"] = 0
        except Exception:
            pass
        stop_lightshow()
        state["last_led_color"] = None
        set_status("اورژانس: همه موتورها، بوق، LED و سنسورها خاموش")
    return redirect(url_for("index"))

@app.post("/api/motor/speed")
def api_motor_speed():
    s = clamp_int(request.form.get("speed"), 0, 255, DEFAULT_SPEED)
    state["speed"] = s
    set_status(f"سرعت تنظیم شد به {s}")
    return redirect(url_for("index"))

@app.post("/api/motor/move")
def api_motor_move():
    direction = request.form.get("dir", "forward")
    speed = state["speed"]
    with hw_lock:
        if direction == "forward":
            drive_all(speed)
            set_status(f"حرکت به جلو با سرعت {speed}")
        elif direction == "backward":
            drive_all(-speed)
            set_status(f"حرکت به عقب با سرعت {speed}")
        elif direction == "left":
            spin_left(speed)
            set_status(f"چرخش به چپ با سرعت {speed}")
        elif direction == "right":
            spin_right(speed)
            set_status(f"چرخش به راست با سرعت {speed}")
        else:
            stop_all_motors()
            set_status("توقف موتورها")
    return redirect(url_for("index"))

@app.post("/api/motor/stop")
def api_motor_stop():
    with hw_lock:
        stop_all_motors()
        set_status("موتورها متوقف شدند")
    return redirect(url_for("index"))

@app.post("/api/motor/pulse")
def api_motor_pulse():
    speed = state["speed"]
    with hw_lock:
        drive_all(speed)
        set_status(f"پالس جلو ۰.۵ ثانیه با سرعت {speed}")
    time.sleep(0.5)
    with hw_lock:
        stop_all_motors()
        set_status("پالس پایان یافت")
    return redirect(url_for("index"))

@app.post("/api/servo/set_json")
def api_servo_set_json():
    data = request.get_json(force=True, silent=True) or {}
    pan = clamp_int(data.get("pan"), PAN_MIN, PAN_MAX, state["pan"])
    tilt = clamp_int(data.get("tilt"), TILT_MIN, TILT_MAX, state["tilt"])
    with hw_lock:
        safe_servo(1, pan)
        safe_servo(2, tilt)
        set_status(f"سروو پن={state['pan']} تیلت={state['tilt']}")
    return jsonify({"ok": True, "pan": state["pan"], "tilt": state["tilt"]})

@app.post("/api/servo/center")
def api_servo_center():
    with hw_lock:
        safe_servo(1, 90)
        safe_servo(2, 90)
        set_status("سرووها در مرکز")
    return redirect(url_for("index"))

@app.post("/api/servo/random")
def api_servo_random():
    with hw_lock:
        pan = random.randint(PAN_MIN, PAN_MAX)
        tilt = random.randint(TILT_MIN, TILT_MAX)
        safe_servo(1, pan)
        safe_servo(2, tilt)
        set_status(f"سروو تصادفی پن={pan} تیلت={tilt}")
    return redirect(url_for("index"))

@app.post("/api/led/preset")
def api_led_preset():
    color = clamp_int(request.form.get("color"), 0, 6, 0)
    rgb_map = {
        0: (255, 0, 0),     # Red
        1: (0, 255, 0),     # Green
        2: (0, 0, 255),     # Blue
        3: (255, 255, 0),   # Yellow
        4: (255, 0, 255),   # Purple
        5: (0, 255, 255),   # Cyan
        6: (255, 255, 255), # White
    }
    r, g, b = rgb_map.get(color, (255, 0, 0))
    with hw_lock:
        bot.Ctrl_WQ2812_brightness_ALL(r, g, b)
        state["last_led_color"] = color
        set_status(f"LED preset color={color} (RGB {r},{g},{b})")
    return redirect(url_for("index"))

@app.post("/api/led/off")
def api_led_off():
    with hw_lock:
        bot.Ctrl_WQ2812_ALL(0, 0)
        state["last_led_color"] = None
        set_status("LED ها خاموش")
    return redirect(url_for("index"))

@app.post("/api/led/rgb_all")
def api_led_rgb_all():
    r = clamp_int(request.form.get("r"), 0, 255, 0)
    g = clamp_int(request.form.get("g"), 0, 255, 0)
    b = clamp_int(request.form.get("b"), 0, 255, 0)
    with hw_lock:
        bot.Ctrl_WQ2812_brightness_ALL(r, g, b)
        state["last_led_color"] = None
        set_status(f"LED RGB همه ({r},{g},{b})")
    return redirect(url_for("index"))

@app.post("/api/led/rgb_one")
def api_led_rgb_one():
    n = clamp_int(request.form.get("n"), 0, NUM_LEDS, 1)
    r = clamp_int(request.form.get("r"), 0, 255, 0)
    g = clamp_int(request.form.get("g"), 0, 255, 0)
    b = clamp_int(request.form.get("b"), 0, 255, 0)
    with hw_lock:
        bot.Ctrl_WQ2812_brightness_Alone(n, r, g, b)
        state["last_led_color"] = None
        set_status(f"LED {n} RGB ({r},{g},{b})")
    return redirect(url_for("index"))

@app.post("/api/buzzer")
def api_buzzer():
    st = clamp_int(request.form.get("state"), 0, 1, 0)
    with hw_lock:
        bot.Ctrl_BEEP_Switch(st)
        set_status("بوق روشن" if st else "بوق خاموش")
    return redirect(url_for("index"))

@app.post("/api/buzzer/pulse")
def api_buzzer_pulse():
    with hw_lock:
        bot.Ctrl_BEEP_Switch(1)
        set_status("بوق ۰.۲ ثانیه")
    time.sleep(0.2)
    with hw_lock:
        bot.Ctrl_BEEP_Switch(0)
        set_status("بوق پایان")
    return redirect(url_for("index"))

@app.post("/api/ultra")
def api_ultra():
    st = clamp_int(request.form.get("state"), 0, 1, 0)
    with hw_lock:
        bot.Ctrl_Ulatist_Switch(st)
        state["ultra_on"] = st
        set_status("سنسور فراصوت روشن" if st else "سنسور فراصوت خاموش")
    return redirect(url_for("index"))

@app.post("/api/ultra/read")
def api_ultra_read():
    with hw_lock:
        if not state["ultra_on"]:
            bot.Ctrl_Ulatist_Switch(1)
            state["ultra_on"] = 1
            time.sleep(0.05)
        try:
            d = read_ultrasonic_mm()
            state["last_distance_mm"] = d
            set_status(f"فاصله فراصوت {d} mm")
        except Exception as e:
            set_status(f"خطا در خواندن فراصوت: {e}")
    return redirect(url_for("index"))

@app.post("/api/ir")
def api_ir():
    st = clamp_int(request.form.get("state"), 0, 1, 0)
    with hw_lock:
        bot.Ctrl_IR_Switch(st)
        state["ir_on"] = st
        set_status("IR روشن" if st else "IR خاموش")
    return redirect(url_for("index"))

@app.post("/api/ir/read")
def api_ir_read():
    with hw_lock:
        if not state["ir_on"]:
            bot.Ctrl_IR_Switch(1)
            state["ir_on"] = 1
            time.sleep(0.05)
        try:
            b = read_ir_byte()
            state["last_ir_byte"] = b
            set_status(f"کد IR {b}")
        except Exception as e:
            set_status(f"خطا در خواندن IR: {e}")
    return redirect(url_for("index"))

@app.post("/api/light/effect")
def api_light_effect():
    global lightshow_thread
    name = request.form.get("name", "breathing")
    if name not in LIGHT_EFFECTS:
        name = "breathing"
    duration = clamp_float(request.form.get("duration"), 1, 300, 10)
    speed = clamp_float(request.form.get("speed"), 0.01, 1.0, 0.05)
    color = clamp_int(request.form.get("color"), 0, 6, 0)
    with hw_lock:
        stop_lightshow()
    def worker():
        try:
            run_lightshow(name, duration, speed, color)
        finally:
            with hw_lock:
                set_status("افکت نوری پایان یافت")
    lightshow_thread = threading.Thread(target=worker, daemon=True)
    lightshow_thread.start()
    set_status(f"افکت '{name}' شروع شد ({duration} ثانیه)")
    return redirect(url_for("index"))

@app.post("/api/light/stop")
def api_light_stop():
    with hw_lock:
        stop_lightshow()
        try:
            bot.Ctrl_WQ2812_ALL(0, 0)
        except Exception:
            pass
        set_status("افکت نوری متوقف شد")
    return redirect(url_for("index"))

@app.post("/api/camera/snap")
def api_camera_snap():
    p = capture_picture(tag="snap")
    if p is None:
        set_status("عکس گرفته نشد")
        return redirect(url_for("index"))
    set_status(f"ذخیره شد {p.name}")
    return redirect(url_for("gallery"))

@app.post("/api/sequence/run")
def api_sequence_run():
    if not run_lock.acquire(blocking=False):
        set_status("دنباله در حال اجراست")
        return redirect(url_for("index"))
    def worker():
        try:
            with hw_lock:
                stop_all_motors()
            time.sleep(0.1)
            with hw_lock:
                drive_all(state["speed"])
                set_status("دنباله: حرکت به جلو ۳ ثانیه")
            time.sleep(3.0)
            with hw_lock:
                stop_all_motors()
                set_status("دنباله: توقف، گرفتن ۳ عکس با سروو تصادفی")
            time.sleep(0.2)
            for i in range(3):
                with hw_lock:
                    safe_servo(1, random.randint(PAN_MIN, PAN_MAX))
                    safe_servo(2, random.randint(TILT_MIN, TILT_MAX))
                time.sleep(0.25)
                capture_picture(tag=f"seq{i}")
                time.sleep(0.75)
            with hw_lock:
                drive_all(-state["speed"])
                set_status("دنباله: حرکت به عقب ۳ ثانیه")
            time.sleep(3.0)
            with hw_lock:
                stop_all_motors()
                set_status("دنباله پایان یافت")
        finally:
            run_lock.release()
    threading.Thread(target=worker, daemon=True).start()
    return redirect(url_for("index"))


# ----------------------------------------------------------------------
# START SERVER
# ----------------------------------------------------------------------
if __name__ == "__main__":
    with hw_lock:
        try:
            stop_all_motors()
        except Exception:
            pass
        try:
            bot.Ctrl_BEEP_Switch(0)
        except Exception:
            pass
        try:
            bot.Ctrl_WQ2812_ALL(0, 0)
        except Exception:
            pass

    print("Raspbot Control Center · RTL Persian · Dual Joysticks · Polling Fixed")
    print("Find IP: hostname -I")
    print(f"Open: http://<PI_IP>:{PORT}")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
