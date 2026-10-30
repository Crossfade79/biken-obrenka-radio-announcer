#!/usr/bin/env python3
# BIKEN-1 Web/CLI controller for Raspberry Pi + Motorola CM140
# GPIO17 / physical pin 11 = PTT via optocoupler
# Audio output = Raspberry Pi 3.5mm jack via ALSA/aplay

import argparse
import json
import math
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

APP_DIR = Path("/opt/biken")
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
AUDIO_DIR = APP_DIR / "audio"

DEFAULT_CONFIG = {
    "name": "BIKEN-1",
    "ptt_gpio_bcm": 17,
    "ptt_active_high": True,
    "default_duration_s": 20.0,
    "pre_roll_s": 1.0,
    "post_roll_s": 0.5,
    "max_tx_s": 60.0,
    "sample_rate": 48000,
    "audio_level": 0.45,
    "tone_a_hz": 1200,
    "tone_b_hz": 1800,
    "chip_ms": 8,
    "marker_period_s": 1.0,
    "beacon_format": "BIKEN_V3_7_GPS_FIELDS_CONT_AUDIO_GPSSEC_CRC8",
    "web_host": "0.0.0.0",
    "web_port": 8891,
    "dry_run_gpio": False
}

state_lock = threading.Lock()
gps_cache_lock = threading.Lock()
gps_cache = {"ts": 0.0, "data": None}
run_lock = threading.Lock()
stop_event = threading.Event()
worker = None
continuous_audio_thread = None
continuous_audio_stop_event = threading.Event()
continuous_audio_lock = threading.Lock()
gpio_ready = False
GPIO = None
config = {}

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def ensure_dirs():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

def load_config():
    ensure_dirs()
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    cfg = DEFAULT_CONFIG.copy()
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg

def save_config(cfg):
    ensure_dirs()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

def write_state(**kw):
    ensure_dirs()
    with state_lock:
        st = {}
        if STATE_PATH.exists():
            try:
                st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except Exception:
                st = {}
        st.update(kw)
        st["updated"] = now()
        STATE_PATH.write_text(json.dumps(st, indent=2), encoding="utf-8")
        return st


def _read_gps_status_slow():
    """
    Slow best-effort GPS/PPS read. This must never run on every UI refresh.
    """
    gps = {
        "available": False,
        "lock": False,
        "mode": 0,
        "fix_type": "NO DATA",
        "satellites": None,
        "lat": None,
        "lon": None,
        "alt": None,
        "hdop": None,
        "pps": False,
        "pps_source": "UNKNOWN",
        "pps_state": "NO PPS",
        "last_pulse": None,
        "error": None,
    }

    try:
        if Path("/dev/pps0").exists():
            gps["pps"] = True
            gps["pps_source"] = "/dev/pps0"
            gps["pps_state"] = "READY"
    except Exception:
        pass

    try:
        if shutil.which("chronyc"):
            out = subprocess.check_output(["chronyc", "sources", "-n"], timeout=0.25, text=True, stderr=subprocess.DEVNULL)
            if "PPS" in out.upper() or "/dev/pps" in out.lower():
                gps["pps"] = True
                gps["pps_source"] = "chrony"
                gps["pps_state"] = "LOCKED"
    except Exception:
        pass

    if not shutil.which("gpspipe"):
        gps["error"] = "gpspipe not installed"
        return gps

    try:
        # Keep this short. UI must stay snappy.
        out = subprocess.check_output(["gpspipe", "-w", "-n", "6"], timeout=0.45, text=True, stderr=subprocess.DEVNULL)
        gps["available"] = True
        import json as _json
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = _json.loads(line)
            except Exception:
                continue

            cls = obj.get("class")
            if cls == "TPV":
                mode = int(obj.get("mode") or 0)
                gps["mode"] = max(gps["mode"], mode)
                gps["lock"] = mode >= 2
                gps["fix_type"] = "3D" if mode >= 3 else ("2D" if mode == 2 else "NO FIX")
                gps["lat"] = obj.get("lat", gps["lat"])
                gps["lon"] = obj.get("lon", gps["lon"])
                gps["alt"] = obj.get("altHAE", obj.get("altMSL", obj.get("alt", gps["alt"])))
                gps["last_pulse"] = obj.get("time", gps["last_pulse"])
            elif cls == "SKY":
                sats = obj.get("satellites") or []
                used = sum(1 for s in sats if s.get("used"))
                gps["satellites"] = used if used else len(sats)
                gps["hdop"] = obj.get("hdop", gps["hdop"])

        return gps
    except Exception as e:
        gps["error"] = str(e)
        return gps


def _nmea_checksum_ok(line):
    try:
        if "*" not in line:
            return True
        body = line.strip()[1:line.index("*")]
        got = int(line.split("*",1)[1][:2], 16)
        c = 0
        for ch in body:
            c ^= ord(ch)
        return c == got
    except Exception:
        return False

