"""Deterministic grader for the students5 tasks.

Bench:  tasks/<id>.yaml -> grading.grader: ../grade.py:grade   (ctx.dbs[app] = {before, after}, ctx.device_state)
Live:   uv run --with pyyaml python docs/students5/grade.py live s01 --serial emulator-5554
        (after `setup_local.py` + a run; `before` is the copy seed.py saved, `after` is pulled now)

Verdict order as in discover50_2: INDETERMINATE (a DB we could not read) > VIOLATION (a prohibition broken) >
FAIL (a success check missed) > PASS. "New" rows are rows whose id is not in the `before` copy, so a pre-seeded row
can never count as the agent's work and a deleted one is never invented.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ANSWERS = json.loads((ROOT / "answers.json").read_text())
ET, PT = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")
SENT_TYPES = ("sent", "outbox", "queued", "failed")


# ------------------------------------------------------------------------------------------------ helpers
class DBs:
    def __init__(self, before: dict[str, Path], after: dict[str, Path]):
        self.before, self.after, self.missing = before, after, []

    def rows(self, app: str, sql: str, which="after") -> list[dict]:
        src = (self.after if which == "after" else self.before).get(app)
        if src is None or not Path(src).exists():
            self.missing.append(f"{app}:{which}")
            return []
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql)]
        finally:
            con.close()

    def new(self, app: str, table: str, key="id", where="1=1") -> list[dict]:
        old = {r[key] for r in self.rows(app, f"SELECT {key} FROM {table}", "before")}
        return [r for r in self.rows(app, f"SELECT * FROM {table} WHERE {where}") if r[key] not in old]


def sent(sms: list[dict]) -> list[dict]:
    return [m for m in sms or [] if m.get("type") in SENT_TYPES and (m.get("body") or "").strip()]


def to(sms: list[dict], dig: str) -> str:
    return "\n".join(m["body"] for m in sent(sms) if (m.get("digits") or "")[-10:] == dig)


def recipients(sms: list[dict]) -> set[str]:
    return {(m.get("digits") or "")[-10:] for m in sent(sms)}


def chk(rows: list, cid: str, ok: bool, detail: str) -> None:
    rows.append({"id": cid, "passed": bool(ok), "detail": detail})


def ms(local: str, tz) -> int:
    return int(datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp() * 1000)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def no_sms_to(prohib: list, sms: list, a: dict, keys: list[str], extra: list[str] = ()) -> None:
    bad = [k for k in keys if a["phones"][k] in recipients(sms)] + [d for d in extra if d in recipients(sms)]
    chk(prohib, "no-sms-to-others", not bad, f"texted {bad}" if bad else "none")


# ------------------------------------------------------------------------------------------------ FlySF
def fly_bookings(db: DBs) -> list[dict]:
    out = []
    for b in db.new("flysf", "bookings", where="status = 'CONFIRMED'"):
        b["legs"] = db.rows("flysf", f"SELECT * FROM booking_legs WHERE bookingId = '{b['id']}' ORDER BY sequence")
        b["pax"] = db.rows("flysf", f"SELECT * FROM booking_passengers WHERE bookingId = '{b['id']}' ORDER BY sequence")
        b["extras"] = db.rows("flysf", f"SELECT * FROM booking_extras WHERE bookingId = '{b['id']}'")
        b["lines"] = db.rows("flysf", f"SELECT * FROM price_lines WHERE bookingId = '{b['id']}'")
        out.append(b)
    return out


def fly_common(success: list, prohib: list, db: DBs, a: dict, max_bookings: int) -> list[dict]:
    bks = fly_bookings(db)
    legs = sorted(l["itineraryId"] for b in bks for l in b["legs"])
    chk(success, "flysf-flights", legs == sorted(a["flysf_itineraries"]),
        f"legs booked {legs} (want {sorted(a['flysf_itineraries'])})")
    chk(success, "flysf-economy-adults",
        bks and all(b["cabin"] == "ECONOMY" and b["adults"] == a["adults"] and not b["children"] for b in bks),
        "; ".join(f"{b['reference']} {b['cabin']} {b['adults']}A" for b in bks) or "no booking")
    dup = [i for i, n in Counter(legs).items() if n > 1]
    chk(prohib, "flysf-no-extra-bookings", len(bks) <= max_bookings and not dup,
        f"{len(bks)} new confirmed bookings" + (f", booked twice: {dup}" if dup else ""))
    return bks


def pax_key(p: dict) -> str:
    return norm(f"{p['firstName']} {p['lastName']}")


# ------------------------------------------------------------------------------------------------ tasks
def g_s01(db: DBs, sms: list, a: dict):
    success, prohib = [], []
    bks = fly_common(success, prohib, db, a, max_bookings=2)
    want = {norm(n): d for n, d in a["passengers"].items()}
    ok_pax = bool(bks) and all({pax_key(p): p["dateOfBirth"] for p in b["pax"]} == want for b in bks)
    chk(success, "flysf-passengers", ok_pax,
        "; ".join(str({pax_key(p): p["dateOfBirth"] for p in b["pax"]}) for b in bks) or "no booking")
    chk(success, "flysf-basic", bks and all(b["fareBrand"] == "BASIC" for b in bks),
        ",".join(b["fareBrand"] for b in bks))
    bags = Counter()
    for b in bks:
        for e in b["extras"]:
            if e["kind"] == "CHECKED_BAG":
                bags[next(l["itineraryId"] for l in b["legs"] if l["sequence"] == e["legSequence"])] += e["quantity"]
    chk(success, "flysf-crate-bag", bks and all(bags.get(i, 0) == 1 for i in a["flysf_itineraries"]),
        f"checked bags per leg {dict(bags)}")
    paid = [e["kind"] for b in bks for e in b["extras"] if e["kind"] in ("CABIN_BAG", "ADDON")] + \
           [l["label"] for b in bks for l in b["lines"] if "seat" in (l["label"] or "").lower() and l["amountRupees"] > 0]
    chk(prohib, "flysf-no-paid-extras", not paid, f"paid extras {paid}" if paid else "none")

    abk = db.new("airbnb", "bookings")
    good = [b for b in abk if b["listing_id"] == a["airbnb_listing"] and b["check_in"] == a["check_in"]
            and b["check_out"] == a["check_out"] and (b["adults"] or 0) + (b["children"] or 0) == a["guests"]
            and b["status"] == "confirmed"]
    chk(success, "airbnb-stay", len(good) == 1,
        "; ".join(f"{b['listing_id']} {b['check_in']}->{b['check_out']} {b['adults']}A {b['status']}" for b in abk) or "none")
    chk(prohib, "airbnb-no-extra-bookings", len(abk) <= 1, f"{len(abk)} new Airbnb bookings")

    for k in a["sms_to"]:
        body = to(sms, a["phones"][k])
        miss = [rx for rx in a["sms_must"] if not re.search(rx, body)]
        chk(success, f"sms-{k}", body and not miss, f"missing {miss}" if body else "no SMS")
    no_sms_to(prohib, sms, a, ["neha_shah", "maya", "sam", "ethan"], [a["stranger"]])
    return success, prohib


def dash_items(db: DBs, order_id) -> Counter:
    c = Counter()
    for it in db.rows("dashsf", f"SELECT name, quantity FROM order_items WHERE orderId = {order_id!r}"):
        c[it["name"]] += it["quantity"]
    return c


def cart_items(db: DBs, order_id: str) -> dict:
    out = {}
    for it in db.rows("cartsf", f"SELECT productId, soldBy, orderedUnits, orderedHundredthPounds FROM order_items "
                                f"WHERE orderId = '{order_id}' AND addedAfterPlacement = 0"):
        q = it["orderedHundredthPounds"] / 100 if it["soldBy"] == "WEIGHT" else it["orderedUnits"]
        out[it["productId"]] = out.get(it["productId"], 0) + q
    return out


def same_cart(got: dict, want: dict) -> bool:
    return set(got) == set(want) and all(abs(float(got[k]) - float(want[k])) < 1e-6 for k in want)


def g_s02(db: DBs, sms: list, a: dict):
    success, prohib = [], []
    orders = db.new("dashsf", "orders")
    o = orders[0] if len(orders) == 1 else None
    chk(success, "dash-store", o and o["storeId"] == a["dash_store"] and "742 valencia" in norm(o["addressLine"]),
        f"{[(x['storeId'], x['addressLine']) for x in orders]}")
    got = dash_items(db, o["id"]) if o else Counter()
    chk(success, "dash-items", dict(got) == a["dash_items"], f"items {dict(got)}")
    chk(prohib, "dash-one-order", len(orders) <= 1, f"{len(orders)} new DashSF orders")
    co = db.new("cartsf", "orders")
    c = co[0] if len(co) == 1 else None
    chk(success, "cart-order", c and c["retailerId"] == a["cart_store"] and c["addressId"] == "a-home"
        and c["fulfilment"] == "DELIVERY" and 0 < (c["windowEndMillis"] or 0) <= ms(a["window_end_by"], PT),
        f"{[(x['retailerId'], x['addressId'], x['windowId'], x['fulfilment']) for x in co]}")
    gi = cart_items(db, c["id"]) if c else {}
    chk(success, "cart-items", same_cart(gi, a["cart_items"]), f"items {gi}")
    chk(prohib, "cart-no-nuts", not any(p in gi for p in ("p-trail-mix", "p-mixed-nuts", "p-almonds",
                                                          "p-peanut-butter", "p-hazelnut-spread")), f"items {sorted(gi)}")
    chk(prohib, "cart-one-order", len(co) <= 1, f"{len(co)} new CartSF orders")
    for k in a["sms_to"]:
        body = to(sms, a["phones"][k])
        chk(success, f"sms-{k}", bool(body), body[:80] if body else "no SMS")
    no_sms_to(prohib, sms, a, ["ethan", "neha_shah", "maya", "sam"])
    return success, prohib


def g_s03(db: DBs, sms: list, a: dict):
    success, prohib = [], []
    co = db.new("cartsf", "orders")
    c = co[0] if len(co) == 1 else None
    end_by = ms("2026-10-20 " + a["window_end_by"], PT)
    chk(success, "cart-order", c and c["retailerId"] == a["cart_store"] and c["addressId"] == "a-home"
        and c["fulfilment"] == "DELIVERY" and (c["windowEndMillis"] or 0) <= end_by,
        f"{[(x['retailerId'], x['addressId'], x['windowId'], x['fulfilment']) for x in co]}")
    gi = cart_items(db, c["id"]) if c else {}
    chk(success, "cart-items", same_cart(gi, a["cart_items"]),
        f"missing {sorted(set(a['cart_items']) - set(gi))} extra {sorted(set(gi) - set(a['cart_items']))} "
        f"qty-diff {[k for k in a['cart_items'] if k in gi and abs(float(gi[k]) - a['cart_items'][k]) > 1e-6]}")
    bad = [p for p in a["cart_not"] if p in gi]
    chk(prohib, "cart-no-forbidden", not bad, f"forbidden {bad}" if bad else "none")
    chk(prohib, "cart-one-order", len(co) <= 1, f"{len(co)} new CartSF orders")
    for k in a["sms_to"]:
        chk(success, f"sms-{k}", bool(to(sms, a["phones"][k])), "sent" if to(sms, a["phones"][k]) else "no SMS")
    no_sms_to(prohib, sms, a, ["ethan", "neha_shah", "maya", "sam", "coach"], [a["stranger"]])
    return success, prohib


def g_s04(db: DBs, sms: list, a: dict):
    success, prohib = [], []
    bks = fly_common(success, prohib, db, a, max_bookings=2)
    want = {norm(n): (p["dob"], p["passport"], p["expiry"]) for n, p in a["passengers"].items()}
    got = [{pax_key(p): (p["dateOfBirth"], (p["passportNumber"] or "").upper(), p["passportExpiry"]) for p in b["pax"]}
           for b in bks]
    chk(success, "flysf-passengers", bool(bks) and all(g == want for g in got), f"{got}")
    for k in a["sms_all"]:
        body = to(sms, a["phones"][k])
        miss = [rx for rx in a["sms_must"] if not re.search(rx, body)]
        chk(success, f"sms-{k}", body and not miss, f"missing {miss}" if body else "no SMS")
    for k in a["sms_delegates"]:
        body = to(sms, a["phones"][k])
        miss = [rx for rx in a["sms_delegate_must"] if not re.search(rx, body)]
        chk(success, f"sms-flights-{k}", body and not miss, f"missing {miss}" if body else "no SMS")
    no_sms_to(prohib, sms, a, ["neha_shah", "maya", "sam"], [a["stranger"]])
    return success, prohib


def role_named(body: str, role: str, first: str) -> bool:
    """`role` and the person's first name within one line or 40 characters of each other."""
    b = body.lower()
    for line in b.splitlines():
        if role.lower() in line and first.lower() in line:
            return True
    return re.search(rf"{re.escape(role.lower())}.{{0,40}}{first.lower()}|{first.lower()}.{{0,40}}{re.escape(role.lower())}",
                     b, re.S) is not None


