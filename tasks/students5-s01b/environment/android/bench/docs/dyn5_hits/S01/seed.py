"""students5_s01b copy of docs/students5/seed.py (3 Nov 2026): answers() also handles s01b; unchanged otherwise.
Seed a students5 task onto a device, and re-derive its answers from the live apps.

    python3 docs/students5/seed.py seed s01 --serial emulator-5554      # (the task's setup runs this)
    python3 docs/students5/seed.py answers s01 --serial emulator-5554   # check ANSWERS against the live apps

`seed` pushes the built mail / calendar / Airbnb / DashSF databases (discover50_2's push, with the MailSF and CalSF
session files), resets FlySF and CartSF through their data APIs (clock offset back to zero, simulator QUIET, the
stock the task needs), pushes the task's PDFs, and saves a `before` copy of every app DB for grade.py.
Run it after `set_time`: FlySF and CartSF anchor their seed data on the day they are reset.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "discover50_2"))
import seeder as D52  # noqa: E402

BUILD, STATE = ROOT / "build", ROOT / ".state"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())
ANSWERS = json.loads((ROOT / "answers.json").read_text())

# app -> (package, db file, launch activity, data API port)
APPS = {
    "mailsf": ("dev.drizz.mailsf", "mailsf.db", "dev.drizz.mailsf.MainActivity", None),
    "calsf": ("dev.drizz.calsf", "calsf.db", "dev.drizz.calsf.MainActivity", None),
    "airbnb": ("dev.drizz.airbnb", "airbnb.db", "dev.drizz.airbnb.MainActivity", None),
    "dashsf": ("dev.drizz.dashsf.bench", "dashsf.db", "dev.drizz.dashsf.MainActivity", None),
    "flysf": ("dev.drizz.flysf", "flysf.db", "dev.drizz.flysf.MainActivity", 8791),
    "cartsf": ("dev.drizz.cartsf", "cartsf.db", "dev.drizz.cartsf.MainActivity", 8792),
}


class Dev(D52.Dev):
    def forward(self, port: int) -> int:
        """Forward the app's loopback API to a free host port; returns the host port."""
        return int(self.adb("forward", "tcp:0", f"tcp:{port}").stdout.strip())


def api(host_port: int, action: str, body: dict | None = None, tries: int = 60) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{host_port}/api/actions/{action}",
                                 json.dumps(body or {}).encode(), {"Content-Type": "application/json"})
    last = None
    for _ in range(tries):
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{action}: HTTP {e.code} {e.read().decode()[:300]}") from e
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:   # app still starting
            last = e
            time.sleep(1.5)
    raise RuntimeError(f"{action}: data API not reachable ({last})")


def get(host_port: int, path: str) -> dict:
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{host_port}{path}", timeout=30))


def start_api(dev: Dev, app: str) -> int:
    pkg, _, act, port = APPS[app]
    dev.adb("shell", "am", "start", "-n", f"{pkg}/{act}")
    time.sleep(4)
    return dev.forward(port)


# The apps' demo account is "Asha Rao" with a Rao family's travellers and trips. The persona here is Alex Morgan, a
# student, so after each reset the account is renamed and FlySF's saved travellers and seeded trips are replaced.
PERSONA_SQL = {
    "flysf": """
        UPDATE users SET firstName = 'Alex', lastName = 'Morgan', phone = '+1 415-555-0100', currency = 'USD';
        DELETE FROM travellers;
        INSERT INTO travellers (id, userId, type, title, firstName, lastName, dateOfBirth, gender, nationality,
            passportNumber, passportExpiry, issuingCountry, frequentFlyerAirline, frequentFlyerNumber, mealPreference,
            specialAssistance, isDefault, createdAt)
        VALUES ('t-alex-morgan', 'u-demo', 'ADULT', 'MR', 'Alex', 'Morgan', '2004-07-11', 'MALE', 'United States',
            '', NULL, '', '', '', '', '', 1, 1792529938598);
        DELETE FROM booking_events; DELETE FROM booking_extras; DELETE FROM price_lines; DELETE FROM booking_passengers;
        DELETE FROM booking_segments; DELETE FROM booking_legs; DELETE FROM bookings;
    """,
    "cartsf": "UPDATE users SET firstName = 'Alex', lastName = 'Morgan', phone = '+1 415-555-0100';",
}


def rewrite_db(dev: Dev, app: str, sql: str) -> None:
    """Pull an app's DB (with WAL), apply SQL on the host, push it back as a single file."""
    import sqlite3
    import tempfile
    pkg, dbname, _, _ = APPS[app]
    dev.adb("shell", "am", "force-stop", pkg)
    with tempfile.TemporaryDirectory() as td:
        db = pull(dev, app, Path(td))
        con = sqlite3.connect(db)
        con.executescript(sql)
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        dev.adb("push", str(db), f"/data/local/tmp/{dbname}")
        dev.adb("shell", "chmod", "644", f"/data/local/tmp/{dbname}")
        dev.run_as(pkg, f"cp /data/local/tmp/{dbname} databases/{dbname} && rm -f databases/{dbname}-wal "
                        f"databases/{dbname}-shm")
        dev.adb("shell", "rm", "-f", f"/data/local/tmp/{dbname}")