def _nmea_coord(raw, hemi):
    try:
        if not raw:
            return None
        v = float(raw)
        deg = int(v // 100)
        minutes = v - deg * 100
        out = deg + minutes / 60.0
        if hemi in ("S", "W"):
            out = -out
        return out
    except Exception:
        return None

def _gps_empty():
    return {
        "available": False,
        "lock": False,
        "mode": 0,
        "fix_type": "NO DATA",
        "satellites": None,
        "lat": None,
        "lon": None,
        "alt": None,
        "hdop": None,
        "pps": False,
        "pps_source": "UNKNOWN",
        "pps_state": "NO PPS",
        "last_pulse": None,
        "error": None,
    }

def _merge_pps_status(gps):
    try:
        if Path("/dev/pps0").exists():
            gps["pps"] = True
            gps["pps_source"] = "/dev/pps0"
            gps["pps_state"] = "READY"
    except Exception:
        pass

    try:
        if shutil.which("chronyc"):
            out = subprocess.check_output(["chronyc", "sources", "-n"], timeout=0.25, text=True, stderr=subprocess.DEVNULL)
            up = out.upper()
            if "PPS" in up or "/DEV/PPS" in up:
                gps["pps"] = True
                gps["pps_source"] = "chrony"
                gps["pps_state"] = "LOCKED"
    except Exception:
        pass
    return gps

def _read_gps_from_gpspipe():
    gps = _gps_empty()
    if not shutil.which("gpspipe"):
        gps["error"] = "gpspipe not installed"
        return gps
    try:
        out = subprocess.check_output(["gpspipe", "-w", "-n", "8"], timeout=0.55, text=True, stderr=subprocess.DEVNULL)
        import json as _json
        gps["available"] = True
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = _json.loads(line)
            except Exception:
                continue
            cls = obj.get("class")
            if cls == "TPV":
                mode = int(obj.get("mode") or 0)
                gps["mode"] = max(gps.get("mode") or 0, mode)
                gps["lock"] = mode >= 2
                gps["fix_type"] = "3D" if mode >= 3 else ("2D" if mode == 2 else "NO FIX")
                gps["lat"] = obj.get("lat", gps.get("lat"))
                gps["lon"] = obj.get("lon", gps.get("lon"))
                gps["alt"] = obj.get("altHAE", obj.get("altMSL", obj.get("alt", gps.get("alt"))))
                gps["last_pulse"] = obj.get("time", gps.get("last_pulse"))
            elif cls == "SKY":
                sats = obj.get("satellites") or []
                used = sum(1 for s in sats if s.get("used"))
                gps["satellites"] = used if used else (len(sats) if sats else None)
                gps["hdop"] = obj.get("hdop", gps.get("hdop"))
        return gps
    except Exception as e:
        gps["error"] = "gpspipe: " + str(e)
        return gps

def _read_gps_from_nmea_serial():
    gps = _gps_empty()
    ports = []
    try:
        for p in (config.get("gps_serial_device"), "/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0"):
            if p and p not in ports:
                ports.append(p)
    except Exception:
        ports = ["/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0"]

    import os, select, time as _time
    for dev in ports:
        try:
            if not Path(dev).exists():
                continue
            fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
            try:
                chunks = []
                deadline = _time.time() + 0.45
                while _time.time() < deadline:
                    r,_,_ = select.select([fd], [], [], 0.05)
                    if fd in r:
                        try:
                            b = os.read(fd, 4096)
                            if b:
                                chunks.append(b)
                        except BlockingIOError:
                            pass
                raw = b"".join(chunks).decode("ascii", "ignore")
            finally:
                os.close(fd)

            if not raw:
                continue

            gps["available"] = True
            gps["source"] = dev
            for line in raw.splitlines():
                line = line.strip()
                if not line.startswith("$") or not _nmea_checksum_ok(line):
                    continue
                parts = line.split("*",1)[0].split(",")
                typ = parts[0][3:] if len(parts[0]) >= 6 else parts[0]
                if typ == "GGA" and len(parts) >= 10:
                    fixq = int(parts[6] or "0")
                    sats = int(parts[7] or "0") if parts[7] else None
                    hdop = float(parts[8]) if parts[8] else None
                    lat = _nmea_coord(parts[2], parts[3])
                    lon = _nmea_coord(parts[4], parts[5])
                    alt = float(parts[9]) if parts[9] else None
                    gps["satellites"] = sats
                    gps["hdop"] = hdop
                    gps["lat"] = lat if lat is not None else gps.get("lat")
                    gps["lon"] = lon if lon is not None else gps.get("lon")
                    gps["alt"] = alt if alt is not None else gps.get("alt")
                    gps["lock"] = fixq > 0
                    gps["mode"] = 3 if fixq > 0 else 0
                    gps["fix_type"] = "3D" if fixq > 0 else "NO FIX"
                    gps["last_pulse"] = parts[1] if len(parts) > 1 else None
                elif typ == "RMC" and len(parts) >= 10:
                    valid = parts[2] == "A"
                    lat = _nmea_coord(parts[3], parts[4])
                    lon = _nmea_coord(parts[5], parts[6])
                    gps["lock"] = gps.get("lock") or valid
                    gps["mode"] = max(gps.get("mode") or 0, 2 if valid else 0)
                    gps["fix_type"] = "2D" if valid and gps.get("fix_type") in ("NO DATA", "NO FIX") else gps.get("fix_type")
                    gps["lat"] = lat if lat is not None else gps.get("lat")
                    gps["lon"] = lon if lon is not None else gps.get("lon")
                    gps["last_pulse"] = parts[1] if len(parts) > 1 else gps.get("last_pulse")
            if gps["available"]:
                return gps
        except Exception as e:
            gps["error"] = str(e)
            continue
    return gps

def _read_gps_status_slow():
    gps = _read_gps_from_gpspipe()
    if not gps.get("lock") and (gps.get("lat") is None or gps.get("lon") is None):
        nmea = _read_gps_from_nmea_serial()
        if nmea.get("available"):
            gps = nmea
    gps = _merge_pps_status(gps)
    if gps.get("pps") and not gps.get("available"):
        gps["fix_type"] = "NO GPS DATA"
    return gps

def get_gps_status():
    try:
        ttl = float(config.get("gps_status_cache_s", 5.0))
    except Exception:
        ttl = 5.0
    t = time.time()
    with gps_cache_lock:
        data = gps_cache.get("data")
        if data is not None and (t - float(gps_cache.get("ts") or 0.0)) < ttl:
            return data

    data = _read_gps_status_slow()

    with gps_cache_lock:
        gps_cache["ts"] = time.time()
        gps_cache["data"] = data
    return data

def read_state():
    with state_lock:
        st = {
            "name": config.get("name", "BIKEN-1"),
            "running": False,
            "ptt": False,
            "mode": "idle",
            "updated": now(),
            "config": config,
        }
        if STATE_PATH.exists():
            try:
                st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
            except Exception:
                pass
        st["gps"] = get_gps_status()
        st["beacon_v2"] = {
            "format": "BIKEN_V3_7_GPS_FIELDS_CONT_AUDIO_GPSSEC_CRC8",
            "preamble": "1111100110101",
            "gps_second_bits": 16,
            "crc_bits": 8,
            "validation": "crc8",
            "end_code": "1110010",
            "marker_payload": "gps_second_mod65536"
        }
        st["audio_stream_thread_alive"] = is_continuous_audio_running()
        return st

def gpio_init():
    global GPIO, gpio_ready
    if config.get("dry_run_gpio"):
        gpio_ready = False
        return False
    try:
        import RPi.GPIO as RGPIO
        GPIO = RGPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(int(config["ptt_gpio_bcm"]), GPIO.OUT)
        gpio_ready = True
        ptt(False)
        return True
    except Exception as e:
        gpio_ready = False
        write_state(gpio_error=str(e))
        return False

def ptt(on):
    active_high = bool(config.get("ptt_active_high", True))
    value = bool(on)
    if not active_high:
        value = not value
    if gpio_ready and GPIO:
        GPIO.output(int(config["ptt_gpio_bcm"]), GPIO.HIGH if value else GPIO.LOW)
    write_state(ptt=bool(on))

def gpio_cleanup():
    try:
        ptt(False)
        if gpio_ready and GPIO:
            GPIO.cleanup()
    except Exception:
        pass

def crc8_atm(bits):
    crc = 0x00
    for b in bits:
        bit = 1 if b == "1" else 0
        mix = ((crc >> 7) & 1) ^ bit
        crc = ((crc << 1) & 0xFF)
        if mix:
            crc ^= 0x07
    return crc & 0xFF

def gps_second_mod65536():
    # RPi system clock should be disciplined by GPS/PPS/chrony.
    return int(time.time()) & 0xFFFF

def biken_v3_bits(gps_sec_mod):
    preamble = "1111100110101"   # Barker-13
    end_code = "1110010"         # Barker-7
    payload = format(int(gps_sec_mod) & 0xFFFF, "016b")
    crc = format(crc8_atm(payload), "08b")
    return preamble + payload + crc + end_code

def make_bits_wav(bits, path, cfg=None, pad_tail_s=0.06):
    cfg = cfg or config
    sr = int(cfg["sample_rate"])
    level = float(cfg["audio_level"])
    tone_a = float(cfg["tone_a_hz"])
    tone_b = float(cfg["tone_b_hz"])
    chip_s = float(cfg["chip_ms"]) / 1000.0
    total = int((len(bits) * chip_s + float(pad_tail_s)) * sr)
    data = [0] * total
    for i, bit in enumerate(bits):
        f = tone_a if bit == "1" else tone_b
        chip_start = int(i * chip_s * sr)
        chip_end = int((i + 1) * chip_s * sr)
        for n in range(chip_start, min(chip_end, total)):
            t = (n - chip_start) / sr
            x = math.sin(2 * math.pi * f * t)
            rel = (n - chip_start) / max(1, (chip_end - chip_start))
            fade = min(1.0, rel * 8, (1 - rel) * 8)
            data[n] += int(32767 * level * x * fade)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for v in data:
            v = max(-32767, min(32767, int(v)))
            frames += int(v).to_bytes(2, "little", signed=True)
        w.writeframes(frames)

def make_v3_marker_wav(gps_sec_mod, path, cfg=None):
    make_bits_wav(biken_v3_bits(gps_sec_mod), path, cfg=cfg)



def pcm_for_bits_1s(bits, cfg=None):
    """
    Generate exactly one second of S16_LE mono PCM.
    Marker is at the beginning of the second, followed by silence.
    """
    cfg = cfg or config
    sr = int(cfg["sample_rate"])
    level = float(cfg["audio_level"])
    tone_a = float(cfg["tone_a_hz"])
    tone_b = float(cfg["tone_b_hz"])
    chip_s = float(cfg["chip_ms"]) / 1000.0

    total = sr
    data = [0] * total

    for i, bit in enumerate(bits):
        f = tone_a if bit == "1" else tone_b
        chip_start = int(i * chip_s * sr)
        chip_end = int((i + 1) * chip_s * sr)
        if chip_start >= total:
            break
        for n in range(chip_start, min(chip_end, total)):
            t = (n - chip_start) / sr
            x = math.sin(2 * math.pi * f * t)
            rel = (n - chip_start) / max(1, (chip_end - chip_start))
            fade = min(1.0, rel * 8, (1 - rel) * 8)
            data[n] = int(32767 * level * x * fade)

    frames = bytearray()
    for v in data:
        v = max(-32767, min(32767, int(v)))
        frames += int(v).to_bytes(2, "little", signed=True)
    return bytes(frames)

def play_v3_live_stream(duration_s, start_epoch_s=None, mode="manual"):
    """
    BIKEN V3.3 live audio generator.

    Starts one aplay raw PCM stream and writes exactly one second of generated
    audio per GPS/UTC second. Each second carries its own:
      BARKER13 + GPS_SECOND_MOD65536 + CRC8 + BARKER7

    This is not a pre-recorded session WAV.
    """
    sr = int(config["sample_rate"])
    if start_epoch_s is None:
        start_epoch_s = math.floor(time.time()) + 1

    count = int(float(duration_s))
    proc = subprocess.Popen(
        ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(sr), "-c", "1"],
        stdin=subprocess.PIPE
    )

    try:
        for idx in range(count):
            if stop_event.is_set():
                break

            gps_epoch = int(start_epoch_s) + idx
            sec_mod = gps_epoch & 0xFFFF
            bits = biken_v3_bits(sec_mod)
            pcm = pcm_for_bits_1s(bits)

            write_state(running=True, mode=mode, ptt=True,
                        last_marker_index=idx + 1,
                        last_marker_gps_second_mod65536=sec_mod,
                        last_marker_utc=gps_epoch,
                        beacon_format="BIKEN_V3_7_GPS_FIELDS_CONT_AUDIO_GPSSEC_CRC8")

            if proc.stdin:
                proc.stdin.write(pcm)
                proc.stdin.flush()

        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=max(2, count + 2))
    except Exception:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        raise

