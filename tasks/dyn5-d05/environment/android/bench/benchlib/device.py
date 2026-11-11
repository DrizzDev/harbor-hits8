"""Emulator / device access: adb for state and capture, the drizz CLI for agent input.

Screenshots use `adb exec-out screencap`, which needs no UiAutomation session, so the runner can
capture screens while any harness (ARTEMIS, Appium, Drizz) holds the device's UiAutomation.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ADB = shutil.which("adb") or "adb"
DRIZZ = shutil.which("drizz") or "drizz"
EMULATOR = shutil.which("emulator") or "emulator"


class DeviceError(RuntimeError):
    pass


def adb_devices() -> list[str]:
    out = subprocess.run([ADB, "devices"], capture_output=True, text=True, check=True).stdout
    return [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith("device")]


def running_avd(serial: str) -> str | None:
    out = subprocess.run([ADB, "-s", serial, "emu", "avd", "name"], capture_output=True, text=True).stdout
    return out.splitlines()[0].strip() if out.strip() else None


def boot_avd(avd: str, timeout_s: int = 240, extra_args: list[str] | None = None) -> str:
    """Boot `avd` unless it is already running; returns its serial once boot has completed."""
    for serial in adb_devices():
        if serial.startswith("emulator-") and running_avd(serial) == avd:
            Device(serial).wait_boot(timeout_s)
            return serial
    before = set(adb_devices())
    subprocess.Popen([EMULATOR, "-avd", avd, "-no-snapshot-save", "-no-boot-anim", *(extra_args or [])],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        new = [s for s in adb_devices() if s not in before and s.startswith("emulator-")]
        if new:
            Device(new[0]).wait_boot(max(10, int(deadline - time.time())))
            return new[0]
        time.sleep(2)
    raise DeviceError(f"emulator {avd} did not appear within {timeout_s}s")


class Device:
    def __init__(self, serial: str):
        self.serial = serial

    # ---- raw adb -----------------------------------------------------------------------------
    def adb(self, *args: str, check: bool = True, timeout: float = 60, binary: bool = False,
            input: bytes | None = None) -> str | bytes:
        p = subprocess.run([ADB, "-s", self.serial, *args], capture_output=True, timeout=timeout, input=input)
        if check and p.returncode != 0:
            raise DeviceError(f"adb {' '.join(args)} failed: {p.stderr.decode(errors='replace').strip()}")
        return p.stdout if binary else p.stdout.decode(errors="replace").strip()

    def shell(self, cmd: str, check: bool = False, timeout: float = 60) -> str:
        return self.adb("shell", cmd, check=check, timeout=timeout)

    def drizz(self, *args: str, timeout: float = 60) -> str:
        p = subprocess.run([DRIZZ, *args], capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise DeviceError(f"drizz {args[0]} failed: {(p.stderr or p.stdout).strip()}")
        return p.stdout

    # ---- health ------------------------------------------------------------------------------
    def wait_boot(self, timeout_s: int = 180) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if self.shell("getprop sys.boot_completed", timeout=10) == "1":
                    return
            except (DeviceError, subprocess.TimeoutExpired):
                pass
            time.sleep(2)
        raise DeviceError(f"{self.serial} did not finish booting within {timeout_s}s")

    def healthy(self) -> tuple[bool, str]:
        state = self.adb("get-state", check=False)
        boot = self.shell("getprop sys.boot_completed")
        return state == "device" and boot == "1", f"state={state} boot_completed={boot}"

    def info(self) -> dict:
        prop = lambda k: self.shell(f"getprop {k}")  # noqa: E731
        size = self.shell("wm size").split(":")[-1].strip()
        return {"serial": self.serial, "avd": running_avd(self.serial) if self.serial.startswith("emulator-") else None,
                "model": prop("ro.product.model"), "android": prop("ro.build.version.release"),
                "api": prop("ro.build.version.sdk"), "abi": prop("ro.product.cpu.abi"), "screen": size}

    # ---- apps --------------------------------------------------------------------------------
    def installed(self, package: str) -> bool:
        return f"package:{package}" in self.shell(f"pm list packages {package}").split()

    def app_version(self, package: str) -> dict:
        dump = self.shell(f"dumpsys package {package}")
        name = re.search(r"versionName=(\S+)", dump)
        code = re.search(r"versionCode=(\d+)", dump)
        return {"version_name": name[1] if name else None, "version_code": code[1] if code else None}

    def install(self, apk: Path) -> None:
        self.adb("install", "-r", "-g", str(apk), timeout=300)

    def launch(self, package: str, activity: str | None = None) -> None:
        if not activity:
            out = self.shell(f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {package}")
            component = out.splitlines()[-1].strip() if out else ""
            activity = component.split("/", 1)[1] if component.startswith(f"{package}/") else None
        if activity:
            out = self.shell(f"am start -W -n {package}/{activity}")
            if "Error" in out:
                raise DeviceError(f"launch {package}/{activity} failed: {out[-300:]}")
        else:
            # monkey exits non-zero on some images even when the launch worked.
            self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

    def force_stop(self, package: str) -> None:
        self.shell(f"am force-stop {package}")

    def clear_data(self, package: str) -> None:
        self.shell(f"pm clear {package}", check=True)

    def foreground_package(self) -> str | None:
        out = self.shell("dumpsys activity activities | grep -m1 -E 'ResumedActivity|topResumedActivity'")
        m = re.search(r"\s([\w.]+)/", out)
        return m[1] if m else None

    # ---- clock / location --------------------------------------------------------------------
    def set_clock(self, zone: str, hh_mm: str, date_iso: str | None = None) -> datetime:
        """Pin the device clock to hh:mm (today in `zone`, or `date_iso`). Returns the target time."""
        z = ZoneInfo(zone)
        day = datetime.fromisoformat(date_iso).date() if date_iso else datetime.now(z).date()
        hour, minute = map(int, hh_mm.split(":"))
        target = datetime(day.year, day.month, day.day, hour, minute, tzinfo=z)
        self.shell("settings put global auto_time 0")
        self.shell("settings put global auto_time_zone 0")
        self.shell(f"cmd alarm set-timezone {zone}")
        self.shell(f"cmd alarm set-time {int(target.timestamp() * 1000)}")
        return target

    def restore_clock(self, zone: str | None = None) -> None:
        if zone:
            self.shell(f"cmd alarm set-timezone {zone}")
        self.shell("settings put global auto_time_zone 1")
        self.shell("settings put global auto_time 1")

    def now(self, zone: str) -> datetime:
        return datetime.fromtimestamp(int(self.shell("date +%s")), ZoneInfo(zone))

    def set_location(self, lat: float, lng: float) -> None:
        if self.serial.startswith("emulator-"):
            self.adb("emu", "geo", "fix", str(lng), str(lat))
        else:
            self.drizz("set-location", self.serial, str(lat), str(lng))

    # ---- capture -----------------------------------------------------------------------------
    def screenshot_png(self) -> bytes:
        return self.adb("exec-out", "screencap", "-p", binary=True, timeout=30)

    def screenshot(self, path: Path) -> str:
        data = self.screenshot_png()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return hashlib.sha1(data).hexdigest()

    def snapshot(self) -> tuple[Path, str]:
        """drizz take-snapshot: (png path, hierarchy xml)."""
        out = self.drizz("take-snapshot", self.serial, timeout=90)
        first, _, rest = out.partition("\n")
        return Path(first.strip()), rest.strip()

    def ui_hierarchy(self) -> str:
        try:
            return Path(self.drizz("get-ui-hierarchy", self.serial, timeout=90).strip()).read_text()
        except (DeviceError, OSError, subprocess.TimeoutExpired):
            self.shell("uiautomator dump /sdcard/__bench_ui.xml", timeout=60)
            return self.adb("exec-out", "cat", "/sdcard/__bench_ui.xml", check=False)

    def logcat_clear(self) -> None:
        self.adb("logcat", "-c", check=False)

    def logcat_dump(self) -> str:
        return self.adb("logcat", "-d", check=False, timeout=120)

    # ---- input (used by the built-in agent) ----------------------------------------------------
    def tap(self, x: int, y: int) -> None:
        self.drizz("tap", self.serial, str(x), str(y))

    def long_press(self, x: int, y: int, ms: int = 800) -> None:
        self.shell(f"input swipe {x} {y} {x} {y} {ms}", check=True)

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.drizz("swipe", self.serial, str(x1), str(y1), str(x2), str(y2))

    def type_text(self, text: str) -> None:
        self.drizz("type-text", self.serial, text)

    def clear_text(self) -> None:
        self.drizz("clear-text", self.serial)

    def back(self) -> None:
        self.drizz("press-back", self.serial)

    def home(self) -> None:
        self.drizz("press-home", self.serial)

    def keyevent(self, key: str) -> None:
        self.shell(f"input keyevent {key}", check=True)
