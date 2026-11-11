"""One benchmark trial end to end, and the task x harness x model x trial matrix over a device pool.

Trial lifecycle (runs/<run_id>/<task>/<harness>__<model>/trial-<n>/):
  1. setup     task setup steps (seed backend, clock, location, launch), then validity gates
  2. agent     harness.run() under the task's timeout and step limit
  3. capture   final screen + UI hierarchy, logcat, the app database after the run, state diff
  4. grade     success + prohibited checks (and/or a python grader) against the backend state
  5. cleanup   task cleanup steps, clock restore; result.json is written last
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from benchlib import ROOT, devicestate
from benchlib.backend import SeedError, SqliteBackend, diff_summary, ensure_forward, http_request, state_diff
from benchlib.config import App, HarnessSpec, ModelSpec, Task, load_app, render
from benchlib.device import Device, DeviceError
from benchlib.grading import GradeContext, compare, load_callable, normalize_grader_output, run_checks
from benchlib.harnesses import create as create_harness
from benchlib.harnesses.base import HarnessResult, RunContext, RunLog

RESULT_SCHEMA = "drizz-bench-result/1"
_GLOBAL_LOCK = threading.Lock()          # for harnesses with parallel_safe: false
_INSTALL_LOCK = threading.Lock()


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")


def sha256_file(p: Path | None) -> str | None:
    if not p or not p.exists():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Job:
    task: Task
    harness: HarnessSpec
    model: ModelSpec
    trial: int

    @property
    def cell(self) -> str:
        return f"{self.task.id} | {self.harness.id} | {self.model.alias} | #{self.trial}"


# ---- setup steps -----------------------------------------------------------------------------------

class SetupState:
    def __init__(self):
        self.seed_meta: dict | None = None
        self.seeds: dict[str, dict] = {}         # app id -> seed metadata (multi-app tasks)
        self.clock: dict | None = None
        self.launched = False
        self.launched_app: str | None = None
        self.validity: list[dict] = []
        self.orig_timezone: str | None = None


def _step_items(steps: list) -> list[tuple[str, object]]:
    out = []
    for s in steps:
        if isinstance(s, str):
            out.append((s, None))
        elif isinstance(s, dict) and len(s) == 1:
            out.append(next(iter(s.items())))
        else:
            raise ValueError(f"bad setup step {s!r}: use `name` or `{{name: arg}}`")
    return out


def _local(path: str, vars_: dict) -> Path:
    """Host paths in task steps are relative to the task file."""
    p = Path(str(path)).expanduser()
    return p if p.is_absolute() or not vars_.get("task_dir") else (Path(vars_["task_dir"]) / p).resolve()


def _targets(arg, app: App, apps: dict[str, App]) -> list[App]:
    """`stop_app` / `clear_app_data` argument: none -> primary app, "all", an app id or a list of ids."""
    if arg is None or isinstance(arg, (int, float)):
        return [app]
    if arg == "all":
        return list(apps.values())
    return [apps[a] for a in (arg if isinstance(arg, list) else [arg])]


def run_steps(steps: list, app: App, dev: Device, out: Path, vars_: dict, st: SetupState, log: RunLog,
              apps: dict[str, App] | None = None) -> None:
    apps = apps or {app.id: app}
    for name, arg in _step_items(render(steps, vars_)):
        log(f"setup: {name} {arg if arg is not None else ''}".rstrip())
        if name == "stop_app":
            for a in _targets(arg, app, apps):
                dev.force_stop(a.package)
        elif name == "clear_app_data":
            for a in _targets(arg, app, apps):
                dev.clear_data(a.package)
        elif name == "home":
            dev.home()
        elif name == "sqlite_seed":
            spec = arg if isinstance(arg, dict) else {"seed": arg}
            target = apps[spec.get("app", app.id)]
            seed = str(spec["seed"])
            if any(seed.endswith(x) for x in (".sql", ".db")) and not Path(seed).is_absolute():
                seed = str(_local(seed, vars_)) if _local(seed, vars_).exists() else seed
            overlays = [_local(o, vars_) for o in (spec.get("overlays") or [])]
            work = out / "backend" / target.id if len(apps) > 1 else out / "backend"
            work.mkdir(parents=True, exist_ok=True)
            meta = SqliteBackend(target, dev).seed(seed, work, overlays)
            st.seeds[target.id] = meta
            if target.id == app.id:
                st.seed_meta = meta
                vars_["seed"] = meta
        elif name == "device_reset":
            spec = arg or {}
            if spec.get("sms", True):
                devicestate.reset_sms(dev)
            if spec.get("contacts", True):
                devicestate.reset_contacts(dev)
            if spec.get("alarms", True):
                devicestate.reset_alarms(dev)
            if spec.get("media"):
                devicestate.reset_media(dev, spec["media"])
        elif name == "contacts":
            for c in arg:
                devicestate.add_contact(dev, c["name"], c["phone"])
        elif name == "sms_in":
            for m in arg if isinstance(arg, list) else [arg]:
                devicestate.sms_in(dev, m["from"], m["body"])
                time.sleep(float(m.get("gap_s", 2)))
        elif name == "alarm":
            for a in arg if isinstance(arg, list) else [arg]:
                devicestate.add_alarm(dev, int(a["hour"]), int(a["minute"]), a.get("label", ""))
        elif name == "push_media":
            for m in arg if isinstance(arg, list) else [arg]:
                devicestate.push_media(dev, _local(m["src"], vars_), m["dest"], m.get("mtime"))
        elif name == "push_file":
            dev.adb("push", str(_local(arg["src"], vars_)), arg["dest"], timeout=300)
        elif name == "adb":
            for cmd in arg if isinstance(arg, list) else [arg]:
                dev.adb(*shlex.split(cmd), check=True, timeout=120)
        elif name == "shell":
            cmds = arg if isinstance(arg, list) else [arg]
            for cmd in cmds:
                p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300,
                                   env={**os.environ, "SERIAL": dev.serial, "PACKAGE": app.package})
                if p.returncode != 0:
                    raise RuntimeError(f"setup shell failed ({p.returncode}): {cmd}\n{p.stderr.strip()[-500:]}")
        elif name == "http":
            url = arg["url"]
            headers = dict(arg.get("headers") or {})
            if not url.startswith(("http://", "https://")) and app.http:
                ensure_forward(dev, app.http.adb_forward)
                url, headers = app.http.base_url + "/" + url.lstrip("/"), {**app.http.headers, **headers}
            resp = http_request(arg.get("method", "POST"), url, headers, arg.get("body"))
            want = arg.get("status", [200, 201, 202, 204])
            if resp.get("status") not in (want if isinstance(want, list) else [want]):
                raise RuntimeError(f"setup http {url}: status {resp.get('status')} {resp.get('error') or ''}")
            if arg.get("save_as"):
                vars_[arg["save_as"]] = resp.get("json")
        elif name == "set_time":
            spec = arg if isinstance(arg, dict) else {"time": arg}
            zone = spec.get("timezone") or vars_.get("timezone") or "UTC"
            st.orig_timezone = st.orig_timezone or dev.shell("getprop persist.sys.timezone") or None
            target = dev.set_clock(zone, spec["time"], spec.get("date"))
            st.clock = {"zone": zone, "target": target}
            vars_.update({"benchmark_date": target.date().isoformat(), "benchmark_time": spec["time"],
                          "timezone": zone})
        elif name == "set_location":
            dev.set_location(float(arg["lat"]), float(arg["lng"]))
        elif name == "launch_app":
            # arg: none | seconds to wait | app id | {app, wait}
            spec = arg if isinstance(arg, dict) else ({"wait": arg} if isinstance(arg, (int, float)) or arg is None
                                                       else {"app": arg})
            target = apps[spec.get("app", app.id)]
            dev.launch(target.package, target.launch_activity)
            st.launched, st.launched_app = True, target.id
            time.sleep(float(spec["wait"]) if spec.get("wait") is not None else target.launch_wait_s)
        elif name == "deeplink":
            dev.shell(f"am start -a android.intent.action.VIEW -d {shlex.quote(str(arg))}", check=True)
            st.launched = True
        elif name == "grant":
            for perm in arg if isinstance(arg, list) else [arg]:
                dev.shell(f"pm grant {app.package} {perm}")
        elif name == "broadcast":
            dev.shell(f"am broadcast {arg}", check=True)
        elif name == "wait":
            time.sleep(float(arg))
        else:
            raise ValueError(f"unknown setup step '{name}'")


class EventRunner(threading.Thread):
    """Fires task `events` while the agent works. Each event fires at most once.

        events:
          - id: mom_delayed
            when:                      # any listed condition fires the event
              after_s: 480             # seconds since the agent started
              sms_sent_to: "+14155550162"   # an outgoing SMS to this number exists (since agent start)
              foreground: dev.drizz.airbnb  # this package is in the foreground
              sql: {app: airbnb, query: "SELECT COUNT(*) FROM bookings WHERE ...", expect: {gte: 1}}
            delay_s: 5                 # wait after the condition holds
            do:                        # setup-step syntax (sms_in, adb, alarm, ...)
              - sms_in: {from: "+14155550161", body: "Delayed! Now landing 5:35 PM."}
    """

    POLL_S = 3.0
    SQL_POLL_S = 12.0

    def __init__(self, events: list, app: App, apps: dict[str, App], dev: Device, out: Path, vars_: dict, log: RunLog):
        super().__init__(daemon=True)
        self.events, self.app, self.apps, self.dev, self.out, self.vars, self.log = events, app, apps, dev, out, vars_, log
        self.stop_flag = threading.Event()
        self.fired: list[dict] = []
        self.t0 = time.time()
        self.t0_device_ms = 0
        self.snapshots = tempfile.TemporaryDirectory()

    def stop(self) -> list[dict]:
        self.stop_flag.set()
        self.join(timeout=30)
        self.snapshots.cleanup()
        return self.fired

    def _device_ms(self) -> int | None:
        """The device's clock, so a grader can compare a trigger with what the app recorded. None if unreadable."""
        try:
            return int(self.dev.shell("date +%s")) * 1000
        except Exception:  # noqa: BLE001
            return None

    def _sent_to(self, number: str) -> bool:
        want = devicestate.digits(number)
        return any(m["type"] == "sent" and m["digits"] == want and m["date"] >= self.t0_device_ms - 5000
                   for m in devicestate.read_sms(self.dev))

    def _sql(self, spec: dict, cache: dict) -> bool:
        """One pull per app per SQL_POLL_S; every trigger then runs its own query on that copy. The answer is never
        shared: reusing one query's answer fired every SQL trigger on an app as soon as the first came true (3 Nov,
        dyn5 D02 and D05). A DB or query that can't be read is not a condition met."""
        target = self.apps[spec.get("app", self.app.id)]
        now = time.time()
        if now - cache.get(target.id, (0, None))[0] >= self.SQL_POLL_S:
            try:
                db = SqliteBackend(target, self.dev).pull(Path(self.snapshots.name) / target.id)
            except Exception as e:  # noqa: BLE001
                self.log(f"event sql poll failed: {e}")
                db = None
            cache[target.id] = (now, db)
        db = cache[target.id][1]
        if db is None:
            return False
        con = sqlite3.connect(db)
        try:
            row = con.execute(spec["query"]).fetchone()
        except sqlite3.Error as e:
            self.log(f"event sql query failed: {e}")
            return False
        finally:
            con.close()
        return compare(row[0] if row else None, spec.get("expect", {"gte": 1}))[0]

    def run(self) -> None:
        try:
            self.t0_device_ms = int(self.dev.shell("date +%s") or 0) * 1000
        except Exception:  # noqa: BLE001
            self.t0_device_ms = int(time.time() * 1000)
        pending = list(self.events)
        sql_cache: dict = {}
        while pending and not self.stop_flag.wait(self.POLL_S):
            for ev in list(pending):
                when = ev.get("when") or {}
                reason = None
                try:
                    if "after_s" in when and time.time() - self.t0 >= float(when["after_s"]):
                        reason = f"after_s {when['after_s']}"
                    elif "foreground" in when and self.dev.foreground_package() == when["foreground"]:
                        reason = f"foreground {when['foreground']}"
                    elif "sms_sent_to" in when and self._sent_to(str(when["sms_sent_to"])):
                        reason = f"sms_sent_to {when['sms_sent_to']}"
                    elif "sql" in when and self._sql(when["sql"], sql_cache):
                        reason = "sql"
                except Exception as e:  # noqa: BLE001
                    self.log(f"event {ev.get('id')}: condition error {e}")
                if not reason:
                    continue
                pending.remove(ev)
                if ev.get("delay_s"):
                    time.sleep(float(ev["delay_s"]))
                at = round(time.time() - self.t0, 1)
                at_device_ms = self._device_ms()
                self.log(f"event {ev.get('id')} fired at +{at}s ({reason})")
                err = None
                try:
                    run_steps(ev.get("do") or [], self.app, self.dev, self.out, self.vars, SetupState(), self.log, self.apps)
                except Exception as e:  # noqa: BLE001
                    err = f"{type(e).__name__}: {e}"
                    self.log(f"event {ev.get('id')} action failed: {err}")
                self.fired.append({"id": ev.get("id"), "at_s": at, "at_device_ms": at_device_ms, "reason": reason, "error": err})