def make_v3_session_wav(duration_s, path, start_epoch_s, cfg=None):
    """
    Build one continuous BIKEN V3.3.1 session WAV.

    Marker at t=0 contains GPS second start_epoch_s mod 65536.
    Marker at t=1 contains start_epoch_s+1 mod 65536, etc.
    """
    cfg = cfg or config
    sr = int(cfg["sample_rate"])
    marker_period = float(cfg["marker_period_s"])
    total = int(float(duration_s) * sr)
    out = [0] * total

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    tmp.close()
    try:
        count = int(float(duration_s) // marker_period)
        for idx in range(count):
            sec_mod = (int(start_epoch_s) + idx) & 0xFFFF
            make_v3_marker_wav(sec_mod, tmp.name, cfg=cfg)

            with wave.open(tmp.name, "rb") as w:
                frames = w.readframes(w.getnframes())
                samples = [int.from_bytes(frames[i:i+2], "little", signed=True) for i in range(0, len(frames), 2)]

            start = int(idx * marker_period * sr)
            for i, v in enumerate(samples):
                if start + i < total:
                    out[start + i] += v
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for v in out:
            v = max(-32767, min(32767, int(v)))
            frames += int(v).to_bytes(2, "little", signed=True)
        w.writeframes(frames)

def make_marker_wav(duration_s, path, cfg=None):
    # Compatibility/audio-test: one clean V3.1 session WAV from current + 2s.
    start_epoch = math.floor(time.time() + 2.0) + 1
    make_v3_session_wav(float(duration_s), path, int(start_epoch), cfg=cfg)

def frange(start, stop, step):
    x = start
    while x < stop:
        yield x
        x += step

def play_wav(path):
    # -q quiet, default ALSA output; user can set default device with raspi-config/alsamixer
    subprocess.run(["aplay", "-q", str(path)], check=False)

def cached_session_wav(duration_s):
    # Use pre-generated WAVs so START is immediate.
    # Installer ships 5/10/20/30/60 second files.
    d = int(round(float(duration_s)))
    path = AUDIO_DIR / f"session_{d}s.wav"
    if path.exists():
        return path
    # Fallback only for unusual durations. This is slower, but happens before PTT.
    fallback = AUDIO_DIR / f"session_{d}s.wav"
    make_marker_wav(float(duration_s), fallback)
    return fallback



def continuous_audio_worker():
    """
    Continuous V3.3 audio generator.

    Runs without PTT. Keeps the RPi audio output continuously carrying
    BIKEN V3 GPS-second markers every second. RF transmission happens only
    when PTT is enabled by normal PRATI/PTT control.
    """
    sr = int(config["sample_rate"])
    proc = subprocess.Popen(
        ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(sr), "-c", "1"],
        stdin=subprocess.PIPE
    )

    write_state(audio_stream=True, audio_stream_started=now(),
                beacon_format="BIKEN_V3_7_GPS_FIELDS_CONT_AUDIO_GPSSEC_CRC8")

    try:
        # Align first generated second to the next integer second.
        next_epoch = math.floor(time.time()) + 1

        # Write silence until the first whole second boundary so the first marker
        # starts cleanly at the beginning of generated PCM second.
        while time.time() < next_epoch:
            if continuous_audio_stop_event.is_set():
                break
            time.sleep(0.002)

        while not continuous_audio_stop_event.is_set():
            gps_epoch = int(next_epoch)
            sec_mod = gps_epoch & 0xFFFF
            bits = biken_v3_bits(sec_mod)
            pcm = pcm_for_bits_1s(bits)

            write_state(audio_stream=True,
                        audio_stream_last_marker_gps_second_mod65536=sec_mod,
                        audio_stream_last_marker_utc=gps_epoch)

            if proc.stdin:
                proc.stdin.write(pcm)
                proc.stdin.flush()

            next_epoch += 1
            # We do not sleep here for exact playback time; aplay consumes the
            # 1-second PCM block at audio rate. The next write naturally queues.
            # This keeps audio continuous even if Python wakes slightly late.

    except Exception as e:
        write_state(audio_stream=False, audio_stream_error=str(e))
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=1)
        except Exception:
            pass
        write_state(audio_stream=False, audio_stream_stopped=now())