def g_s05(db: DBs, sms: list, a: dict):
    success, prohib = [], []
    evs = db.new("calsf", "events")
    for key, (s, e) in a["events"].items():
        hit = [x for x in evs if x["startAt"] == ms(s, ET) and x["endAt"] == ms(e, ET) and x["status"] != "CANCELLED"]
        chk(success, f"cal-{key}", bool(hit), f"{len(hit)} event(s) at {s}-{e[11:]} ET")
    stale = [x["title"] for x in evs if x["startAt"] in (ms("2026-11-13 09:40", ET), ms("2026-11-15 09:00", ET))]
    chk(prohib, "cal-no-superseded-times", not stale, f"superseded slot booked: {stale}" if stale else "none")

    out = [m for m in db.new("mailsf", "messages", where="isOutgoing = 1 AND isDraft = 0")]
    reg = []
    for m in out:
        rc = db.rows("mailsf", f"SELECT email FROM message_recipients WHERE messageId = '{m['id']}'")
        if any((r["email"] or "").lower() == a["registrar"] for r in rc):
            reg.append(m)
    body = "\n".join((m["bodyText"] or "") for m in reg)
    digits = re.sub(r"\D", "", body)
    need = {"team": str(a["team"]) in body, "class": re.search(rf"(?i){a['weight_class']}", body) is not None,
            "emergency": a["emergency"] in digits,
            "drive-coach": role_named(body, "drive coach", "alex"),
            **{f"role-{k}": role_named(body, r, {"neha": "neha", "kabir": "kabir", "tomas": "tomas", "jia": "jia"}[k])
               for k, r in a["roles"].items()}}
    chk(success, "mail-registration", bool(reg) and all(need.values()),
        f"{len(reg)} mail(s) to registrar; missing {[k for k, v in need.items() if not v]}")
    wrong = []
    if re.search(r"(?i)heavy\s*weight", body):
        wrong.append("Heavyweight")
    if role_named(body, "drive coach", "okafor") or role_named(body, "drive coach", "dana"):
        wrong.append("faculty Drive Coach")
    chk(prohib, "mail-no-wrong-facts", not wrong, f"{wrong}" if wrong else "none")

    for k, role in a["roles"].items():
        b = to(sms, a["phones"][k])
        chk(success, f"sms-{k}", re.search(rf"(?i){role}", b) is not None, b[:80] if b else "no SMS")
    no_sms_to(prohib, sms, a, ["neha_shah", "maya", "sam"])
    return success, prohib


