"""The phone side of a Harbor trial: prepares the emulator for one task, serves the agent's phone tools over MCP, and
hands the final state to the verifier.

It runs in the `android` service next to the emulator, never in the agent's container. Every step that touches the
device reuses the bench's own code (benchlib run_steps, EventRunner, SqliteBackend.pull, devicestate.capture), so a
task is set up, triggered and captured the same way as in the runs it was mined from.

    python3 phone_service.py --task docs/dyn5_hits/D02/tasks/d02c.yaml --serial emulator-5554 --state /state

Lifecycle:
  1. prepare   health and install gates, the task's setup steps, clock gate, `before` DBs, start screenshot
  2. serve     MCP on :8000/mcp for the agent; mid-run triggers start with the agent's first tool call
  3. capture   POST /capture (bearer token; Harbor's [[verifier.collect]] hook calls it from inside this container
               once the agent is done): stops triggers and tools, pulls `after` DBs, SMS and contacts, and writes
               everything the grader reads to /state/capture, which Harbor hands to the separate verifier

A setup failure leaves no ready file and writes /state/setup_error.json: the trial never starts, so a harness fault
can't become a model failure.
"""
from __future__ import annotations

import argparse
import hashlib
import base64
import json
import os
import re
import secrets
import shutil
import shlex
import sys
import threading
import time
import traceback
from datetime import timedelta
from pathlib import Path

BENCH = Path(os.environ.get("BENCH_ROOT", "/opt/drizz/bench"))
sys.path.insert(0, str(BENCH))

from benchlib import devicestate  # noqa: E402
from benchlib.backend import SqliteBackend  # noqa: E402
from benchlib.config import load_app, load_task, render  # noqa: E402
import benchlib.device as _bdev  # noqa: E402
from benchlib.device import Device  # noqa: E402

# The phone image ships adb only. benchlib sends a few calls through Drizz's own CLI; point that at a name that cannot
# exist, so any call this service did not reroute fails loudly instead of depending on a tool the image lacks.
_bdev.DRIZZ = "/nonexistent/drizz-cli-not-in-image"


class AdbDevice(Device):
    """benchlib's Device with its CLI-backed calls done through adb (same key events and input commands)."""

    def home(self) -> None:
        self.shell("input keyevent KEYCODE_HOME", check=True)

    def back(self) -> None:
        self.shell("input keyevent KEYCODE_BACK", check=True)

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {x} {y}", check=True)

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.shell(f"input swipe {x1} {y1} {x2} {y2}", check=True)

    def ui_hierarchy(self) -> str:
        self.shell("uiautomator dump /sdcard/__bench_ui.xml", timeout=60)
        return self.adb("exec-out", "cat", "/sdcard/__bench_ui.xml", check=False)
from benchlib.harnesses.base import RunLog  # noqa: E402
from benchlib.runner import EventRunner, SetupState, run_steps  # noqa: E402
from mcp.server.fastmcp import FastMCP, Image  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402

# Steps that change the device count against the task's step budget; looking at the screen does not.
ACTIONS = {"tap", "long_press", "swipe", "type_text", "press_key", "launch_app"}
KEYS = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER", "delete": "KEYCODE_DEL",
        "recent_apps": "KEYCODE_APP_SWITCH", "tab": "KEYCODE_TAB", "search": "KEYCODE_SEARCH"}
STOCK = {"messages": "com.google.android.apps.messaging", "contacts": "com.google.android.contacts",
         "files": "com.google.android.documentsui", "chrome": "com.android.chrome"}