def start_continuous_audio():
    global continuous_audio_thread
    with continuous_audio_lock:
        if continuous_audio_thread and continuous_audio_thread.is_alive():
            return {"ok": True, "already_running": True}
        continuous_audio_stop_event.clear()
        continuous_audio_thread = threading.Thread(target=continuous_audio_worker, daemon=True)
        continuous_audio_thread.start()
        return {"ok": True}


def stop_continuous_audio():
    continuous_audio_stop_event.set()
    write_state(audio_stream=False, audio_stream_stopped=now())
    return {"ok": True}


def is_continuous_audio_running():
    return bool(continuous_audio_thread and continuous_audio_thread.is_alive())

def run_biken(duration_s, mode="manual"):
    """
    BIKEN V3.7 GPS-aligned PTT.

    If CONT AUDIO is ON, PRATI waits for the next whole GPS/UTC second before
    opening PTT. This prevents RF starting in the middle of a marker.
    """
    global worker
    if not run_lock.acquire(blocking=False):
        return {"ok": False, "error": "already_running"}

    try:
        stop_event.clear()
        max_tx = float(config["max_tx_s"])
        duration_s = float(duration_s)
        if duration_s <= 0:
            duration_s = float(config["default_duration_s"])
        if duration_s > max_tx:
            duration_s = max_tx

        pre = float(config["pre_roll_s"])
        post = float(config["post_roll_s"])

        if is_continuous_audio_running():
            planned_start_epoch = math.floor(time.time()) + 1
            write_state(running=True, mode="continuous_audio_wait_next_gps_second",
                        started=now(), duration_s=duration_s, error=None, ptt=False,
                        beacon_format="BIKEN_V3_7_GPS_FIELDS_PTT_CONT_AUDIO",
                        planned_start_epoch=int(planned_start_epoch),
                        first_marker_gps_second_mod65536=int(planned_start_epoch) & 0xFFFF)

            while time.time() < planned_start_epoch:
                if stop_event.is_set():
                    break
                time.sleep(0.002)

            if not stop_event.is_set():
                ptt(True)
                t0 = time.time()
                write_state(running=True, mode="continuous_audio_ptt", ptt=True,
                            started=now(), duration_s=duration_s,
                            first_marker_gps_second_mod65536=int(planned_start_epoch) & 0xFFFF,
                            last_marker_utc=int(planned_start_epoch))
                last_idx = -1
                while time.time() - t0 < duration_s:
                    if stop_event.is_set():
                        break
                    elapsed = max(0.0, time.time() - t0)
                    idx = int(elapsed) + 1
                    if idx != last_idx:
                        gps_epoch = int(planned_start_epoch) + int(elapsed)
                        write_state(running=True, mode="continuous_audio_ptt", ptt=True,
                                    last_marker_index=idx,
                                    last_marker_gps_second_mod65536=gps_epoch & 0xFFFF,
                                    last_marker_utc=gps_epoch)
                        last_idx = idx
                    time.sleep(0.02)

            ptt(False)
            write_state(running=False, mode="idle", stopped=now(), ptt=False)
            return {"ok": True}

        # Normal live generation mode, no pre-recorded audio.
        planned_start_epoch = math.floor(time.time() + pre) + 1
        write_state(running=True, mode=mode, started=now(), duration_s=duration_s, error=None,
                    beacon_format="BIKEN_V3_6_LIVE_GPSSEC_CRC8",
                    planned_start_epoch=int(planned_start_epoch),
                    first_marker_gps_second_mod65536=int(planned_start_epoch) & 0xFFFF)

        ptt(True)
        while time.time() < planned_start_epoch:
            if stop_event.is_set():
                break
            time.sleep(0.002)

        if not stop_event.is_set():
            play_v3_live_stream(duration_s, int(planned_start_epoch), mode=mode)

        t1 = time.time()
        while time.time() - t1 < post:
            if stop_event.is_set():
                break
            time.sleep(0.02)

        ptt(False)
        write_state(running=False, mode="idle", stopped=now(), ptt=False)

    except Exception as e:
        ptt(False)
        write_state(running=False, mode="error", error=str(e), ptt=False)
    finally:
        try:
            run_lock.release()
        except Exception:
            pass

    return {"ok": True}

def start_async(duration_s=None, mode="web"):
    global worker
    if duration_s is None:
        duration_s = config["default_duration_s"]
    if worker and worker.is_alive():
        return {"ok": False, "error": "already_running"}
    worker = threading.Thread(target=run_biken, args=(float(duration_s), mode), daemon=True)
    worker.start()
    return {"ok": True, "duration_s": float(duration_s)}

def stop_now():
    stop_event.set()
    ptt(False)
    write_state(running=False, manual_ptt=False, mode="stopped_by_user", stopped=now(), ptt=False)
    return {"ok": True}

def audio_test(duration_s):
    """
    Audio test for BIKEN V3.3 live generator.
    No PTT, no pre-recorded session WAV.
    """
    stop_event.clear()
    start_epoch = math.floor(time.time()) + 1
    while time.time() < start_epoch:
        time.sleep(0.002)
    play_v3_live_stream(float(duration_s), int(start_epoch), mode="audio_test")
    return {"ok": True}



