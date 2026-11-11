"""Stock-app state on the device: SMS, contacts, Clock alarms, media files.

Used by cross-app tasks: setup steps reset and seed this state, the capture step snapshots it to
device_state.json so graders (and regrades) read one file instead of the live device.

Notes from the drizz emulators (API 34, Google Messages / Clock):
  * deleting SMS needs `appops set com.android.shell WRITE_SMS allow`; Google Messages keeps its own
    conversation copy, so it is cleared too (no onboarding afterwards, it stays the default SMS app).
  * never `pm clear com.android.providers.telephony`: it breaks the SMS store until a cold boot.
  * Clock alarms are read from the alarm list UI (labels are not exposed anywhere else without root).
"""

from __future__ import annotations

import re
import shlex
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from benchlib.device import Device

MESSAGING = "com.google.android.apps.messaging"
DESKCLOCK = "com.google.android.deskclock"
UIA_HOLDERS = ("com.drizz.example.mydeletableapplication2", "com.artemis.helper")


def digits(phone: str | None) -> str:
    d = re.sub(r"\D", "", phone or "")
    return d[-10:]


def _q(s: str) -> str:
    """Quote for the device shell (adb shell joins argv into one string)."""
    return shlex.quote(s)


# ---- reset / seed ---------------------------------------------------------------------------------

def reset_sms(dev: Device) -> None:
    dev.shell("appops set com.android.shell WRITE_SMS allow")
    dev.shell("content delete --uri content://sms")
    dev.shell("content delete --uri content://mms")
    dev.shell(f"pm clear {MESSAGING}")


def sms_in(dev: Device, sender: str, body: str) -> None:
    """Deliver an inbound SMS through the emulator modem (shows up as a real notification)."""
    dev.adb("emu", "sms", "send", digits(sender) or sender, body, check=True)


def reset_contacts(dev: Device) -> None:
    dev.shell("content delete --uri 'content://com.android.contacts/raw_contacts?caller_is_syncadapter=true'")


def add_contact(dev: Device, name: str, phone: str) -> None:
    dev.shell("content insert --uri content://com.android.contacts/raw_contacts "
              "--bind account_type:n: --bind account_name:n:")
    rows = dev.shell("content query --uri content://com.android.contacts/raw_contacts --projection _id")
    ids = [int(m) for m in re.findall(r"_id=(\d+)", rows)]
    rid = max(ids)
    first, _, last = name.partition(" ")
    dev.shell("content insert --uri content://com.android.contacts/data "
              f"--bind raw_contact_id:i:{rid} --bind mimetype:s:vnd.android.cursor.item/name "
              f"--bind data1:s:{_q(name)} --bind data2:s:{_q(first)} --bind data3:s:{_q(last)}")
    dev.shell("content insert --uri content://com.android.contacts/data "
              f"--bind raw_contact_id:i:{rid} --bind mimetype:s:vnd.android.cursor.item/phone_v2 "
              f"--bind data1:s:{_q(phone)} --bind data2:i:2")


def reset_alarms(dev: Device) -> None:
    dev.shell(f"pm clear {DESKCLOCK}")


def add_alarm(dev: Device, hour: int, minute: int, label: str = "") -> None:
    extra = f" --es android.intent.extra.alarm.MESSAGE {_q(label)}" if label else ""
    dev.shell("am start -a android.intent.action.SET_ALARM --ei android.intent.extra.alarm.HOUR "
              f"{hour} --ei android.intent.extra.alarm.MINUTES {minute} "
              f"--ez android.intent.extra.alarm.SKIP_UI true{extra}", check=True)
    time.sleep(2)
    dev.shell(f"am force-stop {DESKCLOCK}")


def reset_media(dev: Device, paths: list[str]) -> None:
    """Remove exactly these files/dirs (never whole shared folders: other files on the device stay)."""
    for p in paths:
        dev.shell(f"rm -rf {_q(p)}")
    dev.shell("content call --method scan_volume --uri content://media --arg external_primary")


