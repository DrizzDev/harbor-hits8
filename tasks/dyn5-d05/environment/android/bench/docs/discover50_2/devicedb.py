"""The `device` pseudo-app: Messages and Contacts state as one SQLite file, so checkpoints grade texts with SQL.

The bench captures stock-app state after a run into device_state.json (benchlib/devicestate.capture: `sms`,
`contacts`). This module writes that JSON, or a task's start state, as `device.db`:

  sms(id, address, digits, body, date, type, mms)   one row per text (an MMS group text: one row per recipient)
                                                     type: inbox | sent | outbox | queued | failed | <raw code>
  contacts(name, phone, digits)                      one row per phone number

`digits` is the last 10 digits of the number, so '+1 415-555-0123', '4155550123' and '(415) 555-0123' all match
'4155550123'. A checkpoint with `app: device` runs on this file; other apps are ATTACHed as usual (`shopsf.orders`).

Used by docs/discover50_2/_build/make_all.py (start state), qualify.py (simulated runs) and graders/discover50_2.py
(after a real run).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE sms (id TEXT, address TEXT, digits TEXT, body TEXT NOT NULL DEFAULT '', date INTEGER NOT NULL DEFAULT 0,
                  type TEXT NOT NULL, mms INTEGER NOT NULL DEFAULT 0);
CREATE TABLE contacts (name TEXT, phone TEXT, digits TEXT);
"""
# What a sent text looks like in the sms table: anything the agent sent, whether or not the emulator modem delivered it.
OUTGOING = ("sent", "outbox", "queued", "failed")


def digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


def write(path: Path, contacts: list[dict], sms: list[dict]) -> Path:
    """device.db from contacts [{name, phone}] and texts [{id, address, body, date, type, mms?}]."""
    path = Path(path)
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO contacts VALUES (?, ?, ?)",
                    [(c.get("name"), c.get("phone"), digits(c.get("phone"))) for c in contacts])
    con.executemany("INSERT INTO sms VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(str(m.get("id")), m.get("address"), digits(m.get("address")), m.get("body") or "",
                      int(m.get("date") or 0), str(m.get("type")), int(bool(m.get("mms")))) for m in sms])
    con.commit()
    con.close()
    return path


def start_state(path: Path, contacts: list[dict], sms_in: list[dict], clock_iso: str) -> Path:
    """The state the task's setup leaves: the device contacts and the inbound texts, delivered just after the clock."""
    t0 = int(datetime.fromisoformat(clock_iso).timestamp() * 1000)
    rows = [{"id": f"in-{i + 1}", "address": m["from"], "body": m["body"], "date": t0 + 1000 * (i + 1), "type": "inbox"}
            for i, m in enumerate(sms_in)]
    return write(path, contacts, rows)


def from_capture(device_state: dict, path: Path) -> Path:
    """device.db from a trial's captured device_state (dict or device_state.json contents)."""
    return write(path, device_state.get("contacts") or [], device_state.get("sms") or [])


if __name__ == "__main__":   # python3 devicedb.py <trial>/device_state.json <out>/device.db
    write_to = from_capture(json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]))
    print(write_to)
