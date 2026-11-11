"""discover50_2 seeder (copied from docs/trap50/seeder.py): loads the three-section seed format (docs: ~/Documents/grader/docs/seed-format.md) and runs it.

    python3 docs/discover50_2/seeder.py check  [task ...]              # format only (no databases touched)
    python3 docs/discover50_2/seeder.py build  [task ...|all]          # host: base + app_state SQL -> build/<task>/<app>.db
    python3 docs/discover50_2/seeder.py seed    <task> --serial S      # device: steps 1-6 of the execution order
    python3 docs/discover50_2/seeder.py cleanup <task> --serial S      # device: step 7 (+ 8 with --restore-snapshot)
    python3 docs/discover50_2/seeder.py pull    --serial S --out DIR   # copy the six app DBs off a device (WAL folded in)

A task is named by its id prefix (`t07`) or the scenario file stem. Exit codes: 0 ok, 2 bad input, 3 seed INVALID
(a read-back did not match: the trial must not be scored), 4 cleanup did not verify.

How the sections map onto the six bench apps
  baseline.snapshot   emulator snapshot (`bench_clean` on the EC2 host). Loaded only with --restore-snapshot: inside a
                      bench run the runner owns the device, so the task YAML calls `seed` without it.
  baseline.apps       trap50 extension: every app whose base DB (docs/discover50_2/base/<app>.db) is pushed, seeded rows or not.
  app_state[]         SQL is applied on the host to a copy of the base DB (`build`), read back there (`verify`, foreign
                      keys, cleanup dry-run), then the whole file is pushed with run-as, the WAL dropped, and `verify`
                      runs again on a copy pulled back off the device. MailSF / CalSF also get their DataStore session.
  device_state        clock (cmd alarm), location (emu geo fix), network (svc wifi / svc data, airplane mode), battery
                      (dumpsys battery), settings (settings put). Previous values are saved and restored by `cleanup`.
  files[]             pushed with adb; `media_scan: true` broadcasts MEDIA_SCANNER_SCAN_FILE.
  source_assertions[] trap50 extension: {kind: APP_DB, app, query, expect} runs on the pulled DB after seeding.
  stock_apps          discover50_2 extension: {contacts, sms_in} for Contacts / Messages, applied by the task's setup steps.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import devicedb  # noqa: E402
BENCH = ROOT.parent.parent
BASE, SCEN, BUILD, STATE = ROOT / "base", ROOT / "scenarios", ROOT / "build", ROOT / ".state"
TASKS = ROOT / "tasks"

# app key -> (package, on-device DB file name, launch activity)
APPS = {
    "mailsf": ("dev.drizz.mailsf", "mailsf.db", "dev.drizz.mailsf.MainActivity"),
    "calsf": ("dev.drizz.calsf", "calsf.db", "dev.drizz.calsf.MainActivity"),
    "airbnb": ("dev.drizz.airbnb", "airbnb.db", "dev.drizz.airbnb.MainActivity"),
    "ridesf": ("dev.drizz.ridesf.bench", "ridesf.db", None),
    "dashsf": ("dev.drizz.dashsf.bench", "dashsf.db", "dev.drizz.dashsf.MainActivity"),
    "shopsf": ("dev.drizz.shopsf.bench", "shopsf.db", "dev.drizz.shopsf.MainActivity"),
}
PKG_APP = {v[0]: k for k, v in APPS.items()}
SESSION_APPS = ("mailsf", "calsf")

TEXT_DIRS = ("/sdcard/Documents/", "/sdcard/Download/", "/data/local/tmp/")
MEDIA_DIRS = ("/sdcard/Pictures/", "/sdcard/DCIM/")
DEVICE_KEYS = {"clock", "location", "network", "battery", "settings", "app_prefs"}
MARKER_RE = re.compile(r"^DRZBENCH-D52-X?[A-Z][0-9]{2}$")   # X: a variant outside the 50 (XA01)


class SeedError(Exception):
    code = 2


class Invalid(SeedError):
    code = 3


class CleanupFailed(SeedError):
    code = 4


# ============================================================================================== loading + format check
def scenario_path(name: str) -> Path:
    p = Path(name)
    if p.suffix in (".yaml", ".yml") and p.exists():
        return p
    hits = sorted(SCEN.glob(f"{name}*.yaml"))
    if len(hits) != 1:
        raise SeedError(f"task {name!r}: expected one scenarios/{name}*.yaml, found {[h.name for h in hits]}")
    return hits[0]


def load(name: str) -> tuple[Path, dict]:
    p = scenario_path(name)
    doc = yaml.safe_load(p.read_text()) or {}
    scs = doc.get("scenarios") or []
    if len(scs) != 1:
        raise SeedError(f"{p.name}: discover50_2 files hold exactly one scenario, found {len(scs)}")
    return p, scs[0]


def _q(v) -> bool:
    return isinstance(v, str)


def check(p: Path, sc: dict) -> list[str]:
    """Format problems (empty list = well formed). Mirrors the table rules in seed-format.md."""
    errs = []
    for k in ("task_id", "source_app"):
        if not sc.get(k):
            errs.append(f"missing {k}")
    if sc.get("source_app") and sc["source_app"] not in PKG_APP:
        errs.append(f"source_app {sc['source_app']!r} is not a bench package")
    apps = (sc.get("baseline") or {}).get("apps") or []
    for a in apps:
        if a not in APPS:
            errs.append(f"baseline.apps: unknown app {a!r}")
    markers = set()
    for i, b in enumerate(sc.get("app_state") or []):
        w = f"app_state[{i}]"
        for k in ("package", "database", "marker", "statements"):
            if not b.get(k):
                errs.append(f"{w}: missing {k}")
        pkg = b.get("package")
        if pkg and pkg not in PKG_APP:
            errs.append(f"{w}: unknown package {pkg}")
        elif pkg:
            want = f"/data/data/{pkg}/databases/{APPS[PKG_APP[pkg]][1]}"
            if b.get("database") != want:
                errs.append(f"{w}: database must be {want}")
            if PKG_APP[pkg] not in apps:
                errs.append(f"{w}: {PKG_APP[pkg]} is not in baseline.apps")
        if b.get("marker") and not MARKER_RE.match(str(b["marker"])):
            errs.append(f"{w}: marker {b['marker']!r} must look like DRZBENCH-D52-A07")
        markers.add(b.get("marker"))
        v = b.get("verify") or {}
        if not (v.get("query") and _q(v.get("expect"))):
            errs.append(f"{w}: verify needs query and a quoted expect")
        c = b.get("cleanup") or {}
        cv = c.get("verify") or {}
        if not (c.get("statements") and cv.get("query") and _q(cv.get("expect"))):
            errs.append(f"{w}: cleanup needs statements and verify {{query, quoted expect}}")
        if b.get("marker") and b["marker"] not in str(c.get("statements", "")) and "-- ids:" not in str(c.get("statements", "")):
            errs.append(f"{w}: cleanup must target the marker (or list ids after a '-- ids:' comment)")
    if len(markers - {None}) > 1:
        errs.append(f"one marker per task, found {sorted(m for m in markers if m)}")
    ds = sc.get("device_state") or {}
    for k in ds:
        if k not in DEVICE_KEYS:
            errs.append(f"device_state.{k} is not a typed key")
    clock = ds.get("clock") or {}
    if clock:
        try:
            dt = datetime.fromisoformat(str(clock.get("at")))
            if dt.tzinfo is None:
                errs.append("device_state.clock.at needs a UTC offset")
        except ValueError:
            errs.append(f"device_state.clock.at {clock.get('at')!r} is not ISO-8601")
        if not clock.get("timezone"):
            errs.append("device_state.clock.timezone missing")
    for k, v in (ds.get("network") or {}).items():
        if v not in ("on", "off"):
            errs.append(f"device_state.network.{k} must be 'on' or 'off'")
    if ds.get("battery") and not _q((ds["battery"] or {}).get("level")):
        errs.append("device_state.battery.level must be a quoted string")
    for s in ds.get("settings") or []:
        if s.get("namespace") not in ("system", "secure", "global") or not _q(s.get("value")):
            errs.append(f"device_state.settings {s.get('key')}: namespace system|secure|global, quoted value")
    if ds.get("app_prefs"):
        errs.append("device_state.app_prefs: not supported by the discover50_2 seeder (leave [])")
    for f in sc.get("files") or []:
        path = str(f.get("path", ""))
        if not path.startswith("/") or ".." in path:
            errs.append(f"files {path!r}: absolute path without '..'")
        if ("text" in f) == ("source" in f):
            errs.append(f"files {path}: exactly one of text / source")
        if "source" in f:
            if not re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256", ""))):
                errs.append(f"files {path}: sha256 (64 hex) required with source")
            elif (p.parent / f["source"]).exists() and sha(p.parent / f["source"]) != f["sha256"]:
                errs.append(f"files {path}: sha256 does not match {f['source']}")
            elif not (p.parent / f["source"]).exists():
                errs.append(f"files {path}: source {f['source']} not found")
        allowed = MEDIA_DIRS if re.search(r"\.(jpe?g|png|gif|webp|mp4)$", path, re.I) else TEXT_DIRS
        if not path.startswith(allowed):
            errs.append(f"files {path}: not in an allowed folder {allowed}")
    for a in sc.get("source_assertions") or []:
        if a.get("kind") != "APP_DB" or a.get("app") not in APPS or not a.get("query") or not _q(a.get("expect")):
            errs.append(f"source_assertions: {{kind: APP_DB, app, query, quoted expect}} expected, got {a}")
    errs += check_stock_apps(sc.get("stock_apps") or {})
    if not (sc.get("final_restore") or {}).get("snapshot"):
        errs.append("final_restore.snapshot missing")
    return errs


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def fairness_hash(p: Path, sc: dict) -> str:
    """Hash of everything that sets the start state: the scenario, base DB hashes, file bytes."""
    lock = json.loads((BASE / "base.lock.json").read_text())
    apps = (sc.get("baseline") or {}).get("apps") or []
    files = {f["path"]: f.get("sha256") or hashlib.sha256(f.get("text", "").encode()).hexdigest() for f in sc.get("files") or []}
    blob = json.dumps({"scenario": sc, "base": {a: lock[f"{a}.db"]["sha256"] for a in apps}, "files": files},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# ============================================================================================== host build
def flat(rows) -> str:
    return ";".join("|".join("-" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
                             for v in r) for r in rows)


def query(db: Path, sql: str) -> str:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return flat(con.execute(sql).fetchall())
    finally:
        con.close()


def apply_sql(db: Path, sql: str) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
        bad = con.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise SeedError(f"{db.name}: foreign key violations after the script: {bad[:5]}")
    except sqlite3.Error as e:
        raise SeedError(f"{db.name}: SQL failed: {e}") from e
    finally:
        con.close()


def lint(app: str, db: Path) -> list[str]:
    """App invariants a hand-written seed breaks without the app noticing until it misbehaves."""
    out = []
    con = sqlite3.connect(db)
    try:
        if app == "mailsf":
            for tid, mc, uc, n, u in con.execute(
                    "SELECT t.id, t.messageCount, t.unreadCount, COUNT(m.id), SUM(m.isUnread AND NOT m.isDraft) "
                    "FROM threads t LEFT JOIN messages m ON m.threadId = t.id GROUP BY t.id"):
                if mc != n or uc != (u or 0):
                    out.append(f"mailsf thread {tid}: messageCount/unreadCount {mc}/{uc} but rows say {n}/{u or 0}")
            if not con.execute("SELECT COUNT(*) FROM threads").fetchone()[0]:
                out.append("mailsf: threads is empty, the app would wipe and reseed itself")
        elif app == "calsf":
            for sid, oc, n in con.execute("SELECT s.id, s.occurrenceCount, COUNT(e.id) FROM event_series s "
                                          "LEFT JOIN events e ON e.seriesId = s.id GROUP BY s.id"):
                if oc != n:
                    out.append(f"calsf series {sid}: occurrenceCount {oc} but {n} events")
            if not con.execute("SELECT COUNT(*) FROM events").fetchone()[0]:
                out.append("calsf: events is empty, the app would wipe and reseed itself")
        elif app == "ridesf":
            c = int(con.execute("SELECT value FROM app_settings WHERE name = 'rng.ride_counter'").fetchone()[0])
            mx = con.execute("SELECT MAX(CAST(substr(id, 6) AS INTEGER)) FROM rides WHERE id LIKE 'ride-%'").fetchone()[0] or 0
            if c < mx:
                out.append(f"ridesf: rng.ride_counter {c} < highest seeded ride number {mx} (new rides would collide)")
        elif app == "dashsf":
            c = int(con.execute("SELECT value FROM app_settings WHERE name = 'seq.order_counter'").fetchone()[0])
            mx = con.execute("SELECT MAX(CAST(substr(id, 5) AS INTEGER)) FROM orders WHERE id LIKE 'ord-%'").fetchone()[0] or 0
            if c < mx:
                out.append(f"dashsf: seq.order_counter {c} < highest seeded order number {mx}")
        elif app == "shopsf":
            row = con.execute("SELECT value FROM app_settings WHERE name = 'rng.order_counter'").fetchone()
            n = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            if row and int(row[0]) < n:
                out.append(f"shopsf: rng.order_counter {row[0]} < {n} orders (new order ids could collide)")
    finally:
        con.close()
    return out


def build(name: str, quiet=False) -> dict:
    p, sc = load(name)
    errs = check(p, sc)
    if errs:
        raise SeedError(f"{p.name}: " + "; ".join(errs))
    tid = sc["task_id"]
    out = BUILD / tid
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    apps = sc["baseline"]["apps"]
    for a in apps:
        shutil.copy(BASE / f"{a}.db", out / f"{a}.db")
    report = {"task_id": tid, "scenario": p.name, "fairness_sha256": fairness_hash(p, sc), "blocks": [], "lint": []}
    for b in sc.get("app_state") or []:
        app = PKG_APP[b["package"]]
        db = out / f"{app}.db"
        apply_sql(db, str(b["statements"]))
        got = query(db, b["verify"]["query"])
        if got != b["verify"]["expect"]:
            raise Invalid(f"{tid} {app}: verify returned {got!r}, expected {b['verify']['expect']!r}")
        with tempfile.TemporaryDirectory() as td:            # cleanup dry-run on a copy
            cp = Path(td) / db.name
            shutil.copy(db, cp)
            apply_sql(cp, str(b["cleanup"]["statements"]))
            cg = query(cp, b["cleanup"]["verify"]["query"])
            if cg != b["cleanup"]["verify"]["expect"]:
                raise CleanupFailed(f"{tid} {app}: cleanup verify returned {cg!r}, expected {b['cleanup']['verify']['expect']!r}")
        report["blocks"].append({"app": app, "verify": got, "cleanup_verify": cg})
    for a in apps:
        report["lint"] += lint(a, out / f"{a}.db")
        con = sqlite3.connect(out / f"{a}.db")
        con.execute("VACUUM")
        con.close()
    for s in sc.get("source_assertions") or []:
        got = query(out / f"{s['app']}.db", s["query"])
        if got != s["expect"]:
            raise Invalid(f"{tid}: source assertion on {s['app']} returned {got!r}, expected {s['expect']!r}")
    if report["lint"]:
        raise SeedError(f"{tid}: " + "; ".join(report["lint"]))
    report["dbs"] = {a: sha(out / f"{a}.db") for a in apps}
    sa = sc.get("stock_apps") or {}
    if sa:                       # start state of the `device` pseudo-app (Messages + Contacts) for the checkpoints
        devicedb.start_state(out / "device.db", sa.get("contacts") or [], sa.get("sms_in") or [],
                             sc["device_state"]["clock"]["at"])
        report["dbs"]["device"] = sha(out / "device.db")
    (out / "build.json").write_text(json.dumps(report, indent=1) + "\n")
    if not quiet:
        blocks = ", ".join(f"{x['app']}={x['verify']}" for x in report["blocks"]) or "no app_state"
        print(f"  {tid}: {blocks}  [{report['fairness_sha256'][:12]}]")
    return report


# ============================================================================================== device
class Dev:
    def __init__(self, serial: str):
        self.serial = serial

    def adb(self, *a, check=True, binary=False, timeout=120):
        r = subprocess.run(["adb", "-s", self.serial, *a], capture_output=True, text=not binary, timeout=timeout)
        if check and r.returncode != 0:
            raise SeedError(f"adb {' '.join(a)} failed: {(r.stderr or '')[-400:]}")
        return r

    def sh(self, cmd: str, check=False) -> str:
        return (self.adb("shell", cmd, check=check).stdout or "").strip()

    def run_as(self, pkg: str, cmd: str):
        return self.adb("shell", "run-as", pkg, "sh", "-c", f"'{cmd}'")


def push_db(dev: Dev, app: str, src: Path) -> None:
    pkg, dbname, _ = APPS[app]
    dev.adb("shell", "am", "force-stop", pkg)
    dev.adb("push", str(src), f"/data/local/tmp/{dbname}")
    dev.adb("shell", "chmod", "644", f"/data/local/tmp/{dbname}")
    cmds = [f"cp /data/local/tmp/{dbname} databases/{dbname}", f"rm -f databases/{dbname}-wal databases/{dbname}-shm"]
    if app in SESSION_APPS:
        pb = f"{app}_session.preferences_pb"
        dev.adb("push", str(BASE / pb), f"/data/local/tmp/{pb}")
        dev.adb("shell", "chmod", "644", f"/data/local/tmp/{pb}")
        cmds += ["mkdir -p files/datastore", f"cp /data/local/tmp/{pb} files/datastore/{pb}"]
    dev.run_as(pkg, "mkdir -p databases && " + " && ".join(cmds))
    dev.adb("shell", "rm", "-f", f"/data/local/tmp/{dbname}")


def pull_db(dev: Dev, app: str, out: Path) -> Path | None:
    pkg, dbname, _ = APPS[app]
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=out))
    ok = False
    for suffix in ("", "-wal", "-shm"):
        r = dev.adb("exec-out", "run-as", pkg, "cat", f"databases/{dbname}{suffix}", check=False, binary=True)
        if r.returncode == 0 and r.stdout:
            (tmp / f"{app}.db{suffix}").write_bytes(r.stdout)
            ok = ok or suffix == ""
    if not ok:
        shutil.rmtree(tmp)
        return None
    con = sqlite3.connect(tmp / f"{app}.db")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    dst = out / f"{app}.db"
    shutil.move(tmp / f"{app}.db", dst)
    shutil.rmtree(tmp)
    return dst


def launch(dev: Dev, app: str) -> None:
    pkg, _, act = APPS[app]
    if act:
        dev.sh(f"am start -n {pkg}/{act}")
    else:
        dev.sh(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")


def apply_device_state(dev: Dev, ds: dict, saved: dict) -> None:
    clock = ds.get("clock") or {}
    if clock:
        saved["timezone"] = dev.sh("getprop persist.sys.timezone")
        at = datetime.fromisoformat(clock["at"])
        dev.sh("settings put global auto_time 0")
        dev.sh("settings put global auto_time_zone 0")
        dev.sh(f"cmd alarm set-timezone {clock['timezone']}")
        dev.sh(f"cmd alarm set-time {int(at.timestamp() * 1000)}")
        now = int(dev.sh("date +%s") or 0)
        if abs(now - at.timestamp()) > 120:
            raise Invalid(f"clock read-back {now} is not {int(at.timestamp())}")
    loc = ds.get("location") or {}
    if loc:
        dev.adb("emu", "geo", "fix", loc["longitude"], loc["latitude"], loc.get("altitude_metres", "0"))
    net = ds.get("network") or {}
    if "wifi" in net:
        saved["wifi"] = dev.sh("settings get global wifi_on")
        dev.sh(f"svc wifi {'enable' if net['wifi'] == 'on' else 'disable'}")
    if "mobile_data" in net:
        saved["mobile_data"] = dev.sh("settings get global mobile_data")
        dev.sh(f"svc data {'enable' if net['mobile_data'] == 'on' else 'disable'}")
    if "airplane_mode" in net:
        saved["airplane_mode"] = dev.sh("settings get global airplane_mode_on")
        dev.sh(f"cmd connectivity airplane-mode {'enable' if net['airplane_mode'] == 'on' else 'disable'}")
    if (ds.get("battery") or {}).get("level"):
        saved["battery"] = True
        dev.sh(f"dumpsys battery set level {ds['battery']['level']}")
    saved["settings"] = []
    for s in ds.get("settings") or []:
        prev = dev.sh(f"settings get {s['namespace']} {s['key']}")
        saved["settings"].append({**s, "prev": prev})
        dev.sh(f"settings put {s['namespace']} {s['key']} {s['value']}")
        if dev.sh(f"settings get {s['namespace']} {s['key']}") != s["value"]:
            raise Invalid(f"setting {s['namespace']}/{s['key']} did not read back as {s['value']!r}")


def restore_device_state(dev: Dev, saved: dict) -> None:
    for s in reversed(saved.get("settings") or []):
        if s["prev"] in ("", "null"):
            dev.sh(f"settings delete {s['namespace']} {s['key']}")
        else:
            dev.sh(f"settings put {s['namespace']} {s['key']} {s['prev']}")
    if saved.get("battery"):
        dev.sh("dumpsys battery reset")
    if "airplane_mode" in saved:
        dev.sh(f"cmd connectivity airplane-mode {'enable' if saved['airplane_mode'] == '1' else 'disable'}")
    if "mobile_data" in saved:
        dev.sh(f"svc data {'enable' if saved['mobile_data'] == '1' else 'disable'}")
    if "wifi" in saved:
        dev.sh(f"svc wifi {'enable' if saved['wifi'] == '1' else 'disable'}")
    if saved.get("timezone"):
        dev.sh(f"cmd alarm set-timezone {saved['timezone']}")
    dev.sh("settings put global auto_time_zone 1")
    dev.sh("settings put global auto_time 1")


PHONE_RE = re.compile(r"^\+1 \d{3}-\d{3}-\d{4}$")


def check_stock_apps(sa: dict) -> list[str]:
    """discover50_2 extension `stock_apps`: {contacts: [{name, phone}], sms_in: [{from, name, body}]}. The bench task's
    setup steps (device_reset, contacts, sms_in) apply it on the device; `seed` never touches Contacts or Messages."""
    errs = []
    for k in sa:
        if k not in ("contacts", "sms_in"):
            errs.append(f"stock_apps.{k}: only contacts and sms_in")
    names = {}
    for c in sa.get("contacts") or []:
        if not c.get("name") or not PHONE_RE.match(str(c.get("phone", ""))):
            errs.append(f"stock_apps.contacts {c}: name and phone '+1 415-555-0123'")
        names[c.get("name")] = c.get("phone")
    for m in sa.get("sms_in") or []:
        if not m.get("body") or not PHONE_RE.match(str(m.get("from", ""))):
            errs.append(f"stock_apps.sms_in {m}: from '+1 415-555-0123' and body")
        elif m.get("name") and names.get(m["name"]) != m["from"]:
            errs.append(f"stock_apps.sms_in from {m['from']}: {m['name']!r} has a different number in contacts")
    return errs


def seed(name: str, serial: str, restore_snapshot: bool) -> None:
    p, sc = load(name)
    tid = sc["task_id"]
    built = BUILD / tid / "build.json"
    if not built.exists() or json.loads(built.read_text())["fairness_sha256"] != fairness_hash(p, sc):
        build(name, quiet=True)                                   # stale or missing: rebuild from the scenario
    dev = Dev(serial)
    if restore_snapshot and (sc.get("baseline") or {}).get("snapshot"):
        dev.adb("emu", "avd", "snapshot", "load", sc["baseline"]["snapshot"])
        dev.adb("wait-for-device")
    saved: dict = {}
    STATE.mkdir(exist_ok=True)
    try:
        apply_device_state(dev, sc.get("device_state") or {}, saved)
    finally:
        (STATE / f"{serial}.json").write_text(json.dumps({"task_id": tid, "device": saved}))
    blocks = {PKG_APP[b["package"]]: b for b in sc.get("app_state") or []}
    with tempfile.TemporaryDirectory() as td:
        for app in sc["baseline"]["apps"]:
            push_db(dev, app, BUILD / tid / f"{app}.db")
            got_db = pull_db(dev, app, Path(td) / app)
            if got_db is None:
                raise Invalid(f"{app}: database not readable after push")
            got = ""
            if app in blocks:
                got = query(got_db, blocks[app]["verify"]["query"])
                if got != blocks[app]["verify"]["expect"]:
                    raise Invalid(f"{tid} {app}: device read-back {got!r}, expected {blocks[app]['verify']['expect']!r}")
            print(f"  {app}: pushed{' + verified ' + repr(got) if app in blocks else ''}")
        for s in sc.get("source_assertions") or []:
            got_db = pull_db(dev, s["app"], Path(td) / ("sa-" + s["app"]))
            if got_db is None or query(got_db, s["query"]) != s["expect"]:
                raise Invalid(f"{tid}: source assertion on {s['app']} failed")
    for f in sc.get("files") or []:
        if "text" in f:
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=Path(f["path"]).suffix) as fh:
                fh.write(f["text"])
            src = Path(fh.name)
        else:
            src = p.parent / f["source"]
            if sha(src) != f["sha256"]:
                raise Invalid(f"{f['path']}: {src} sha256 changed")
        dev.adb("shell", "mkdir", "-p", str(Path(f["path"]).parent))
        dev.adb("push", str(src), f["path"])
        if f.get("media_scan"):
            dev.sh(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{f['path']}")
        print(f"  file: {f['path']}")
    for b in sc.get("app_state") or []:
        if b.get("launch_after"):
            launch(dev, PKG_APP[b["package"]])
    print(f"SEEDED {tid} on {serial} [{fairness_hash(p, sc)[:12]}]")


def cleanup(name: str, serial: str, restore_snapshot: bool) -> None:
    p, sc = load(name)
    tid, dev, problems = sc["task_id"], Dev(serial), []
    for f in reversed(sc.get("files") or []):
        dev.sh(f"rm -f '{f['path']}'")
        if dev.sh(f"ls '{f['path']}' 2>/dev/null"):
            problems.append(f"file {f['path']} still present")
    with tempfile.TemporaryDirectory() as td:
        for b in reversed(sc.get("app_state") or []):
            app = PKG_APP[b["package"]]
            dev.adb("shell", "am", "force-stop", APPS[app][0])
            db = pull_db(dev, app, Path(td) / app)
            if db is None:
                problems.append(f"{app}: database not readable")
                continue
            apply_sql(db, str(b["cleanup"]["statements"]))
            got = query(db, b["cleanup"]["verify"]["query"])
            if got != b["cleanup"]["verify"]["expect"]:
                problems.append(f"{app}: cleanup verify {got!r} != {b['cleanup']['verify']['expect']!r}")
            push_db(dev, app, db)
    st = STATE / f"{serial}.json"
    if st.exists():
        restore_device_state(dev, json.loads(st.read_text()).get("device") or {})
        st.unlink()
    if restore_snapshot and (sc.get("final_restore") or {}).get("snapshot"):
        dev.adb("emu", "avd", "snapshot", "load", sc["final_restore"]["snapshot"])
    if problems:
        raise CleanupFailed(f"{tid}: " + "; ".join(problems))
    print(f"CLEANED {tid} on {serial}")


# ============================================================================================== cli
def names(args: list[str]) -> list[str]:
    if not args or args == ["all"]:
        return [p.stem for p in sorted(SCEN.glob("*.yaml"))]
    return args


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("check", "build"):
        sub.add_parser(c).add_argument("tasks", nargs="*")
    for c in ("seed", "cleanup"):
        s = sub.add_parser(c)
        s.add_argument("task")
        s.add_argument("--serial", required=True)
        s.add_argument("--restore-snapshot", action="store_true", help="load baseline / final_restore snapshots")
    pl = sub.add_parser("pull")
    pl.add_argument("--serial", required=True)
    pl.add_argument("--out", required=True)
    a = ap.parse_args()
    try:
        if a.cmd == "check":
            bad = 0
            for n in names(a.tasks):
                p, sc = load(n)
                errs = check(p, sc)
                bad += bool(errs)
                print(f"  {p.stem}: " + ("ok" if not errs else "\n    - " + "\n    - ".join(errs)))
            return 2 if bad else 0
        if a.cmd == "build":
            fails = []
            for n in names(a.tasks):
                try:
                    build(n)
                except SeedError as e:
                    fails.append(n)
                    print(f"  {n}: FAILED {e}")
            print(f"built {len(names(a.tasks)) - len(fails)}/{len(names(a.tasks))}")
            return 2 if fails else 0
        if a.cmd == "seed":
            seed(a.task, a.serial, a.restore_snapshot)
        elif a.cmd == "cleanup":
            cleanup(a.task, a.serial, a.restore_snapshot)
        elif a.cmd == "pull":
            dev = Dev(a.serial)
            for app in APPS:
                got = pull_db(dev, app, Path(a.out))
                print(f"  {app}: {got or 'not pulled'}")
        return 0
    except SeedError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