class Phone:
    def __init__(self, task_file: Path, serial: str, state: Path, max_steps: int | None):
        self.task = load_task(task_file)
        self.app = load_app(self.task.app)
        self.apps = {a: (self.app if a == self.app.id else load_app(a)) for a in self.task.apps}
        self.dev = AdbDevice(serial)
        self.state = state
        self.max_steps = max_steps or self.task.max_steps
        self.vars: dict = {}
        self.events: EventRunner | None = None
        self.steps = 0
        self.closed = False
        self.lock = threading.Lock()
        self.log = RunLog(state / "phone.log", serial, False)
        self.packages = {a.id: a.package for a in self.apps.values()}
        for name in self.task.raw.get("stock_apps") or []:
            if name in STOCK:
                self.packages[name] = STOCK[name]

    # ---- 1. prepare ---------------------------------------------------------------------------------
    def prepare(self) -> None:
        gates = []

        def gate(gid: str, ok: bool, detail: str) -> None:
            gates.append({"id": gid, "passed": bool(ok), "detail": detail})
            if not ok:
                raise RuntimeError(f"{gid}: {detail}")

        st = SetupState()
        self.vars = {"package": self.app.package, "serial": self.dev.serial, "app_name": self.app.name,
                     "task_dir": str(self.task.path.parent), **self.task.vars}
        try:
            self.web_check(gate)
            gate("emulator_healthy", *self.dev.healthy())
            missing = [a.package for a in self.apps.values() if not self.dev.installed(a.package)]
            gate("app_installed", not missing, f"missing: {missing}" if missing else "all installed")
            run_steps(self.task.setup, self.app, self.dev, self.state, self.vars, st, self.log, self.apps)
            gate("setup_steps", True, f"{len(self.task.setup)} steps")
            if st.clock:
                drift = self.dev.now(st.clock["zone"]) - st.clock["target"]
                gate("benchmark_clock", timedelta(0) <= drift <= timedelta(minutes=3), f"drift {drift}")
            for aid, app in self.apps.items():
                if app.sqlite:
                    SqliteBackend(app, self.dev).pull(self.state / "backend" / aid / "before")
            self.dev.screenshot(self.state / "screens" / "start.png")
        except Exception as e:  # noqa: BLE001
            (self.state / "setup_error.json").write_text(json.dumps(
                {"error": f"{type(e).__name__}: {e}", "gates": gates, "trace": traceback.format_exc()}, indent=2))
            raise
        instruction = render(self.task.prompt, self.vars)
        (self.state / "meta.json").write_text(json.dumps(
            {"task_id": self.task.id, "instruction": instruction, "vars": self.vars, "gates": gates,
             "clock": {"zone": st.clock["zone"], "target": st.clock["target"].isoformat()} if st.clock else None,
             "max_steps": self.max_steps, "apps": self.packages}, indent=2, default=str))
        (self.state / "ready").write_text(str(int(time.time())))

    def web_check(self, gate) -> None:
        """N4 tasks are answered from live web pages. Re-read this task's sources first (the bench's web_check.py,
        limited to this task): if a figure has moved, the answer key is stale and the trial must not run."""
        tid = self.task.vars.get("n4_id")
        script = BENCH / "docs" / "n4" / "web_check.py"
        if not tid or not script.exists():
            return
        import importlib.util
        spec = importlib.util.spec_from_file_location("n4_web_check", script)
        wc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wc)
        rows = []
        for w in wc.ANSWERS[tid].get("web", []):
            try:
                body = wc.text(w["fetch"])
                miss = [m for m in w["must"] if m not in body]
                rows.append({"fact": w["fact"], "url": w["fetch"], "ok": not miss, "missing": miss,
                             "sha256": hashlib.sha256(body.encode()).hexdigest()})
            except Exception as e:  # noqa: BLE001 - unreachable is unverified, which also stops the trial
                rows.append({"fact": w["fact"], "url": w["fetch"], "ok": False, "missing": [f"unreachable: {e}"]})
            time.sleep(1)
        (self.state / "web_check.json").write_text(json.dumps({"at": time.time(), "sources": rows}, indent=1))
        bad = [r["fact"] for r in rows if not r["ok"]]
        gate("live_web_sources", not bad, f"{len(rows) - len(bad)}/{len(rows)} still say what the answer key says"
             + (f"; changed or unreachable: {bad}" if bad else ""))

    # ---- 2. tools -----------------------------------------------------------------------------------
    def act(self, name: str, args: dict, fn):
        """Every tool call goes through here: budget, freeze after capture, trigger start, and a log line written
        before the call returns, so a trial that dies mid-run still has its actions on disk."""
        with self.lock:
            if self.closed:
                raise RuntimeError("the trial has ended; the phone no longer accepts actions")
            if self.events is None and self.task.events:
                self.events = EventRunner(self.task.events, self.app, self.apps, self.dev, self.state, self.vars,
                                          self.log)
                self.events.start()
            elif self.events is None:
                self.events = False  # no triggers; remember that the agent has started
                self.agent_started_ms = int(self.dev.shell("date +%s") or 0) * 1000
            if name in ACTIONS:
                if self.steps >= self.max_steps:
                    raise RuntimeError(f"step budget used up ({self.max_steps} actions)")
                self.steps += 1
            t = time.time()
            err = None
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                raise
            finally:
                with (self.state / "actions.jsonl").open("a") as f:
                    f.write(json.dumps({"t": round(t, 2), "tool": name, "args": args, "step": self.steps,
                                        "error": err}) + "\n")

    def screenshot_png(self) -> bytes:
        return self.dev.screenshot_png()

    def ui_tree(self) -> str:
        self.dev.shell("uiautomator dump /sdcard/__h_ui.xml", timeout=60)
        return self.dev.adb("exec-out", "cat", "/sdcard/__h_ui.xml", check=False)

    def type_text(self, text: str) -> None:
        # `input text` takes one line of ASCII; a newline becomes Shift+Enter, which chat apps read as a line break.
        for i, line in enumerate(text.split("\n")):
            if i:
                self.dev.shell("input keycombination SHIFT_LEFT ENTER", check=True)
            for chunk in re.findall(r".{1,60}", line, re.S):
                esc = re.sub(r"([\\\"'`$&|;<>()*~!#?\[\]{}^])", r"\\\1", chunk).replace(" ", "%s")
                self.dev.shell(f"input text {esc}", check=True)

    # ---- oracle (Harbor's oracle agent only) ----------------------------------------------------------
    def oracle_before(self) -> dict:
        """The seeded `before` databases, so the oracle can build the gold end state on this phone's own seed."""
        out = {}
        for aid, app in self.apps.items():
            if app.sqlite:
                p = self.state / "backend" / aid / "before" / app.sqlite.db.rsplit("/", 1)[-1]
                out[aid] = base64.b64encode(p.read_bytes()).decode()
        return out

    def oracle_apply(self, dbs: dict, sms: list, fire: list | None = None) -> dict:
        """Write a gold end state onto the device: the app databases and the texts. Triggers are switched off first,
        so they can't rewrite the state; the texts a trigger would have delivered come in the gold list as inbox
        messages, or are delivered for real by firing the named triggers' own actions (`fire`), which are then logged as
        fired like any trigger. Recorded in capture.json, so an oracle trial is never mistaken for an agent's."""
        with self.lock:
            if self.closed:
                raise RuntimeError("the trial has ended")
            if self.events:
                self.events.stop()
            self.events = False
            self.agent_started_ms = int(self.dev.shell("date +%s") or 0) * 1000
            self.oracle = True
        fired = []
        for ev in [e for e in self.task.events if e.get("id") in set(fire or [])]:
            at = self.events_t0 = getattr(self, "events_t0", time.time())
            err = None
            try:
                run_steps(ev.get("do") or [], self.app, self.dev, self.state, self.vars, SetupState(), self.log, self.apps)
                self._wait_for_texts(ev)
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
            fired.append({"id": ev["id"], "at_s": round(time.time() - at, 1),
                          "at_device_ms": int(self.dev.shell("date +%s") or 0) * 1000, "reason": "oracle", "error": err})
        self.oracle_fired = fired
        work = self.state / "oracle"
        for aid, data in dbs.items():
            app = self.apps[aid]
            p = work / aid / app.sqlite.db.rsplit("/", 1)[-1]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(data))
            SqliteBackend(app, self.dev).push(p)
        self.dev.shell("appops set com.android.shell WRITE_SMS allow")
        now = int(self.dev.shell("date +%s") or 0) * 1000
        for i, m in enumerate(sms):
            d = re.sub(r"\D", "", m.get("digits") or m.get("address") or "")[-10:]
            kind = {"sent": 2, "inbox": 1}[m["type"]]
            body = m["body"].replace("\\", "\\\\").replace(":", "\\:")     # `content` splits bindings on unescaped ':'
            binds = [f"address:s:+1{d}", f"body:s:{body}", f"type:i:{kind}", f"date:l:{now + 1000 * (i + 1)}",
                     "read:i:1"]
            cmd = "content insert --uri content://sms " + " ".join(f"--bind {shlex.quote(b)}" for b in binds)
            for attempt in range(3):
                res = self.dev.shell(cmd, check=True)
                if not res.strip() and any(x["body"] == m["body"] for x in devicestate.read_sms(self.dev)):
                    break
                time.sleep(2)
            else:
                raise RuntimeError(f"SMS insert failed for text {i + 1}: {res.strip()[:300]}")
        # Every text must be on the device, or the oracle would be graded on less than it claims.
        on_device = {(m["digits"], m["body"]) for m in devicestate.read_sms(self.dev)}
        lost = [m["body"][:60] for m in sms
                if (re.sub(r"\D", "", m.get("digits") or m.get("address") or "")[-10:], m["body"]) not in on_device]
        if lost:
            raise RuntimeError(f"{len(lost)} of {len(sms)} oracle texts are not on the device: {lost}")
        self.dev.home()
        return {"dbs": sorted(dbs), "sms": len(sms), "fired": [f["id"] for f in fired]}

    def _wait_for_texts(self, ev: dict, timeout_s: float = 180) -> None:
        """A fired trigger's texts come in through the emulator modem, which stores them a few seconds later (longer on
        a slow host). Wait until every one is in the SMS store, so the capture can't run ahead of them."""
        def key(t: str) -> str:            # ASCII words only: emoji and line breaks can come back re-encoded
            return " ".join(re.sub(r"[^A-Za-z0-9]+", " ", t).split())
        want = [key(render(str(step["sms_in"]["body"]), self.vars))[:40]
                for step in ev.get("do") or [] if isinstance(step, dict) and "sms_in" in step]
        deadline = time.time() + timeout_s
        while want:
            bodies = [key(str(m.get("body") or "")) for m in devicestate.read_sms(self.dev)]
            want = [w for w in want if not any(w in b for b in bodies)]
            if not want:
                return
            if time.time() > deadline:
                raise RuntimeError(f"trigger {ev.get('id')}: {len(want)} text(s) not delivered after {timeout_s:.0f} s: {want}")
            time.sleep(2)

    # ---- 3. capture ---------------------------------------------------------------------------------
    def capture(self) -> dict:
        with self.lock:
            self.closed = True
        fired = (self.events.stop() if self.events else []) + getattr(self, "oracle_fired", [])
        started = (self.events.t0_device_ms if self.events else getattr(self, "agent_started_ms", None))
        out = self.state
        (out / "events.json").write_text(json.dumps(fired, indent=2))
        self.dev.screenshot(out / "screens" / "final.png")
        errors = []
        for aid, app in self.apps.items():
            if app.sqlite:
                try:
                    SqliteBackend(app, self.dev).pull(out / "backend" / aid / "after")
                except Exception as e:  # noqa: BLE001 - the grader reports a missing DB as INDETERMINATE
                    errors.append(f"{aid}: {type(e).__name__}: {e}")
        ds = None
        if self.task.capture_device is not None:
            try:
                ds = devicestate.capture(self.dev, self.task.capture_device or {})
                ds["agent_started_ms"] = started
                (out / "device_state.json").write_text(json.dumps(ds, indent=2, default=str))
            except Exception as e:  # noqa: BLE001
                errors.append(f"device_state: {type(e).__name__}: {e}")
        (out / "capture.json").write_text(json.dumps(
            {"steps": self.steps, "agent_started": started is not None, "errors": errors,
             "oracle": getattr(self, "oracle", False),
             "oracle_route_enabled": bool(os.environ.get("ORACLE_TOKEN"))}, indent=2))
        dest = out / "capture"
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir()
        for name in ("meta.json", "events.json", "device_state.json", "capture.json", "actions.jsonl", "web_check.json",
                     "backend", "screens"):
            src = out / name
            if src.is_dir():
                shutil.copytree(src, dest / name)
            elif src.exists():
                shutil.copy2(src, dest / name)
        return {"steps": self.steps, "errors": errors, "dir": str(dest)}