def push_session(dev: Dev, app: str) -> None:
    """FlySF and CartSF keep the signed-in user in a DataStore file; push the demo account's (u-demo) so they open signed in."""
    pkg = APPS[app][0]
    pb = f"{app}_session.preferences_pb"
    dev.adb("push", str(ROOT / "base" / pb), f"/data/local/tmp/{pb}")
    dev.adb("shell", "chmod", "644", f"/data/local/tmp/{pb}")
    dev.run_as(pkg, f"mkdir -p files/datastore && cp /data/local/tmp/{pb} files/datastore/{pb}")
    dev.adb("shell", "rm", "-f", f"/data/local/tmp/{pb}")


def reset_flysf(dev: Dev) -> None:
    hp = start_api(dev, "flysf")
    api(hp, "reset_data", {"keepAudit": False})
    api(hp, "set_simulation", {"scenario": "QUIET", "searchSeconds": 0, "fixedReferences": True})
    off = (get(hp, "/api/simulation") or {}).get("clockOffsetMinutes", 0) or 0
    if off:
        api(hp, "shift_clock", {"offsetMinutes": -int(off)})
    rewrite_db(dev, "flysf", PERSONA_SQL["flysf"])
    push_session(dev, "flysf")


def reset_cartsf(dev: Dev, spec: dict) -> None:
    hp = start_api(dev, "cartsf")
    api(hp, "reset_data", {"keepAudit": False})
    api(hp, "set_simulation", {"scenario": "QUIET", "fixedOrderNumbers": True})
    off = (get(hp, "/api/simulation") or {}).get("clockOffsetMinutes", 0) or 0
    if off:
        api(hp, "shift_clock", {"offsetMinutes": -int(off)})
    for retailer, stock in (spec.get("stock") or {}).items():
        for pid, ok in stock.items():
            api(hp, "set_stock", {"retailerId": retailer, "productId": pid, "inStock": bool(ok)})
    rewrite_db(dev, "cartsf", PERSONA_SQL["cartsf"])
    push_session(dev, "cartsf")


def pull(dev: Dev, app: str, out_dir: Path) -> Path | None:
    """Copy an app's DB (with its WAL) off the device; sqlite folds the WAL in on open."""
    pkg, dbname, _, _ = APPS[app]
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{app}.db"
    for suffix in ("", "-wal", "-shm"):
        r = subprocess.run(["adb", "-s", dev.serial, "exec-out", "run-as", pkg, "cat", f"databases/{dbname}{suffix}"],
                           capture_output=True, timeout=120)
        target = Path(str(dst) + suffix)
        if r.returncode == 0 and r.stdout and not r.stdout.startswith(b"run-as:") and b"No such file" not in r.stdout[:80]:
            target.write_bytes(r.stdout)
        elif target.exists():
            target.unlink()
    return dst if dst.exists() else None


