#!/usr/bin/env bash

set -u

TARGET="/opt/biken-v4"
FAIL=0
WARN=0

pass() {
    echo "PASS  $*"
}

fail() {
    echo "FAIL  $*"
    FAIL=$((FAIL+1))
}

warn() {
    echo "WARN  $*"
    WARN=$((WARN+1))
}

echo "============================================================"
echo " BIKEN + OBRENKA VERIFY"
echo "============================================================"

echo
echo "--- SERVICE ---"

if systemctl is-active --quiet biken-v4.service; then
    pass "biken-v4.service ACTIVE"
else
    fail "biken-v4.service NOT ACTIVE"
fi

echo
echo "--- PYTHON ---"

if python3 -m py_compile \
    "$TARGET/biken_v4.py" \
    "$TARGET/biken_v37_locked.py" \
    "$TARGET/plugins/plugin_host.py" 2>/dev/null
then
    pass "Python syntax"
else
    fail "Python syntax"
fi

echo
echo "--- SAFE CONFIG ---"

python3 - <<'PY'
import json, sys

with open("/opt/biken-v4/config.json", encoding="utf-8") as f:
    hw=json.load(f)

with open("/opt/biken-v4/announcer_config.json", encoding="utf-8") as f:
    ann=json.load(f)

with open("/opt/biken-v4/plugins/plugin_config.json", encoding="utf-8") as f:
    dsp=json.load(f)

print("dry_run_gpio :", hw.get("dry_run_gpio"))
print("master_enable:", ann.get("master_enable"))
print("automation   :", ann.get("automation"))
print("time_source  :", ann.get("time_source"))
print("DSP master   :", dsp.get("master_enabled"))

safe = (
    hw.get("dry_run_gpio") is True
    and ann.get("master_enable") is False
    and ann.get("automation") is False
)

sys.exit(0 if safe else 1)
PY

if [ $? -eq 0 ]; then
    pass "Fresh-install RF safety"
else
    fail "Fresh-install RF safety"
fi

echo
echo "--- API ---"

TMP="$(mktemp)"

if curl -fsS --max-time 10 \
    http://127.0.0.1:8891/api/v4/state \
    >"$TMP"
then

    python3 - "$TMP" <<'PY'
import json, sys

with open(sys.argv[1], encoding="utf-8") as f:
    s=json.load(f)

print("Version        :", s.get("version"))
print("Active mode    :", s.get("active_mode"))

a=s.get("announcer") or {}
print("Announcer      :", a.get("state"))
print("PTT            :", a.get("ptt"))

vp=s.get("voice_pack") or {}
print("Voice Pack     :", "READY" if vp.get("ready") else "NOT READY")

t=s.get("timing") or {}
print("Timing status  :", t.get("status"))
print("Requested time :", t.get("requested_source"))
print("Selected time  :", t.get("selected_source"))

for w in t.get("warnings") or []:
    print("Timing warning :", w)
PY

    pass "Web API :8891"

else
    fail "Web API :8891"
fi

rm -f "$TMP"

echo
echo "--- VOICE PACK ---"

if [ -f "$TARGET/voice-packs/sr_female_01/manifest.json" ]; then

    COUNT="$(find \
        "$TARGET/voice-packs/sr_female_01" \
        -maxdepth 1 -type f -name '*.wav' | wc -l)"

    pass "Voice Pack manifest"
    echo "      WAV files: $COUNT"

else
    fail "Voice Pack manifest missing"
fi

echo
echo "--- AUDIO ---"

if command -v aplay >/dev/null 2>&1; then

    if aplay -l >/tmp/biken-aplay.txt 2>&1; then
        pass "ALSA playback devices detected"
        sed 's/^/      /' /tmp/biken-aplay.txt
    else
        warn "No ALSA playback device detected"
    fi

else
    fail "aplay missing"
fi

rm -f /tmp/biken-aplay.txt

echo
echo "--- SYSTEM CLOCK ---"

if command -v chronyc >/dev/null 2>&1; then

    if chronyc tracking >/tmp/biken-chrony.txt 2>&1; then

        pass "chrony available"

        REF="$(grep '^Reference ID' /tmp/biken-chrony.txt | head -1 || true)"
        LEAP="$(grep '^Leap status' /tmp/biken-chrony.txt | head -1 || true)"

        echo "      $REF"
        echo "      $LEAP"

        if echo "$LEAP" | grep -q 'Normal'; then
            pass "System clock synchronized"
        else
            warn "System clock not synchronized yet"
        fi

    else
        warn "chronyc tracking failed"
    fi

else
    fail "chrony missing"
fi

rm -f /tmp/biken-chrony.txt

echo
echo "--- OPTIONAL GPS / PPS ---"

if [ -e /dev/pps0 ]; then
    pass "/dev/pps0 detected"
else
    echo "INFO  GPS/PPS not installed - SYSTEM_NTP can be used."
fi

if [ -e /dev/serial0 ]; then
    echo "INFO  /dev/serial0 detected"
else
    echo "INFO  UART GPS not detected"
fi

echo
echo "============================================================"

if [ "$FAIL" -eq 0 ]; then
    echo " BIKEN VERIFY: PASS"
    echo " Warnings: $WARN"
    echo "============================================================"
    exit 0
else
    echo " BIKEN VERIFY: FAIL"
    echo " Failures: $FAIL"
    echo " Warnings: $WARN"
    echo "============================================================"
    exit 1
fi
