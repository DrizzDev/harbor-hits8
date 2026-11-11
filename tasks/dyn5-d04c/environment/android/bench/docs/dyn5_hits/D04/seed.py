"""Seed a dyn5 task onto a device, and run its mid-run trigger actions.

    python3 docs/dyn5/seed.py seed d02 --serial emulator-5554              # (the task's setup runs this)
    python3 docs/dyn5/seed.py event d02 shopper_starts --serial emulator-5554   # (the task's events run this)

`seed` pushes the built mail / calendar / Airbnb / DashSF databases (discover50_2's push), resets CartSF through its
data API (clock offset 0, simulator QUIET so nothing moves unless a trigger moves it, the task's stock pins), rewrites
CartSF with the task's SQL (carts, past orders), pushes the task's PDF, and saves a `before` copy of every app DB.
Run it after `set_time`: CartSF anchors its seed data on the day it is reset.

`event` is what a trigger does to CartSF while the agent works: empty a shelf, or let the shopper shop the new order
item by item until it needs an answer (an out-of-stock line) or is done (then it is delivered). It never brings
CartSF to the front: the data API runs in the app process, which a broadcast wakes if it is not running.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# students5's seed.py (Dev, api, get, pull, rewrite_db, push_session, start_api, PERSONA_SQL); its module has the
# same name as this one, so it is loaded by path
_spec = importlib.util.spec_from_file_location("students5_seed", ROOT.parent / "students5" / "seed.py")
S5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S5)

D52 = S5.D52
BUILD, STATE = ROOT / "build", ROOT / ".state"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())
ANSWERS = json.loads((ROOT / "answers.json").read_text())
CART_PKG, CART_PORT = S5.APPS["cartsf"][0], S5.APPS["cartsf"][3]
ALL_FILES = [f["dest"] for t in MANIFEST.values() for f in t["files"]] + \
            [f["dest"] for t in S5.MANIFEST.values() for f in t["files"]]


def reset_cartsf(dev: S5.Dev, spec: dict) -> None:
    hp = S5.start_api(dev, "cartsf")
    S5.api(hp, "reset_data", {"keepAudit": False})
    S5.api(hp, "set_simulation", {"scenario": "QUIET", "fixedOrderNumbers": True, "decisionTimeoutSeconds": 0})
    off = (S5.get(hp, "/api/simulation") or {}).get("clockOffsetMinutes", 0) or 0
    if off:
        S5.api(hp, "shift_clock", {"offsetMinutes": -int(off)})
    for retailer, stock in (spec.get("stock") or {}).items():
        for pid, ok in stock.items():
            S5.api(hp, "set_stock", {"retailerId": retailer, "productId": pid, "inStock": bool(ok)})
    S5.rewrite_db(dev, "cartsf", S5.PERSONA_SQL["cartsf"] + "\n" + spec.get("sql", ""))
    S5.push_session(dev, "cartsf")


def seed(tid: str, serial: str) -> None:
    m = MANIFEST[tid]
    dev = S5.Dev(serial)
    for app, dbfile in m["dbs"].items():
        D52.push_db(dev, app, BUILD / tid / dbfile)
        print(f"  {app}: pushed")
    reset_cartsf(dev, m["cartsf"])
    pins = sum(len(v) for v in m["cartsf"].get("stock", {}).values())
    print(f"  cartsf: reset (QUIET, clock offset 0, {pins} stock pins, task SQL)")
    for other in ALL_FILES:          # no other dyn5 / students5 PDF may leak into this task
        dev.adb("shell", "rm", "-f", other, check=False)
        dev.sh(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{other}")
    for f in m["files"]:
        dev.adb("shell", "mkdir", "-p", str(Path(f["dest"]).parent))
        dev.adb("push", str(ROOT / f["src"]), f["dest"])
        dev.sh(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{f['dest']}")
        print(f"  file: {f['dest']}")
    before = STATE / serial / tid / "before"
    for app in m["apps"]:
        if app in S5.APPS:
            dev.adb("shell", "am", "force-stop", S5.APPS[app][0])   # flush the WAL before copying
            S5.pull(dev, app, before)
    (STATE / serial / tid / "seeded.json").write_text(json.dumps({"tid": tid, "at": time.time()}))
    print(f"SEEDED dyn5 {tid} on {serial}")


# ------------------------------------------------------------------------------------------------ triggers
def cart_api(dev: S5.Dev) -> int:
    """CartSF's data API on a free host port, without opening the app's UI."""
    hp = dev.forward(CART_PORT)
    try:
        S5.get(hp, "/api/simulation")
    except Exception:  # noqa: BLE001  app process not running: a broadcast starts it (and its data server);
        # -f 0x20 (FLAG_INCLUDE_STOPPED_PACKAGES): without it Android drops the broadcast to a force-stopped app
        dev.sh(f"am broadcast -f 0x20 -p {CART_PKG} -a dev.drizz.cartsf.SET_CLOCK --el shift 0")
        S5.api(hp, "list_retailers", tries=20)
    return hp


def live_orders(hp: int, retailer: str) -> list[dict]:
    rows = S5.api(hp, "list_orders", {"limit": 50}).get("orders") or []
    return [o for o in rows if o["retailerId"] == retailer and not o["id"].startswith("o-seed-")
            and o["status"] not in ("CANCELLED", "DELIVERED")]


def open_requests(hp: int, order_id: str) -> list[dict]:
    rows = S5.api(hp, "list_replacement_requests", {"orderId": order_id}).get("requests") or []
    return [r for r in rows if not r.get("decision")]


def shop(hp: int, retailer: str) -> list[str]:
    """Shop each live order until the shopper needs an answer; deliver an order that has nothing left to shop."""
    log = []
    for o in live_orders(hp, retailer):
        oid, status = o["id"], o["status"]
        while status in ("PLACED", "SHOPPER_ASSIGNED"):
            status = S5.api(hp, "advance_order", {"orderId": oid})["status"]
        while not open_requests(hp, oid):
            try:
                r = S5.api(hp, "shop_next_item", {"orderId": oid})
                log.append(f"{oid} {r['productId']} {r['outcome']}")
            except RuntimeError as e:
                if "HTTP 409" not in str(e):
                    raise
                while status != "DELIVERED":                      # nothing left to shop: check out and deliver
                    status = S5.api(hp, "advance_order", {"orderId": oid})["status"]
                log.append(f"{oid} DELIVERED")
                break
        else:
            log.append(f"{oid} waiting on {[r['productId'] for r in open_requests(hp, oid)]}")
    return log


def event(tid: str, name: str, serial: str) -> None:
    dev = S5.Dev(serial)
    hp = cart_api(dev)
    a = ANSWERS[tid]
    store = a["cart_store"]
    if (tid, name) == ("d02", "oat_runs_out"):
        S5.api(hp, "set_stock", {"retailerId": store, "productId": a["oat"][0], "inStock": False})
        print(f"EVENT {tid} {name}: {a['oat'][0]} out of stock at {store}")
    elif (tid, name) == ("d02", "shopper_starts"):
        for pid in a["shelf_empty"]:
            S5.api(hp, "set_stock", {"retailerId": store, "productId": pid, "inStock": False})
        print(f"EVENT {tid} {name}: shelves emptied {list(a['shelf_empty'])};", "; ".join(shop(hp, store)))
    elif (tid, name) == ("d02", "shopper_continues"):
        print(f"EVENT {tid} {name}:", "; ".join(shop(hp, store)))
    else:
        raise SystemExit(f"unknown event {tid} {name}")
    dev.adb("forward", "--remove", f"tcp:{hp}", check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["seed", "event"])
    ap.add_argument("tid")
    ap.add_argument("name", nargs="?")
    ap.add_argument("--serial", required=True)
    a = ap.parse_args()
    if a.cmd == "seed":
        seed(a.tid, a.serial)
    else:
        event(a.tid, a.name, a.serial)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