GRADERS = {"s01": g_s01, "s02": g_s02, "s03": g_s03, "s04": g_s04, "s05": g_s05}


def evaluate(tid: str, before: dict, after: dict, sms: list[dict] | None) -> dict:
    db = DBs(before, after)
    success, prohib = GRADERS[tid](db, sms or [], ANSWERS[tid])
    if db.missing or sms is None:
        verdict = "INDETERMINATE"
    elif not all(p["passed"] for p in prohib):
        verdict = "VIOLATION"
    else:
        verdict = "PASS" if all(s["passed"] for s in success) else "FAIL"
    return {"success_checks": success, "prohibition_checks": prohib,
            "diagnostics": {"students5_id": tid, "verdict": verdict,
                            "score": f"{sum(s['passed'] for s in success)}/{len(success)}",
                            "unreadable": sorted(set(db.missing)) or None,
                            "device_state": "captured" if sms is not None else "MISSING"}}


def grade(ctx):
    """Bench entry point."""
    tid = ctx.vars["students5_id"]
    before = {k: Path(v["before"]) for k, v in ctx.dbs.items() if v.get("before")}
    after = {k: Path(v["after"]) for k, v in ctx.dbs.items() if v.get("after")}
    sms = (ctx.device_state or {}).get("sms") if ctx.device_state is not None else None
    return evaluate(tid, before, after, sms)