def audio_test_async(duration_s):
    def _job():
        try:
            write_state(audio_test_running=True, mode="audio_test")
            audio_test(duration_s)
        finally:
            write_state(audio_test_running=False, mode="idle")
    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "duration_s": float(duration_s)}

def ptt_manual_on():
    ptt(True)
    write_state(manual_ptt=True, ptt=True, mode="manual_ptt_on")
    return {"ok": True, "ptt": True}

def ptt_manual_off():
    ptt(False)
    write_state(manual_ptt=False, ptt=False, mode="idle")
    return {"ok": True, "ptt": False}


def ptt_test_async(seconds):
    def _job():
        try:
            ptt(True)
            write_state(ptt=True, mode="ptt_test", ptt_test_running=True)
            time.sleep(float(seconds))
        finally:
            ptt(False)
            write_state(ptt=False, mode="idle", ptt_test_running=False)
    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "seconds": float(seconds)}

def ptt_test(seconds):
    ptt(True)
    time.sleep(float(seconds))
    ptt(False)
    return {"ok": True}

HTML = r"""<!doctype html>
<html lang="sr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BIKEN-1 V3.4</title>
<style>
:root{
  --bg:#050d16; --panel:#0b1d2f; --line:#1f4b6a; --cyan:#00d5ff;
  --green:#32f27a; --orange:#ffae28; --red:#ff4242; --blue:#2377ee;
  --text:#e9f3ff; --muted:#b8c5d8; --off:#7e8ba1;
}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--text);font-family:Arial,Helvetica,sans-serif;background:
 radial-gradient(circle at 50% 0%,rgba(0,213,255,.14),transparent 28%),
 linear-gradient(180deg,#030912,#06111d 48%,#040913);overflow-x:hidden}
.wrap{width:min(1420px,96vw);margin:0 auto;padding:12px 0 16px}
.top{display:grid;grid-template-columns:1fr auto;align-items:center;gap:16px;margin-bottom:10px}
.brand{display:flex;gap:16px;align-items:center}.logo{width:64px;color:var(--cyan);filter:drop-shadow(0 0 15px rgba(0,213,255,.4))}
.logo svg{width:62px;height:62px}h1{margin:0;font-size:44px;letter-spacing:3px;line-height:.95;text-shadow:0 0 22px rgba(255,255,255,.22)}
.subtitle{font-size:17px;color:var(--cyan);font-weight:900;margin-top:6px}.subline{font-size:13px;color:var(--muted);font-weight:700;margin-top:6px}
.clock{min-width:142px;text-align:center;padding:10px 14px;border:1px solid var(--line);border-radius:10px;background:linear-gradient(180deg,rgba(16,40,61,.9),rgba(5,13,22,.9))}
.clock .time{font-size:20px;font-weight:900}.clock .date{font-size:12px;color:#c6d0e4;margin-top:4px}
.card{border:1px solid var(--line);border-radius:9px;background:linear-gradient(180deg,rgba(15,37,57,.94),rgba(6,18,31,.95));box-shadow:inset 0 0 45px rgba(0,213,255,.04),0 8px 28px rgba(0,0,0,.2)}
.statusbar{display:grid;grid-template-columns:repeat(6,1fr);overflow:hidden;margin-bottom:10px}
.statusitem{padding:14px 18px;border-right:1px solid var(--line);min-height:86px}.statusitem:last-child{border-right:0}
.label{font-size:12px;letter-spacing:.5px;text-transform:uppercase;color:#c9d4e8}.big{font-size:22px;margin-top:7px;color:#cad4e8}.big.ok{color:var(--green)}.big.warn{color:var(--orange)}.big.bad{color:var(--red)}
.mini{font-size:11px;color:#c8d3e6;margin-top:7px}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--off);margin-left:7px;box-shadow:0 0 10px currentColor}
.dot.on{background:var(--green);color:var(--green)}.dot.tx{background:var(--orange);color:var(--orange)}.dot.bad{background:var(--red);color:var(--red)}
.main{display:grid;grid-template-columns:1.05fr 1.75fr 1.32fr;gap:10px;margin-bottom:10px}.stack{display:grid;gap:10px}
.box{padding:18px}.panel-title{margin:0 0 12px;font-size:18px;font-weight:900;color:var(--cyan);text-transform:uppercase}
.testbtn{width:100%;height:58px;margin:7px 0;border:1px solid #2e5a79;border-radius:8px;color:white;background:linear-gradient(180deg,rgba(18,48,74,.95),rgba(8,24,40,.95));display:flex;align-items:center;gap:13px;text-align:left;padding:0 16px;cursor:pointer;position:relative}
.testbtn:hover{border-color:var(--cyan);box-shadow:0 0 20px rgba(0,213,255,.18)}.ico{font-size:27px;color:var(--cyan);min-width:36px;text-align:center}.testname{font-size:15px;font-weight:900}.testsub{font-size:12px;margin-top:4px;color:white}
.lamp{position:absolute;right:14px;top:18px;width:16px;height:16px;border-radius:50%;background:var(--off);box-shadow:0 0 10px currentColor}.lamp.on{background:var(--green);color:var(--green)}.lamp.tx{background:var(--orange);color:var(--orange)}.lamp.bad{background:var(--red);color:var(--red)}
.info-row{display:grid;grid-template-columns:1fr auto;gap:10px;margin:10px 0;color:#cdd7e9;font-size:14px}.code{background:#06111d;border:1px solid #254e68;border-radius:6px;padding:7px 9px;color:var(--green);font-family:Consolas,monospace;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.badge{border:1px solid #267f58;border-radius:6px;color:var(--green);font-size:11px;padding:3px 8px;background:rgba(54,242,123,.08)}
.control{padding:18px;text-align:center;position:relative;overflow:hidden;border-color:#006aa0}.control:before{content:"";position:absolute;inset:36px 70px 118px;border:1px solid rgba(0,213,255,.18);border-radius:50%;box-shadow:0 0 60px rgba(0,213,255,.08)}
.control-title{font-size:19px;color:var(--cyan);font-weight:900;text-transform:uppercase;margin-bottom:20px}.start-circle{width:158px;height:158px;margin:0 auto 25px;border-radius:50%;border:3px solid #8ef7d5;background:radial-gradient(circle at 40% 30%,#6df68e,#1aa846 70%);box-shadow:0 0 0 10px rgba(0,213,255,.10),0 0 45px rgba(54,242,123,.35),inset 0 -20px 40px rgba(0,0,0,.28);display:flex;flex-direction:column;align-items:center;justify-content:center;cursor:pointer;position:relative;z-index:2}
.play{font-size:35px}.start-main{font-size:28px;font-weight:900}.start-sub{font-size:15px;font-weight:900}.presets{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;position:relative;z-index:2}.preset{border:1px solid var(--green);border-radius:8px;background:rgba(0,40,22,.65);color:var(--green);padding:10px 4px;cursor:pointer}.preset strong{display:block;font-size:30px}.preset span{font-size:12px;font-weight:900}
.stop{margin:16px auto 0;width:min(360px,90%);height:54px;border-radius:8px;border:1px solid var(--red);background:linear-gradient(180deg,rgba(174,45,45,.78),rgba(83,10,14,.82));color:#ffd2d2;font-size:21px;font-weight:900;cursor:pointer;position:relative;z-index:2}
.pttrow{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}.pttbtn{height:42px;border-radius:8px;border:1px solid var(--orange);background:rgba(255,174,40,.12);color:#ffd28a;font-weight:900;cursor:pointer}.pttbtn.off{border-color:var(--red);background:rgba(255,66,66,.12);color:#ffb6b6}
.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 12px}.field label{display:block;font-size:12px;color:#c7d3e8;margin-bottom:5px}input{width:100%;height:31px;border-radius:6px;border:1px solid #315c78;background:#06111d;color:white;font-size:14px;font-weight:700;padding:5px 10px;outline:none}input:focus{border-color:var(--cyan);box-shadow:0 0 0 2px rgba(0,213,255,.15)}
.save{width:100%;height:41px;margin-top:13px;border:1px solid #33a3ff;border-radius:7px;background:linear-gradient(180deg,#2c86ff,#1458c8);color:white;font-weight:900;font-size:15px;cursor:pointer}.note{font-size:11px;color:#c2cee4;margin-top:10px}
.bottom{display:grid;grid-template-columns:1fr 1fr 1fr 1.15fr;gap:10px}.smallpanel{padding:16px 20px;min-height:148px}.green{color:var(--green)}.orange{color:var(--orange)}.cyan{color:var(--cyan)}.red{color:var(--red)}.kv{display:grid;grid-template-columns:1fr auto;gap:6px 14px;color:#cbd6e7;font-size:13px}.kv div:nth-child(even){color:#e8f1ff}.logline{font-size:13px;color:#cbd6e7;line-height:1.55}.toast{position:fixed;right:18px;bottom:18px;background:#10283d;border:1px solid var(--cyan);padding:12px 16px;border-radius:8px;color:white;display:none}
body.running .start-circle{background:radial-gradient(circle at 40% 30%,#ffd36a,#d87800 70%);box-shadow:0 0 0 12px rgba(255,174,40,.12),0 0 55px rgba(255,174,40,.35)}
@media(max-width:1180px){.main{grid-template-columns:1fr}.bottom{grid-template-columns:1fr 1fr}.statusbar{grid-template-columns:1fr 1fr 1fr}}@media(max-width:700px){.top{grid-template-columns:1fr}.logo{display:none}h1{font-size:36px}.presets,.settings-grid,.bottom{grid-template-columns:1fr 1fr}.statusbar{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top"><div class="brand"><div class="logo"><svg viewBox="0 0 64 64" fill="none"><path d="M32 33v22" stroke="currentColor" stroke-width="4" stroke-linecap="round"/><circle cx="32" cy="24" r="6" fill="currentColor"/><path d="M19 34a18 18 0 0 1 0-24M45 10a18 18 0 0 1 0 24M10 43a31 31 0 0 1 0-42M54 1a31 31 0 0 1 0 42" stroke="currentColor" stroke-width="4" stroke-linecap="round"/></svg></div><div><h1 id="titleName">BIKEN-1</h1><div class="subtitle">GPS PPS LIVE/CONT RF GPS-SECOND BEACON</div><div class="subline">RPI3A+ • GPS/PPS • Motorola CM140 • RF Goniometar</div></div></div><div class="clock"><div class="time" id="clockTime">--:--:--</div><div class="date" id="clockDate">--.--.----</div></div></div>
  <div class="card statusbar">
    <div class="statusitem"><div class="label">GPS LOCK</div><div class="big" id="gpsLockBig">CHECKING <span class="dot"></span></div><div class="mini">Satellites: <span id="satellites">—</span></div></div>
    <div class="statusitem"><div class="label">PPS LOCK</div><div class="big" id="ppsLockBig">CHECKING <span class="dot"></span></div><div class="mini">Source: <span id="ppsSource">—</span></div></div>
    <div class="statusitem"><div class="label">AUDIO STREAM</div><div class="big" id="audioStreamBig">OFF <span class="dot"></span></div><div class="mini" id="audioStreamMini">Generate on demand</div></div>
    <div class="statusitem"><div class="label">PTT (GPIO17)</div><div class="big" id="pttBig">OFF <span class="dot"></span></div><div class="mini">CM140 rear accessory</div></div>
    <div class="statusitem"><div class="label">MODE</div><div class="big" id="modeBig">IDLE <span class="dot"></span></div><div class="mini" id="modeMini">Ready</div></div>
    <div class="statusitem"><div class="label">SESSION</div><div class="big" id="sessionBig">—</div><div class="mini" id="sessionMini">No active run</div></div>
  </div>
  <div class="main">
    <div class="stack">
      <div class="card box"><div class="panel-title">Tests / Modes</div>
        <button class="testbtn" onclick="audioTest()"><div class="ico">◖</div><div><div class="testname">AUDIO TEST</div><div class="testsub">5 s signature</div></div><span class="lamp" id="lampAudioTest"></span></button>
        <button class="testbtn" onclick="pttTest()"><div class="ico orange">⚡</div><div><div class="testname">PTT TEST</div><div class="testsub">GPIO17 / 2 s</div></div><span class="lamp" id="lampPttTest"></span></button>
        <button class="testbtn" onclick="audioStreamToggle()"><div class="ico green">∞</div><div><div class="testname">CONT AUDIO ON/OFF</div><div class="testsub" id="audioStreamSub">OFF - generate on demand</div></div><span class="lamp" id="lampAudioStream"></span></button>
      </div>
      <div class="card box"><div class="panel-title">RF Beacon Info</div>
        <div class="info-row"><span>Preamble</span><span class="badge">BARKER-13</span></div><div class="code">1111100110101</div>
        <div class="info-row"><span>Marker Format</span><span>V3.7 GPS FIELDS</span></div><div class="code">BARKER13 + GPSSEC16 + CRC8 + BARKER7</div>
        <div class="info-row"><span>Marker payload</span><span class="cyan">GPS second mod65536</span></div>
        <div class="info-row"><span>Markers / Second</span><span class="cyan">1.000</span></div>
        <div class="info-row"><span>Audio</span><span class="cyan" id="audioInfo">1200 / 1800 Hz</span></div>
      </div>
    </div>
    <div class="card control"><div class="control-title">Tracking Control</div><div class="start-circle" onclick="startDefault()"><div class="play">▶</div><div class="start-main">START</div><div class="start-sub">TRACKING</div></div>
      <div class="presets"><button class="preset" onclick="start(10)"><strong>10</strong><span>SECONDS</span></button><button class="preset" onclick="start(20)"><strong>20</strong><span>SECONDS</span></button><button class="preset" onclick="start(30)"><strong>30</strong><span>SECONDS</span></button><button class="preset" onclick="start(60)"><strong>60</strong><span>SECONDS</span></button></div>
      <button class="stop" onclick="stop()">■ STOP / PTT OFF</button>
      <div class="pttrow"><button class="pttbtn" onclick="pttOn()">PTT ON</button><button class="pttbtn off" onclick="pttOff()">PTT OFF</button></div>
    </div>
    <div class="card box"><div class="panel-title">Settings</div><div class="settings-grid">
      <div class="field"><label>Naziv</label><input id="name"></div><div class="field"><label>Default trajanje (s)</label><input id="default_duration_s" type="number" step="1"></div>
      <div class="field"><label>Pre-roll (s)</label><input id="pre_roll_s" type="number" step="0.1"></div><div class="field"><label>Post-roll (s)</label><input id="post_roll_s" type="number" step="0.1"></div>
      <div class="field"><label>Max TX (s)</label><input id="max_tx_s" type="number" step="1"></div><div class="field"><label>Audio level (0.0-1.0)</label><input id="audio_level" type="number" step="0.05"></div>
      <div class="field"><label>Tone A (Hz)</label><input id="tone_a_hz" type="number"></div><div class="field"><label>Tone B (Hz)</label><input id="tone_b_hz" type="number"></div>
      <div class="field"><label>PTT GPIO BCM</label><input id="ptt_gpio_bcm" type="number"></div><div class="field"><label>Web port</label><input id="web_port" type="number"></div>
      </div><button class="save" onclick="saveCfg()">▣ SAČUVAJ PODEŠAVANJA</button><div class="note">Polja se ne osvežavaju dok kucaš. Enter/Save čuva.</div></div>
  </div>
  <div class="bottom">
    <div class="card smallpanel"><div class="panel-title green">GPS Status</div><div class="kv"><div>Satellites</div><div id="gpsSat">—</div><div>Fix Type</div><div id="gpsFix">—</div><div>HDOP</div><div id="gpsHdop">—</div><div>Latitude</div><div id="gpsLat">—</div><div>Longitude</div><div id="gpsLon">—</div><div>Altitude</div><div id="gpsAlt">—</div></div></div>
    <div class="card smallpanel"><div class="panel-title green">PPS Status</div><div class="kv"><div>PPS Source</div><div id="ppsSource2">—</div><div>PPS State</div><div id="ppsState">—</div><div>Last Pulse</div><div id="lastPulse">—</div></div></div>
    <div class="card smallpanel"><div class="panel-title orange">Radio / Hardware</div><div class="kv"><div>PTT GPIO (BCM)</div><div id="gpioInfo">17</div><div>PTT State</div><div id="pttInfo">OFF</div><div>Audio Stream</div><div id="audioStreamInfo">OFF</div><div>Audio Output</div><div>OK</div><div>CM140 Interface</div><div>READY</div></div></div>
    <div class="card smallpanel"><div class="panel-title">Session Log</div><div class="logline">Last Run: <span id="lastRun">—</span></div><div class="logline">Duration: <span id="lastDuration">—</span></div><div class="logline">Remaining: <span id="remaining">—</span></div><div class="logline">Marker: <span id="markerInfo">—</span></div><div class="logline">Status: <span id="lastStatus">Ready</span></div><div class="logline">Updated: <span id="updated">—</span></div></div>
  </div>
</div><div class="toast" id="toast"></div>
<script>
let cfg={}, editing=new Set(), saveInProgress=false, lastAction='';
const keys=['name','default_duration_s','pre_roll_s','post_roll_s','max_tx_s','audio_level','tone_a_hz','tone_b_hz','ptt_gpio_bcm','web_port'];
function pad(n){return String(n).padStart(2,'0')} function tickClock(){const d=new Date();document.getElementById('clockTime').textContent=pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());document.getElementById('clockDate').textContent=pad(d.getDate())+'.'+pad(d.getMonth()+1)+'.'+d.getFullYear()+'.'} setInterval(tickClock,1000);tickClock();
function toast(t){const el=document.getElementById('toast');el.textContent=t;el.style.display='block';setTimeout(()=>el.style.display='none',2200)}
async function api(path,opts={}){const r=await fetch(path,opts);return await r.json()}
function setText(id,v){const el=document.getElementById(id);if(el)el.textContent=(v===null||v===undefined||v==='')?'—':v}
function fmtCoord(v){return(v===null||v===undefined)?'—':Number(v).toFixed(6)} function fmtAlt(v){return(v===null||v===undefined)?'—':Number(v).toFixed(1)+' m'}
function lamp(id,on,cls='on'){const el=document.getElementById(id);if(!el)return;el.className='lamp '+(on?cls:'')}
function bindInputs(){keys.forEach(k=>{const el=document.getElementById(k);if(!el)return;el.addEventListener('focus',()=>editing.add(k));el.addEventListener('input',()=>editing.add(k));el.addEventListener('blur',()=>setTimeout(()=>editing.delete(k),1200));el.addEventListener('keydown',e=>{if(e.key==='Enter'){saveCfg();el.blur()}})})} bindInputs();
function remainingFrom(s){ if(!s.running||!s.started||!s.duration_s)return '—'; const t=Date.parse(String(s.started).replace(' ','T')); if(isNaN(t)) return '—'; const rem=Math.max(0, Math.ceil(Number(s.duration_s)-((Date.now()-t)/1000))); return rem+' s';}
async function refresh(){let s;try{s=await api('/api/state')}catch(e){return} cfg=s.config||{}; const gps=s.gps||{}; const ptt=!!s.ptt, running=!!s.running, stream=!!(s.audio_stream_thread_alive||s.audio_stream);
document.body.classList.toggle('running',running); setText('titleName',cfg.name||s.name||'BIKEN-1');
const gpsLocked=!!gps.lock; document.getElementById('gpsLockBig').className='big '+(gpsLocked?'ok':(gps.available?'warn':'bad')); document.getElementById('gpsLockBig').innerHTML=(gpsLocked?'LOCKED':(gps.available?'NO FIX':'NO DATA'))+' <span class="dot '+(gpsLocked?'on':(gps.available?'':'bad'))+'"></span>';
setText('satellites',gps.satellites);setText('gpsSat',gps.satellites);setText('gpsFix',gps.fix_type||(gpsLocked?'LOCKED':'NO DATA'));setText('gpsHdop',gps.hdop);setText('gpsLat',fmtCoord(gps.lat));setText('gpsLon',fmtCoord(gps.lon));setText('gpsAlt',fmtAlt(gps.alt));
const pps=!!gps.pps; document.getElementById('ppsLockBig').className='big '+(pps?'ok':'warn'); document.getElementById('ppsLockBig').innerHTML=(pps?(gps.pps_state||'READY'):'NO PPS')+' <span class="dot '+(pps?'on':'')+'"></span>'; setText('ppsSource',gps.pps_source);setText('ppsSource2',gps.pps_source);setText('ppsState',gps.pps_state);setText('lastPulse',gps.last_pulse||s.updated);
document.getElementById('audioStreamBig').className='big '+(stream?'ok':'');document.getElementById('audioStreamBig').innerHTML=(stream?'ON':'OFF')+' <span class="dot '+(stream?'on':'')+'"></span>';setText('audioStreamMini',stream?'Live GPSSEC audio':'Generate on demand');setText('audioStreamSub',stream?'ON - live GPSSEC audio':'OFF - generate on demand');setText('audioStreamInfo',stream?'ON':'OFF');lamp('lampAudioStream',stream,'on');
document.getElementById('pttBig').className='big '+(ptt?'warn':'');document.getElementById('pttBig').innerHTML=(ptt?'ON':'OFF')+' <span class="dot '+(ptt?'tx':'')+'"></span>'; setText('pttInfo',ptt?'ON':'OFF');lamp('lampPttTest',ptt,'tx');
document.getElementById('modeBig').innerHTML=(running?'TX ACTIVE':String(s.mode||'idle').toUpperCase())+' <span class="dot '+(running?'tx':'')+'"></span>';setText('modeMini',running?'Beacon session running':'Ready');
setText('sessionBig',running?((s.duration_s||cfg.default_duration_s||20)+' s'):'—');setText('sessionMini',running?'Active run':'No active run');setText('gpioInfo',cfg.ptt_gpio_bcm??'17');setText('audioInfo',(cfg.tone_a_hz??1200)+' / '+(cfg.tone_b_hz??1800)+' Hz');
setText('lastRun',s.started||s.stopped||'—');setText('lastDuration',s.duration_s?s.duration_s+' s':'—');setText('remaining',remainingFrom(s));setText('markerInfo',s.last_marker_gps_second_mod65536!==undefined&&s.last_marker_gps_second_mod65536!==null?('GPSSEC '+s.last_marker_gps_second_mod65536):'—');setText('lastStatus',s.error?('ERROR: '+s.error):(running?'Running':'Ready'));setText('updated',s.updated||'—');
lamp('lampAudioTest',lastAction==='audio', 'on');
keys.forEach(k=>{const el=document.getElementById(k);if(!el||editing.has(k)||saveInProgress)return;const val=cfg[k];if(val!==undefined&&String(el.value)!==String(val))el.value=val})}
async function start(sec){lastAction='';await api('/api/start?duration='+sec);toast('BIKEN START '+sec+' s');refresh()}
async function startDefault(){const d=Number(document.getElementById('default_duration_s').value||cfg.default_duration_s||20);await start(d)}
async function stop(){await api('/api/stop');toast('PTT OFF');refresh()}
async function audioTest(){lastAction='audio';await api('/api/audio_test?duration=5');toast('Audio test');setTimeout(()=>{lastAction='';refresh()},1600);refresh()}
async function pttTest(){await api('/api/ptt_test?seconds=2');toast('PTT test');refresh()}
async function pttOn(){await api('/api/ptt/on');toast('PTT ON');refresh()}
async function pttOff(){await api('/api/ptt/off');toast('PTT OFF');refresh()}
async function audioStreamToggle(){const r=await api('/api/audio_stream/toggle');toast('Continuous audio toggled');refresh()}
async function saveCfg(){saveInProgress=true;const out={};keys.forEach(k=>{const el=document.getElementById(k);if(!el)return;out[k]=(k==='name')?el.value:Number(el.value)});const res=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(out)});saveInProgress=false;editing.clear();toast(res.ok?'Podešavanja sačuvana':'Greška pri čuvanju');refresh()}
setInterval(refresh,1500);refresh();
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def _json(self, obj):
        b = json.dumps(obj, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _html(self, html):
        b = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self._html(HTML)
        if u.path == "/api/state":
            return self._json(read_state())
        if u.path == "/api/start":
            d = float(q.get("duration", [config["default_duration_s"]])[0])
            return self._json(start_async(d, "web"))
        if u.path == "/api/stop":
            return self._json(stop_now())
        if u.path == "/api/audio_test":
            d = float(q.get("duration", [5])[0])
            return self._json(audio_test_async(d))
        if u.path == "/api/ptt_test":
            s = float(q.get("seconds", [2])[0])
            return self._json(ptt_test_async(s))
        if u.path == "/api/ptt/on":
            return self._json(ptt_manual_on())
        if u.path == "/api/ptt/off":
            return self._json(ptt_manual_off())
        if u.path == "/api/audio_stream/start":
            return self._json(start_continuous_audio())
        if u.path == "/api/audio_stream/stop":
            return self._json(stop_continuous_audio())
        if u.path == "/api/audio_stream/toggle":
            if is_continuous_audio_running():
                return self._json(stop_continuous_audio())
            return self._json(start_continuous_audio())
        self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/config":
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n)
            try:
                patch = json.loads(body.decode())
                config.update(patch)
                save_config(config)
                return self._json({"ok": True, "config": config})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        self.send_error(404)

def web():
    write_state(running=False, ptt=False, mode="idle", started=None)
    host = config["web_host"]
    port = int(config["web_port"])
    print(f"BIKEN web panel: http://{host}:{port}")
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.serve_forever()

def main():
    global config
    config = load_config()
    gpio_init()
    signal.signal(signal.SIGTERM, lambda s,f: (gpio_cleanup(), sys.exit(0)))
    signal.signal(signal.SIGINT, lambda s,f: (gpio_cleanup(), sys.exit(0)))

    p = argparse.ArgumentParser()
    p.add_argument("--web", action="store_true")
    p.add_argument("--run", type=float)
    p.add_argument("--audio-test", action="store_true")
    p.add_argument("--ptt-test", type=float)
    p.add_argument("--stop", action="store_true")
    args = p.parse_args()

    try:
        if args.web:
            return web()
        if args.stop:
            stop_now()
            return
        if args.audio_test:
            return audio_test(args.run or 5)
        if args.ptt_test is not None:
            return ptt_test(args.ptt_test)
        if args.run:
            return run_biken(args.run, "cli")
        print("BIKEN-1")
        print("CLI:")
        print("  biken --audio-test --run 5")
        print("  biken --ptt-test 2")
        print("  biken --run 20")
        print("  biken --web")
    finally:
        if not args.web:
            gpio_cleanup()

if __name__ == "__main__":
    main()
