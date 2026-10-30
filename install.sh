#!/usr/bin/env bash
set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
    echo "ERROR: Run with sudo:"
    echo "  sudo bash install.sh"
    exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="/opt/biken-v4"
SERVICE="/etc/systemd/system/biken-v4.service"
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "============================================================"
echo " BIKEN + OBRENKA INSTALLER"
echo " Version: $(cat "$SRC/VERSION" 2>/dev/null || echo unknown)"
echo "============================================================"

echo
echo "[1/10] Platform check"

if [ ! -r /etc/os-release ]; then
    echo "ERROR: /etc/os-release missing."
    exit 1
fi

. /etc/os-release

echo "OS: ${PRETTY_NAME:-unknown}"
echo "ARCH: $(uname -m)"

case "$(uname -m)" in
    armv6l|armv7l|aarch64)
        ;;
    *)
        echo "WARNING: This release is intended for Raspberry Pi."
        ;;
esac

if [ "${VERSION_ID:-}" != "12" ]; then
    echo "WARNING: RC1 reference platform is Raspberry Pi OS / Debian 12 Bookworm."
fi

echo
echo "[2/10] Install required packages"

export DEBIAN_FRONTEND=noninteractive

apt-get update

apt-get install -y \
    python3 \
    python3-numpy \
    python3-rpi.gpio \
    alsa-utils \
    chrony \
    gpsd \
    gpsd-clients \
    pps-tools \
    curl \
    ca-certificates

echo
echo "[3/10] Stop previous BIKEN installation if present"

systemctl stop biken-v4.service 2>/dev/null || true

if [ -d "$TARGET" ]; then
    BACKUP="${TARGET}.backup-${STAMP}"
    echo "Existing installation found."
    echo "Backup: $BACKUP"
    mv "$TARGET" "$BACKUP"
fi

echo
echo "[4/10] Create application directories"

install -d -m 0755 \
    "$TARGET" \
    "$TARGET/plugins" \
    "$TARGET/presets" \
    "$TARGET/voice-packs/sr_female_01" \
    "$TARGET/runtime" \
    "$TARGET/logs" \
    "$TARGET/backups"

echo
echo "[5/10] Install BIKEN application"

install -m 0644 "$SRC/app/biken_v4.py" \
    "$TARGET/biken_v4.py"

install -m 0644 "$SRC/app/biken_v37_locked.py" \
    "$TARGET/biken_v37_locked.py"

install -m 0644 "$SRC/app/marker.wav" \
    "$TARGET/marker.wav"

cp -a "$SRC/plugins/." "$TARGET/plugins/"

echo
echo "[6/10] Install Obrenka Voice Pack"

cp -a "$SRC/voice-pack/sr_female_01/." \
    "$TARGET/voice-packs/sr_female_01/"

echo
echo "[7/10] Install SAFE default configuration"

install -m 0644 \
    "$SRC/config/defaults/announcer_config.json" \
    "$TARGET/announcer_config.json"

install -m 0644 \
    "$SRC/config/defaults/config.json" \
    "$TARGET/config.json"

install -m 0644 \
    "$SRC/config/defaults/mode.json" \
    "$TARGET/mode.json"

install -m 0644 \
    "$SRC/config/defaults/plugin_config.json" \
    "$TARGET/plugins/plugin_config.json"

install -m 0644 \
    "$SRC/config/defaults/presets.json" \
    "$TARGET/presets/presets.json"

# Preserve shipped audio factory presets.
if [ -f "$SRC/plugins/audio_presets.json" ]; then
    install -m 0644 \
        "$SRC/plugins/audio_presets.json" \
        "$TARGET/plugins/audio_presets.json"
fi

echo
echo "[8/10] Python / configuration validation"

python3 -m py_compile \
    "$TARGET/biken_v4.py" \
    "$TARGET/biken_v37_locked.py" \
    "$TARGET/plugins/plugin_host.py"

python3 - <<'PY'
import json

files = [
    "/opt/biken-v4/announcer_config.json",
    "/opt/biken-v4/config.json",
    "/opt/biken-v4/mode.json",
    "/opt/biken-v4/plugins/plugin_config.json",
    "/opt/biken-v4/presets/presets.json",
    "/opt/biken-v4/voice-packs/sr_female_01/manifest.json",
]

for f in files:
    with open(f, encoding="utf-8") as h:
        json.load(h)

print("JSON validation: OK")
PY

echo
echo "[9/10] Install and start service"

install -m 0644 \
    "$SRC/systemd/biken-v4.service" \
    "$SERVICE"

systemctl daemon-reload

# Network chrony is the default clock source.
systemctl enable chrony.service >/dev/null 2>&1 || true
systemctl restart chrony.service || true

systemctl enable biken-v4.service
systemctl restart biken-v4.service

echo "Waiting for BIKEN API..."

READY=0

for i in $(seq 1 30); do
    if curl -fsS --max-time 3 \
        http://127.0.0.1:8891/api/v4/state \
        >/dev/null 2>&1
    then
        READY=1
        break
    fi
    sleep 1
done

if [ "$READY" -ne 1 ]; then
    echo
    echo "ERROR: BIKEN API did not become ready."
    systemctl status biken-v4.service --no-pager -l || true
    exit 1
fi

echo
echo "[10/10] Safety verification"

python3 - <<'PY'
import json

with open("/opt/biken-v4/config.json", encoding="utf-8") as f:
    hw=json.load(f)

with open("/opt/biken-v4/announcer_config.json", encoding="utf-8") as f:
    ann=json.load(f)

with open("/opt/biken-v4/plugins/plugin_config.json", encoding="utf-8") as f:
    dsp=json.load(f)

assert hw.get("dry_run_gpio") is True, "dry_run_gpio MUST be true"
assert ann.get("master_enable") is False, "master_enable MUST be false"
assert ann.get("automation") is False, "automation MUST be false"
assert ann.get("time_source") == "AUTO", "time_source MUST be AUTO"
assert dsp.get("master_enabled") is False, "DSP master MUST be bypassed"

print("SAFE START: PASS")
print("PTT GPIO: DRY RUN")
print("RF MASTER: OFF")
print("AUTOMATION: OFF")
print("DSP: BYPASS")
print("TIME SOURCE: AUTO")
PY

echo
echo "============================================================"
echo " INSTALLATION COMPLETE"
echo "============================================================"
echo
echo "Web UI:"
echo "  http://$(hostname -I 2>/dev/null | awk '{print $1}'):8891"
echo
echo "IMPORTANT:"
echo "  RF transmission is DISABLED by default."
echo "  GPIO is in DRY-RUN mode."
echo "  Automation is OFF."
echo "  GPS/PPS is OPTIONAL for Radio Announcer."
echo
echo "Run:"
echo "  sudo bash $SRC/verify.sh"
echo