def push_media(dev: Device, src: Path, dest: str, mtime: str | None = None) -> None:
    dev.adb("push", str(src), dest, timeout=300)
    if mtime:   # touch -t [[CC]YY]MMDDhhmm[.ss]
        dev.shell(f"touch -t {mtime} {_q(dest)}")
    dev.shell(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d {_q('file://' + dest)}")


# ---- capture --------------------------------------------------------------------------------------

def _rows(out: str) -> list[dict]:
    # A value with a newline (a multi-line SMS body) spills onto the following lines, and the columns after it
    # (date, type) are on the last of them, so a row runs until the next "Row:" line.
    records = []
    for line in out.splitlines():
        if line.startswith("Row:"):
            records.append(line)
        elif records:
            records[-1] += "\n" + line
    rows = []
    for line in records:
        body = line.split(" ", 2)[2] if line.count(" ") >= 2 else ""
        # values may contain ", "; split only before known keys
        parts = re.split(r", (?=[a-z_0-9]+=)", body)
        rows.append({k: v for k, _, v in (p.partition("=") for p in parts)})
    return rows


def read_sms(dev: Device) -> list[dict]:
    out = dev.shell("content query --uri content://sms --projection _id:address:body:date:type", timeout=60)
    msgs = []
    for r in _rows(out):
        msgs.append({"id": int(r.get("_id", 0) or 0), "address": r.get("address"), "digits": digits(r.get("address")),
                     "body": r.get("body", ""), "date": int(r.get("date", 0) or 0),
                     "type": {"1": "inbox", "2": "sent", "4": "outbox", "5": "failed", "6": "queued"}.get(r.get("type"), r.get("type"))})
    return sorted(msgs + read_mms(dev), key=lambda m: m["date"])


def read_mms(dev: Device) -> list[dict]:
    """Group texts are MMS: one row per message with every recipient, so graders can treat them like SMS."""
    out = []
    for r in _rows(dev.shell("content query --uri content://mms --projection _id:date:msg_box", timeout=60)):
        mid = r.get("_id")
        if not mid:
            continue
        addrs = [a.get("address") for a in _rows(dev.shell(f"content query --uri content://mms/{mid}/addr --projection address:type"))
                 if a.get("type") in ("151", "130", "129")]          # TO, CC, BCC
        parts = _rows(dev.shell("content query --uri content://mms/part --projection mid:ct:text"))
        body = " ".join(p.get("text", "") for p in parts if p.get("mid") == mid and p.get("ct") == "text/plain")
        box = {"1": "inbox", "2": "sent", "4": "outbox"}.get(r.get("msg_box"), r.get("msg_box"))
        for a in addrs or [None]:
            out.append({"id": f"mms-{mid}", "address": a, "digits": digits(a), "body": body,
                        "date": int(r.get("date", 0) or 0) * 1000, "type": box, "mms": True})
    return out


def read_contacts(dev: Device) -> list[dict]:
    out = dev.shell("content query --uri content://com.android.contacts/data/phones --projection display_name:data1")
    return [{"name": r.get("display_name"), "phone": r.get("data1")} for r in _rows(out)]


def _ui_dump(dev: Device) -> str:
    """UI hierarchy via the drizz CLI (works while ARTEMIS's accessibility service is enabled, where a plain
    `uiautomator dump` returns nothing); falls back to uiautomator."""
    try:
        xml = dev.ui_hierarchy()
        if xml and "<hierarchy" in xml:
            return xml
    except Exception:  # noqa: BLE001
        pass
    for pkg in UIA_HOLDERS:
        dev.shell(f"am force-stop {pkg}")
    dev.shell("uiautomator dump /sdcard/__bench_ui.xml", timeout=60)
    return dev.adb("exec-out", "cat", "/sdcard/__bench_ui.xml", check=False)


def read_alarms(dev: Device) -> list[dict]:
    """Open Clock's alarm list and read every alarm row (time, label, enabled). Scrolls up to 4 pages."""
    alarms: dict[tuple, dict] = {}
    root = None
    # Start Clock fresh: an agent may have left an editor/dialog open over the list (unsaved edits are discarded),
    # and an "app isn't responding" dialog from another app would cover it (app DBs are already pulled by now).
    for pkg in (DESKCLOCK, "dev.drizz.ridesf.bench", "dev.drizz.dashsf.bench", "dev.drizz.airbnb",
                "com.google.android.apps.messaging", "com.google.android.apps.photos", "com.google.android.apps.docs"):
        dev.shell(f"am force-stop {pkg}")
    dev.shell("input keyevent HOME")
    for attempt in range(4):          # Clock's list can take a while after `pm clear`; it always has default alarms
        dev.shell("am start -a android.intent.action.SHOW_ALARMS")
        time.sleep(3 + 2 * attempt)
        try:
            root = ET.fromstring(_ui_dump(dev))
        except ET.ParseError:
            continue
        if _alarm_rows(root):
            break
    for _ in range(5):
        if root is None:
            break
        before = len(alarms)
        for a in _alarm_rows(root):
            alarms.setdefault((a["time"], a["label"], a["enabled"]), a)
        if len(alarms) == before:
            break
        dev.shell("input swipe 540 1700 540 700 400")
        time.sleep(1.5)
        try:
            root = ET.fromstring(_ui_dump(dev))
        except ET.ParseError:
            break
    dev.shell("input keyevent HOME")
    return list(alarms.values())


def _alarm_rows(root: ET.Element) -> list[dict]:
    # Each alarm row holds (in document order) an optional `label`, a `digital_clock` and an `onoff` switch.
    rows, cur = [], None
    for n in root.iter("node"):
        rid = (n.get("resource-id") or "").split("/")[-1]
        if rid == "alarm_card_layout":     # Clock 7.x and 9.x both wrap each alarm row in alarm_card_layout
            cur = {"time_text": "", "label": "", "enabled": None}
            rows.append(cur)
        elif cur is None:
            continue
        elif rid == "digital_clock":
            cur["time_text"] = (n.get("text") or n.get("content-desc") or "").replace(" ", " ").replace(" ", " ")
        elif rid in ("label", "edit_label") and n.get("text"):
            cur["label"] = n.get("text")
        elif rid == "onoff" and n.get("checkable") == "true":
            cur["enabled"] = n.get("checked") == "true"
    for r in rows:
        r["time"] = _hhmm(r["time_text"])
    return rows


def _hhmm(text: str) -> str | None:
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AaPp])?\.?\s*[Mm]?", text)
    if not m:
        return None
    h, mi = int(m[1]), int(m[2])
    if m[3]:
        ap = m[3].lower()
        h = h % 12 + (12 if ap == "p" else 0)
    return f"{h:02d}:{mi:02d}"


def list_files(dev: Device, dirs: list[str]) -> dict[str, list[str]]:
    return {d: sorted(x for x in dev.shell(f"ls -1 {_q(d)} 2>/dev/null").splitlines() if x.strip()) for d in dirs}


def capture(dev: Device, cfg: dict) -> dict:
    """Snapshot the stock-app state named in cfg: {sms, contacts, alarms, files: [dirs]}."""
    state: dict = {"captured_at_ms": int(dev.shell("date +%s") or 0) * 1000}
    if cfg.get("sms", True):
        state["sms"] = read_sms(dev)
    if cfg.get("contacts", False):
        state["contacts"] = read_contacts(dev)
    if cfg.get("files"):
        state["files"] = list_files(dev, cfg["files"])
    if cfg.get("alarms", False):
        try:
            state["alarms"] = read_alarms(dev)
        except Exception as e:  # noqa: BLE001
            state["alarms_error"] = f"{type(e).__name__}: {e}"
    return state