def serve(phone: Phone, host: str, port: int, token: str) -> None:

    meta = json.loads((phone.state / "meta.json").read_text())
    mcp = FastMCP("phone", host=host, port=port, stateless_http=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
                  instructions=(
                      "Tools for one Android phone (1080x2400 px). Coordinates are screen pixels. Look with "
                      f"screenshot or ui_tree, act with the other tools. You have {phone.max_steps} actions; "
                      "looking is free. Installed apps: " + ", ".join(f"{k} ({v})" for k, v in meta["apps"].items())))

    @mcp.tool()
    def screenshot() -> Image:
        """The current screen as a PNG (1080x2400)."""
        return phone.act("screenshot", {}, lambda: Image(data=phone.screenshot_png(), format="png"))

    @mcp.tool()
    def ui_tree() -> str:
        """The current screen's accessibility tree (uiautomator XML: text, content-desc, bounds)."""
        return phone.act("ui_tree", {}, phone.ui_tree)

    @mcp.tool()
    def tap(x: int, y: int) -> str:
        """Tap at pixel (x, y)."""
        return phone.act("tap", {"x": x, "y": y}, lambda: phone.dev.shell(f"input tap {x} {y}", check=True) or "ok")

    @mcp.tool()
    def long_press(x: int, y: int, ms: int = 800) -> str:
        """Press and hold at (x, y) for ms milliseconds."""
        return phone.act("long_press", {"x": x, "y": y, "ms": ms},
                         lambda: phone.dev.shell(f"input swipe {x} {y} {x} {y} {int(ms)}", check=True) or "ok")

    @mcp.tool()
    def swipe(x1: int, y1: int, x2: int, y2: int, ms: int = 400) -> str:
        """Swipe from (x1, y1) to (x2, y2). Swipe up to scroll down."""
        return phone.act("swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms},
                         lambda: phone.dev.shell(f"input swipe {x1} {y1} {x2} {y2} {int(ms)}", check=True) or "ok")

    @mcp.tool()
    def type_text(text: str) -> str:
        """Type text into the focused field (tap the field first). ASCII; newlines become line breaks."""
        return phone.act("type_text", {"text": text}, lambda: phone.type_text(text) or "ok")

    @mcp.tool()
    def press_key(key: str) -> str:
        """Press a key: back, home, enter, delete, recent_apps, tab, search."""
        if key not in KEYS:
            raise ValueError(f"key must be one of {sorted(KEYS)}")
        return phone.act("press_key", {"key": key},
                         lambda: phone.dev.shell(f"input keyevent {KEYS[key]}", check=True) or "ok")

    @mcp.tool()
    def launch_app(app: str) -> str:
        """Open an installed app by its name in list_apps (e.g. messages) or its package."""
        pkg = phone.packages.get(app, app)
        if pkg not in phone.packages.values():
            raise ValueError(f"unknown app {app!r}; see list_apps")
        return phone.act("launch_app", {"app": app}, lambda: (phone.dev.shell(
            f"monkey -p {shlex.quote(pkg)} -c android.intent.category.LAUNCHER 1", check=True), "ok")[1])

    @mcp.tool()
    def list_apps() -> dict:
        """The apps this task uses: name -> Android package."""
        return phone.act("list_apps", {}, lambda: phone.packages)

    @mcp.tool()
    def wait(seconds: float = 2.0) -> str:
        """Wait up to 30 seconds, e.g. for a delivery or a reply."""
        s = max(0.0, min(float(seconds), 30.0))
        return phone.act("wait", {"seconds": s}, lambda: time.sleep(s) or f"waited {s}s")

    @mcp.custom_route("/capture", methods=["POST"], include_in_schema=False)
    async def capture(request: Request) -> Response:
        if not secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        import anyio
        return JSONResponse(await anyio.to_thread.run_sync(phone.capture))

    oracle_token = os.environ.get("ORACLE_TOKEN")

    def oracle_ok(request: Request) -> bool:
        return bool(oracle_token) and secrets.compare_digest(request.headers.get("authorization", ""),
                                                             f"Bearer {oracle_token}")

    @mcp.custom_route("/oracle/before", methods=["POST"], include_in_schema=False)
    async def oracle_before(request: Request) -> Response:
        if not oracle_ok(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(phone.oracle_before())

    @mcp.custom_route("/oracle/apply", methods=["POST"], include_in_schema=False)
    async def oracle_apply(request: Request) -> Response:
        if not oracle_ok(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        import anyio
        body = await request.json()
        return JSONResponse(await anyio.to_thread.run_sync(
            lambda: phone.oracle_apply(body["dbs"], body["sms"], body.get("fire"))))

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> Response:
        return JSONResponse({"ready": (phone.state / "ready").exists(), "closed": phone.closed})

    mcp.run(transport="streamable-http")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="task YAML, relative to BENCH_ROOT")
    ap.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL", "emulator-5554"))
    ap.add_argument("--state", default="/state")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--max-steps", type=int)
    ap.add_argument("--token", default=os.environ.get("CAPTURE_TOKEN"))
    a = ap.parse_args()
    state = Path(a.state)
    state.mkdir(parents=True, exist_ok=True)
    if not a.token:
        # A fresh capture token per trial, readable only inside this container (the collect hook cats it).
        a.token = secrets.token_hex(24)
        tok = state / "capture_token"
        tok.write_text(a.token)
        tok.chmod(0o600)
    phone = Phone(BENCH / a.task, a.serial, state, a.max_steps)
    phone.prepare()
    serve(phone, a.host, a.port, a.token)


if __name__ == "__main__":
    main()