def seed(tid: str, serial: str) -> None:
    m = MANIFEST[tid]
    dev = Dev(serial)
    for app, dbfile in m["dbs"].items():
        D52.push_db(dev, app, BUILD / tid / dbfile)
        print(f"  {app}: pushed")
    if m.get("flysf"):
        reset_flysf(dev)
        print("  flysf: reset (QUIET, clock offset 0)")
    if m.get("cartsf"):
        reset_cartsf(dev, m["cartsf"])
        print(f"  cartsf: reset (QUIET, clock offset 0, {sum(len(v) for v in m['cartsf'].get('stock', {}).values())} "
              "stock pins)")
    # Another students5 task's PDF must not leak into this one; only our own files are removed.
    for other in {f["dest"] for t in MANIFEST.values() for f in t["files"]}:
        dev.adb("shell", "rm", "-f", other, check=False)
        dev.sh(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{other}")
    for f in m["files"]:
        dev.adb("shell", "mkdir", "-p", str(Path(f["dest"]).parent))
        dev.adb("push", str(ROOT / f["src"]), f["dest"])
        dev.sh(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{f['dest']}")
        print(f"  file: {f['dest']}")
    before = STATE / serial / tid / "before"
    for app in m["apps"]:
        if app in APPS:
            pull(dev, app, before)
    (STATE / serial / tid / "seeded.json").write_text(json.dumps({"tid": tid, "at": time.time()}))
    print(f"SEEDED students5 {tid} on {serial}")


# ------------------------------------------------------------------------------------------------ answers
def _flights(hp: int, frm: str, to: str, date: str, adults: int) -> list[dict]:
    res = api(hp, "search_flights", {"from": frm, "to": to, "departDate": date, "adults": adults})
    return res["legs"][0]["options"]


def _local(ms: int, offset_min: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.fromtimestamp(ms / 1000, timezone.utc) + timedelta(minutes=offset_min)).strftime("%Y-%m-%d %H:%M")


TZ = {"SFO": -480, "JFK": -300, "EWR": -300, "LHR": 0, "LGW": 0}


def answers(tid: str, serial: str) -> int:
    """Re-derive the FlySF answers from the live generator and the rules; print the table and any mismatch."""
    a = ANSWERS[tid]
    dev = Dev(serial)
    bad = 0
    if tid in ("s01", "s01b"):
        hp = start_api(dev, "flysf")
        out = [o for to in ("JFK", "EWR") for o in _flights(hp, "SFO", to, "2026-11-12", 5)]
        back = [o for fr in ("JFK", "EWR") for o in _flights(hp, fr, "SFO", "2026-11-16", 5)]
        ok_out = [o for o in out if _local(o["departMillis"], -480)[11:] >= "06:00"
                  and _local(o["arriveMillis"], -300) <= "2026-11-12 16:00" and o["seatsLeft"] >= 5
                  and not o["selfTransfer"]]
        ok_back = [o for o in back if _local(o["departMillis"], -300)[11:] >= "13:00" and o["seatsLeft"] >= 5
                   and not o["selfTransfer"]]
        for o in out + back:
            tag = "OK " if o in ok_out + ok_back else "-- "
            print(tag, o["id"], _local(o["departMillis"], TZ[o["from"]]), "->", _local(o["arriveMillis"], TZ[o["to"]]),
                  "base", o["basePriceRupees"], "seats", o["seatsLeft"])
        best = (min(ok_out, key=lambda o: o["basePriceRupees"])["id"], min(ok_back, key=lambda o: o["basePriceRupees"])["id"])
        # same airport both ways is a rule (everyone on the same flights is satisfied either way); report both
        print("cheapest qualifying:", best, "expected:", tuple(a["flysf_itineraries"]))
        bad += best != tuple(a["flysf_itineraries"])
        q = api(hp, "get_price_quote", {"itineraryIds": list(best), "brand": "BASIC", "adults": 5})
        print(f"BASIC quote for 5: ${q['breakdown']['totalRupees'] / 100:,.2f} + 2 checked bags ($18.00 each)")
    elif tid == "s04":
        hp = start_api(dev, "flysf")
        out = [o for to in ("LHR", "LGW") for o in _flights(hp, "SFO", to, "2026-12-01", 3)]
        back = [o for fr in ("LHR", "LGW") for o in _flights(hp, fr, "SFO", "2026-12-06", 3)]

        def stops(o):
            return len(o["segments"]) - 1

        ok_out = [o for o in out if "2026-12-02 08:00" <= _local(o["arriveMillis"], 0) <= "2026-12-02 18:00"
                  and not o["selfTransfer"] and stops(o) <= 1 and o["seatsLeft"] >= 3]
        ok_back = [o for o in back if _local(o["departMillis"], 0)[11:] >= "14:00" and not o["selfTransfer"]
                   and stops(o) <= 1 and o["seatsLeft"] >= 3]
        quotes = {}
        for o in ok_out + ok_back:
            quotes[o["id"]] = api(hp, "get_price_quote", {"itineraryIds": [o["id"]], "brand": "BASIC",
                                                          "adults": 3})["breakdown"]["totalRupees"]
        for o in out + back:
            tag = "OK " if o["id"] in quotes else "-- "
            print(tag, o["id"], _local(o["departMillis"], TZ[o["from"]]), "->", _local(o["arriveMillis"], TZ[o["to"]]),
                  "selfT" if o["selfTransfer"] else "", "seats", o["seatsLeft"], quotes.get(o["id"], ""))
        best = (min(ok_out, key=lambda o: quotes[o["id"]])["id"], min(ok_back, key=lambda o: quotes[o["id"]])["id"])
        print("cheapest qualifying:", best, "expected:", tuple(a["flysf_itineraries"]))
        bad += best != tuple(a["flysf_itineraries"])
        q = api(hp, "get_price_quote", {"itineraryIds": list(best), "brand": "BASIC", "adults": 3})
        print(f"BASIC multi-city quote for 3: ${q['breakdown']['totalRupees'] / 100:,.2f}")
    elif tid in ("s02", "s03"):
        hp = start_api(dev, "cartsf")
        for pid in a["cart_items"]:
            p = api(hp, "get_product", {"retailerId": a["cart_store"], "productId": pid})
            prod = p.get("product", p)
            print(f"  {pid:24} {str(prod.get('name')):32} inStock={prod.get('inStock')} price={prod.get('priceCents')}")
            bad += prod.get("inStock") is False
    print("ANSWERS OK" if not bad else f"ANSWERS MISMATCH ({bad})")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["seed", "answers"])
    ap.add_argument("tid")
    ap.add_argument("--serial", required=True)
    a = ap.parse_args()
    return seed(a.tid, a.serial) or 0 if a.cmd == "seed" else answers(a.tid, a.serial)


if __name__ == "__main__":
    raise SystemExit(main())