def _gate(st: SetupState, gid: str, passed: bool, detail: str) -> None:
    st.validity.append({"id": gid, "kind": "validity", "type": "system", "passed": bool(passed), "detail": detail})


# ---- one trial ---------------------------------------------------------------------------------------

def run_trial(job: Job, dev: Device, run_dir: Path, verbose: bool = False, keep_dbs: bool = True) -> dict:
    task, hspec, model = job.task, job.harness, job.model
    app = load_app(task.app)
    apps = {a: (app if a == app.id else load_app(a)) for a in (task.apps or [task.app])}
    multi = len(apps) > 1
    out = run_dir / slug(task.id) / f"{slug(hspec.id)}__{slug(model.alias)}" / f"trial-{job.trial}"
    if out.exists():
        shutil.rmtree(out)
    (out / "backend").mkdir(parents=True)
    (out / "screens").mkdir()
    log = RunLog(out / "harness.log", f"{dev.serial} {job.cell}", verbose)
    t0 = time.time()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result: dict = {
        "schema": RESULT_SCHEMA, "run_id": run_dir.name, "trial": job.trial, "cell": job.cell,
        "task": {"id": task.id, "title": task.title, "app": task.app, "tags": task.tags, "difficulty": task.difficulty,
                 "file": str(task.path.relative_to(ROOT) if task.path.is_relative_to(ROOT) else task.path),
                 "sha256": sha256_file(task.path)},
        "harness": {"id": hspec.id, "type": hspec.type, "params": hspec.params},
        "model": {"id": model.id, "alias": model.alias, "params": model.params},
        "limits": {"timeout_s": task.timeout_s, "max_steps": task.max_steps},
        "timing": {"started_at": started_at},
        "valid": False, "outcome": None, "success": None, "verdict": None, "error": None,
    }
    st = SetupState()
    vars_ = {"today": datetime.now().date().isoformat(), "package": app.package, "serial": dev.serial,
             "app_name": app.name, "task_dir": str(task.path.parent), **task.vars}
    harness_result: HarnessResult | None = None
    gctx = GradeContext(app=app, device=dev, out_dir=out, vars=vars_, apps=apps)
    backend = SqliteBackend(app, dev) if app.sqlite else None
    backends = {a.id: SqliteBackend(a, dev) for a in apps.values() if a.sqlite}
    events: EventRunner | None = None

    try:
        # ---- 1. setup -----------------------------------------------------------------------------
        ok, detail = dev.healthy()
        _gate(st, "emulator_healthy", ok, detail)
        missing = [a.package for a in apps.values() if not dev.installed(a.package)]
        installed = not missing
        _gate(st, "app_installed", installed, ", ".join(a.package for a in apps.values()) + (
            f" (missing: {', '.join(missing)})" if missing else ""))
        result["device"] = dev.info()
        result["app"] = {"id": app.id, "name": app.name, "package": app.package, **dev.app_version(app.package),
                         "apk_sha256": sha256_file(app.apk)}
        if multi:
            result["apps"] = {a.id: {"package": a.package, **dev.app_version(a.package), "apk_sha256": sha256_file(a.apk)}
                              for a in apps.values()}
        if not (ok and installed):
            raise _Invalid("device or app not ready")

        try:
            run_steps(task.setup, app, dev, out, vars_, st, log, apps)
            run_steps(hspec.before_run, app, dev, out, vars_, st, log, apps)
        except (SeedError, DeviceError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            _gate(st, "setup_steps", False, f"{type(e).__name__}: {e}")
            raise _Invalid(str(e)) from e
        _gate(st, "setup_steps", True, f"{len(task.setup)} task + {len(hspec.before_run)} harness steps")
        for aid, meta in (st.seeds.items() if multi else ([(app.id, st.seed_meta)] if st.seed_meta else [])):
            _gate(st, "seed_verified" if not multi else f"seed_verified_{aid}", meta.get("verified_on_device", False),
                  f"seed {meta['seed']} digest {str(meta.get('device_digest') or '')[:16]}")
        if st.clock:
            now = dev.now(st.clock["zone"])
            drift = now - st.clock["target"]
            _gate(st, "benchmark_clock", timedelta(0) <= drift <= timedelta(minutes=3),
                  f"device time {now:%Y-%m-%d %H:%M:%S %Z} (target {st.clock['target']:%H:%M})")
        if st.launched:
            want_pkg = apps[st.launched_app or app.id].package
            fg = dev.foreground_package()
            detail = f"foreground: {fg}"
            if fg != want_pkg:
                crash = re.findall(r"E AndroidRuntime: (?:FATAL EXCEPTION.*|.*(?:Exception|Error):.*)", dev.logcat_dump())
                detail += f"; app crash: {crash[-1][:300]}" if crash else ""
            _gate(st, "app_in_foreground", fg == want_pkg, detail)

        for aid, be in backends.items():
            try:
                before = be.pull(out / "backend" / (f"{aid}/before" if multi else "before"))
            except (SeedError, DeviceError) as e:
                _gate(st, "backend_readable", False, f"{aid}: {e}")
                raise _Invalid(str(e)) from e
            gctx.dbs[aid] = {"before": before, "after": before}   # validity checks read the starting state
        if app.id in gctx.dbs:
            gctx.before_db = gctx.after_db = gctx.dbs[app.id]["before"]
        if app.http:
            ensure_forward(dev, app.http.adb_forward)
        st.validity += run_checks(task.validity, gctx, "validity")
        result["validity"] = st.validity
        result["valid"] = all(c["passed"] for c in st.validity)
        if not result["valid"]:
            raise _Invalid("validity checks failed: " + ", ".join(c["id"] for c in st.validity if not c["passed"]))

        dev.screenshot(out / "screens" / "start.png")
        dev.logcat_clear()
        instruction = render(task.prompt, vars_)
        result["instruction"] = instruction
        result["timing"]["setup_s"] = round(time.time() - t0, 1)

        # ---- 2. agent -----------------------------------------------------------------------------
        harness = create_harness(hspec)
        harness.check_model(model)
        t_agent = time.time()
        ctx = RunContext(task=task, app=app, harness=hspec, model=model, device=dev, instruction=instruction,
                         out_dir=out, deadline=t_agent + task.timeout_s, max_steps=task.max_steps, vars=vars_, log=log)
        lock = _GLOBAL_LOCK if not hspec.parallel_safe else None
        if lock:
            lock.acquire()
        try:
            log(f"agent start: {hspec.id} on {model.id} (timeout {task.timeout_s}s, max {task.max_steps} steps)")
            if task.events:
                events = EventRunner(task.events, app, apps, dev, out, vars_, log)
                events.start()
            harness_result = harness.run(ctx)
        except Exception as e:  # noqa: BLE001 - the harness crashed; still capture and grade the device
            harness_result = HarnessResult("harness_error", error=f"{type(e).__name__}: {e}")
            log(traceback.format_exc())
        finally:
            if events:
                result["events"] = events.stop()
            if lock:
                lock.release()
        result["timing"]["agent_s"] = round(time.time() - t_agent, 1)
        log(f"agent end: {harness_result.outcome} after {harness_result.steps} steps")

        # ---- 3. capture ---------------------------------------------------------------------------
        t_grade = time.time()
        dev.screenshot(out / "screens" / "final.png")
        try:
            gctx.final_ui_xml = dev.ui_hierarchy()
            (out / "final_ui.xml").write_text(gctx.final_ui_xml or "")
        except Exception as e:  # noqa: BLE001
            log(f"final UI hierarchy unavailable: {e}")
        (out / "logcat.txt").write_text(dev.logcat_dump())
        for aid, be in backends.items():
            base = out / "backend" / aid if multi else out / "backend"
            after = be.pull(base / "after")
            gctx.dbs[aid]["after"] = after
            diff = state_diff(gctx.dbs[aid]["before"], after, apps[aid].sqlite.diff_exclude)
            (base / "state_diff.json").write_text(json.dumps(diff, indent=2, default=str))
        if app.id in gctx.dbs:
            gctx.before_db, gctx.after_db = gctx.dbs[app.id]["before"], gctx.dbs[app.id]["after"]
        if task.capture_device is not None:
            gctx.device_state = devicestate.capture(dev, task.capture_device or {})
            gctx.device_state["agent_started_ms"] = (events.t0_device_ms if events else None) or vars_.get("agent_started_ms")
            (out / "device_state.json").write_text(json.dumps(gctx.device_state, indent=2, default=str))
        gctx.agent_answer = harness_result.agent_answer
        gctx.agent_status = harness_result.agent_status
        gctx.extra = {"harness_result": asdict(harness_result), "seed": st.seed_meta, "seeds": st.seeds,
                      "events": result.get("events")}

        # ---- 4. grade -----------------------------------------------------------------------------
        checks = run_checks(task.success, gctx, "success") + run_checks(task.prohibited, gctx, "prohibited")
        if task.grader:
            try:
                checks += [{"kind": "success", "type": "python", **c}
                           for c in normalize_grader_output(load_callable(task.grader)(gctx))]
            except Exception as e:  # noqa: BLE001
                checks.append({"id": "grader", "kind": "success", "type": "python", "passed": False,
                               "error": f"{type(e).__name__}: {e}"})
        graded = [c for c in checks if c.get("kind") in ("success", "prohibited")]
        result["checks"] = checks
        result["success"] = bool(graded) and all(c.get("passed") for c in graded)
        result["failed_checks"] = [c["id"] for c in graded if not c.get("passed")]
        result["timing"]["grade_s"] = round(time.time() - t_grade, 1)
        result["outcome"] = harness_result.outcome
        if not graded:
            result["error"] = "task defines no success checks"

    except _Invalid as e:
        result["validity"] = st.validity
        result["valid"] = False
        result["outcome"] = "invalid_setup"
        result["error"] = str(e)
        try:
            dev.screenshot(out / "screens" / "invalid.png")
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001 - never lose a cell; record the runner error
        result["outcome"] = "runner_error"
        result["error"] = f"{type(e).__name__}: {e}"
        log(traceback.format_exc())
    finally:
        # ---- 5. cleanup ---------------------------------------------------------------------------
        try:
            run_steps(task.cleanup, app, dev, out, vars_, SetupState(), log, apps)
        except Exception as e:  # noqa: BLE001
            log(f"cleanup failed: {e}")
        if st.clock:
            dev.restore_clock(st.orig_timezone)

    hr = harness_result
    if hr:
        result["agent"] = {"status": hr.agent_status, "answer": hr.agent_answer, "summary": hr.agent_summary,
                           "claimed_success": hr.agent_status == "success" if hr.agent_status else None}
        if result["success"] is not None and hr.agent_status:
            result["agent"]["claim_matches_grade"] = (hr.agent_status == "success") == result["success"]
        result["steps"] = hr.steps
        result["usage"] = asdict(hr.usage)
        result["cost_usd"] = _cost(model, hr)
        result["harness_extra"] = hr.extra
        if hr.error and not result["error"]:
            result["error"] = hr.error
    result["backend"] = {"seed": st.seed_meta}
    if multi:
        result["backend"]["seeds"] = st.seeds
        result["backend"]["state_diff_summary"] = {
            aid: diff_summary(json.loads((out / "backend" / aid / "state_diff.json").read_text()))
            for aid in apps if (out / "backend" / aid / "state_diff.json").exists()}
    elif (out / "backend" / "state_diff.json").exists():
        result["backend"]["state_diff_summary"] = diff_summary(json.loads((out / "backend" / "state_diff.json").read_text()))
    if not keep_dbs:
        for base in ([out / "backend" / a for a in apps] if multi else [out / "backend"]):
            for d in ("before", "after"):
                shutil.rmtree(base / d, ignore_errors=True)
            (base / "seeded.db").unlink(missing_ok=True)

    result["verdict"] = verdict(result)
    result["timing"]["total_s"] = round(time.time() - t0, 1)
    result["timing"]["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result["artifacts"] = {"dir": str(out.relative_to(run_dir)),
                           "screens": sorted(p.name for p in (out / "screens").iterdir()) if (out / "screens").exists() else []}
    (out / "result.json").write_text(json.dumps(result, indent=2, default=str))
    return result


class _Invalid(Exception):
    pass


def verdict(r: dict) -> str:
    if not r.get("valid"):
        return "INVALID"
    if r.get("outcome") in ("runner_error",):
        return "ERROR"
    if r.get("success"):
        return "PASS"
    if r.get("outcome") in ("harness_error", "refused"):
        return "ERROR"
    return "FAIL"


def _cost(model: ModelSpec, hr: HarnessResult) -> float | None:
    u = hr.usage
    if model.price_in is not None and model.price_out is not None and (u.input_tokens or u.output_tokens):
        cached_price = model.price_cached if model.price_cached is not None else model.price_in
        return round(((u.input_tokens - u.cached_input_tokens) * model.price_in + u.cached_input_tokens * cached_price
                      + u.output_tokens * model.price_out) / 1e6, 4)
    return hr.extra.get("reported_cost_usd")


# ---- matrix ----------------------------------------------------------------------------------------

def ensure_app(dev: Device, app: App, reinstall: bool, log=print) -> None:
    with _INSTALL_LOCK:
        if reinstall or not dev.installed(app.package):
            if not app.apk or not app.apk.exists():
                raise DeviceError(f"{app.package} is not installed on {dev.serial} and no apk is configured")
            log(f"[{dev.serial}] installing {app.apk.name}")
            dev.install(app.apk)


def run_matrix(jobs: list[Job], devices: list[Device], run_dir: Path, verbose: bool, reinstall: bool,
               keep_dbs: bool, on_result=None) -> list[dict]:
    run_dir.mkdir(parents=True, exist_ok=True)
    apps = {a for j in jobs for a in (j.task.apps or [j.task.app])}
    for dev in devices:
        for a in sorted(apps):
            ensure_app(dev, load_app(a), reinstall)
    q: queue.Queue[Job] = queue.Queue()
    for j in jobs:
        q.put(j)
    results: list[dict] = []
    lock = threading.Lock()

    def worker(dev: Device):
        while True:
            try:
                job = q.get_nowait()
            except queue.Empty:
                return
            r = run_trial(job, dev, run_dir, verbose, keep_dbs)
            with lock:
                results.append(r)
                if on_result:
                    on_result(r, len(results), len(jobs))

    threads = [threading.Thread(target=worker, args=(d,), daemon=True) for d in devices]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results