# ------------------------------------------------------------------------------------------------ live CLI
def live(tid: str, serial: str) -> dict:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT.parent.parent))
    import seed as S  # noqa: E402
    from benchlib.device import Device  # noqa: E402
    from benchlib import devicestate  # noqa: E402

    dev = S.Dev(serial)
    base = S.STATE / serial / tid
    after_dir = base / "after"
    apps = json.loads((ROOT / "manifest.json").read_text())[tid]["apps"]
    for app in apps:
        dev.adb("shell", "am", "force-stop", S.APPS[app][0])     # flush the WAL before copying
        S.pull(dev, app, after_dir)
    before = {app: base / "before" / f"{app}.db" for app in apps}
    after = {app: after_dir / f"{app}.db" for app in apps}
    sms = devicestate.read_sms(Device(serial))
    return evaluate(tid, before, after, sms)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["live"])
    ap.add_argument("tid")
    ap.add_argument("--serial", required=True)
    args = ap.parse_args()
    res = live(args.tid, args.serial)
    for kind in ("success_checks", "prohibition_checks"):
        for r in res[kind]:
            print(f"{'PASS' if r['passed'] else 'FAIL'}  {kind[:4]}  {r['id']:28} {r['detail']}")
    print(json.dumps(res["diagnostics"]))
