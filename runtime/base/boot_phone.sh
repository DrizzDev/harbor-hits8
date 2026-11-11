#!/bin/bash
# Entrypoint of the android service: boot the emulator, install the twin apps, then hand over to the phone service,
# which seeds this task and writes /state/ready (the compose healthcheck) once the phone is ready for the agent.
set -euo pipefail
LOG=/state/boot.log; mkdir -p /state; : > "$LOG"
emulator -avd phone -port 5554 -no-window -no-audio -no-boot-anim -no-snapshot -accel on \
  -gpu swiftshader_indirect -memory 4096 -cores 2 >> /state/emulator.log 2>&1 &
adb start-server >> "$LOG" 2>&1
for _ in $(seq 1 180); do
  [ "$(adb -s emulator-5554 shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ] && break; sleep 5
done
[ "$(adb -s emulator-5554 shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ] || { echo "emulator did not boot" >> "$LOG"; exit 1; }
for _ in $(seq 1 24); do adb -s emulator-5554 shell pm path android >/dev/null 2>&1 && break; sleep 5; done
for apk in /opt/drizz/apks/*.apk; do
  pkg=$(basename "$apk" .apk)
  for attempt in 1 2 3; do
    adb -s emulator-5554 install -r -g "$apk" >> "$LOG" 2>&1 && adb -s emulator-5554 shell "run-as $pkg true" && break
    echo "install $pkg attempt $attempt failed" >> "$LOG"; sleep 10
  done
  adb -s emulator-5554 shell "run-as $pkg true" || { echo "$pkg not installed or not debuggable" >> "$LOG"; exit 1; }
done
for p in com.google.android.gm com.google.android.calendar; do adb -s emulator-5554 shell pm disable-user --user 0 "$p" >/dev/null 2>&1 || true; done
echo "phone booted $(date -u +%FT%TZ)" >> "$LOG"
exec python3 /opt/drizz/phone_service.py --task "$TASK_FILE" --max-steps "$MAX_STEPS" --serial emulator-5554 --state /state
