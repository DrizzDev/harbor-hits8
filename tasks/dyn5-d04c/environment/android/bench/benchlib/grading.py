"""Check engine for validity (before the run) and grading (after it).

SQL checks run on the app database after the run (`db: before` for the starting state); the
starting state is also attached as schema `before` for before/after comparisons.

A check is a dict with an `id`, a `type` and an `expect`:

    - id: ride_booked
      type: sql                     # sql | http | adb | shell | ui | answer | agent_status | python
      query: SELECT status FROM rides ORDER BY createdAt DESC LIMIT 1
      expect: SCHEDULED             # scalar = equality, or a dict of operators (all must hold)

Operators: eq ne gt gte lt lte in not_in contains not_contains icontains regex empty not_empty
between [lo, hi]  approx {value, tol}  len_eq len_gte len_lte.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from benchlib.backend import ensure_forward, http_request
from benchlib.config import App, render
from benchlib.device import Device


@dataclass
class GradeContext:
    app: App
    device: Device
    out_dir: Path
    vars: dict
    before_db: Path | None = None
    after_db: Path | None = None
    final_ui_xml: str | None = None
    agent_answer: str | None = None
    agent_status: str | None = None
    extra: dict = field(default_factory=dict)   # anything else a python grader may want (run result)
    apps: dict = field(default_factory=dict)    # app id -> App (multi-app tasks)
    dbs: dict = field(default_factory=dict)     # app id -> {"before": Path, "after": Path}
    device_state: dict | None = None            # stock-app snapshot (SMS, contacts, alarms, files)

    def db(self, app: str | None = None, which: str = "after") -> Path | None:
        if app is None:
            return self.before_db if which == "before" else self.after_db
        return (self.dbs.get(app) or {}).get(which)


# ---- comparison --------------------------------------------------------------------------------

def _num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def compare(actual: Any, expect: Any) -> tuple[bool, str]:
    if not isinstance(expect, dict):
        expect = {"eq": expect}
    failures = []
    for op, exp in expect.items():
        a = actual
        try:
            if op == "eq":
                ok = a == exp or (_num(a) == _num(exp) and not isinstance(_num(a), str)) or str(a) == str(exp)
            elif op == "ne":
                ok = not (a == exp or str(a) == str(exp))
            elif op in ("gt", "gte", "lt", "lte"):
                x, y = _num(a), _num(exp)
                ok = a is not None and {"gt": x > y, "gte": x >= y, "lt": x < y, "lte": x <= y}[op]
            elif op == "between":
                ok = a is not None and _num(exp[0]) <= _num(a) <= _num(exp[1])
            elif op == "approx":
                ok = a is not None and abs(_num(a) - _num(exp["value"])) <= _num(exp.get("tol", 0))
            elif op == "in":
                ok = a in exp or str(a) in [str(e) for e in exp]
            elif op == "not_in":
                ok = not (a in exp or str(a) in [str(e) for e in exp])
            elif op == "contains":
                ok = a is not None and (exp in a if isinstance(a, (list, dict)) else str(exp) in str(a))
            elif op == "icontains":
                ok = a is not None and str(exp).lower() in str(a).lower()
            elif op == "not_contains":
                ok = a is None or (exp not in a if isinstance(a, (list, dict)) else str(exp) not in str(a))
            elif op == "regex":
                ok = a is not None and re.search(str(exp), str(a), re.S) is not None
            elif op == "empty":
                ok = (not a) == bool(exp)
            elif op == "not_empty":
                ok = bool(a) == bool(exp)
            elif op in ("len_eq", "len_gte", "len_lte"):
                n = len(a or [])
                ok = {"len_eq": n == exp, "len_gte": n >= exp, "len_lte": n <= exp}[op]
            else:
                return False, f"unknown operator '{op}'"
        except (TypeError, ValueError, KeyError, IndexError) as e:
            ok = False
            failures.append(f"{op} {exp!r}: {e}")
            continue
        if not ok:
            failures.append(f"{op} {exp!r}")
    return not failures, "ok" if not failures else "failed: " + ", ".join(failures)


def extract(obj: Any, path: str | None) -> Any:
    """Dotted path into JSON: data.items.0.status ; `len(items)` returns a length."""
    if not path:
        return obj
    m = re.fullmatch(r"len\((.*)\)", path)
    if m:
        v = extract(obj, m[1])
        return len(v) if v is not None else None
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _short(v: Any, n: int = 300) -> Any:
    s = json.dumps(v, default=str) if not isinstance(v, str) else v
    return v if len(s) <= n else s[:n] + "…"


# ---- check types ----------------------------------------------------------------------------------

def _sql(check: dict, ctx: GradeContext) -> Any:
    app = check.get("app")
    before_db = ctx.db(app, "before")
    db = before_db if check.get("db") == "before" else ctx.db(app, "after")
    if db is None or not Path(db).exists():
        raise RuntimeError(f"no app database available for {app or ctx.app.id} (no state.sqlite, or the pull failed)")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    # The starting state is attached as `before`, so a check can compare: JOIN before.rides b USING (id)
    if before_db and Path(before_db).exists() and db != before_db:
        con.execute("ATTACH DATABASE ? AS before", (str(before_db),))
    try:
        rows = [dict(r) for r in con.execute(check["query"], check.get("params") or [])]
    finally:
        con.close()
    if check.get("rows"):
        return rows
    return next(iter(rows[0].values())) if rows else None


def _http(check: dict, ctx: GradeContext) -> Any:
    url = check["url"]
    headers = dict(check.get("headers") or {})
    if not url.startswith(("http://", "https://")):
        if not ctx.app.http:
            raise RuntimeError("relative url but the app has no state.http.base_url")
        ensure_forward(ctx.device, ctx.app.http.adb_forward)
        url = ctx.app.http.base_url + "/" + url.lstrip("/")
        headers = {**ctx.app.http.headers, **headers}
    resp = http_request(check.get("method", "GET"), url, headers, check.get("body"), check.get("timeout", 30))
    responses = ctx.out_dir / "backend" / "http"
    responses.mkdir(parents=True, exist_ok=True)
    (responses / f"{check['id']}.json").write_text(json.dumps(resp, indent=2, default=str))
    if resp.get("status") is None:
        raise RuntimeError(f"request failed: {resp.get('error')}")
    want_status = check.get("status")
    if want_status is not None and resp["status"] != want_status:
        raise RuntimeError(f"HTTP {resp['status']} (expected {want_status})")
    body = resp["json"] if resp["json"] is not None else resp["text"]
    return extract(body, check.get("path")) if check.get("path") != "status" else resp["status"]


def _shell(check: dict, ctx: GradeContext) -> Any:
    p = subprocess.run(check["cmd"], shell=True, capture_output=True, text=True, timeout=check.get("timeout", 60))
    if check.get("expect_exit") is not None and p.returncode != check["expect_exit"]:
        raise RuntimeError(f"exit {p.returncode}: {p.stderr.strip()[:200]}")
    out = p.stdout.strip()
    return extract(json.loads(out), check["path"]) if check.get("path") else out


def _adb(check: dict, ctx: GradeContext) -> Any:
    """`cmd` is a device shell command ("content query --uri ...") or raw adb args ("shell ..." / "exec-out ...")."""
    cmd = check["cmd"]
    if cmd.startswith("shell "):
        out = ctx.device.shell(cmd[6:])
    elif cmd.split()[0] in ("exec-out", "pull", "forward", "emu"):
        out = ctx.device.adb(*shlex.split(cmd), check=False)
    else:
        out = ctx.device.shell(cmd)
    return extract(json.loads(out), check["path"]) if check.get("path") else out


def _ui(check: dict, ctx: GradeContext) -> Any:
    """Is a node present on the final screen? Returns True/False (compare with expect, default True)."""
    if not ctx.final_ui_xml:
        raise RuntimeError("no final UI hierarchy captured")
    root = ET.fromstring(ctx.final_ui_xml)
    for n in root.iter("node"):
        if "text" in check and check["text"] not in (n.get("text") or ""):
            continue
        if "text_regex" in check and not re.search(check["text_regex"], n.get("text") or ""):
            continue
        if "resource_id" in check and (n.get("resource-id") or "").split("/")[-1] != check["resource_id"]:
            continue
        if "content_desc" in check and check["content_desc"] not in (n.get("content-desc") or ""):
            continue
        return True
    return False


CHECKS: dict[str, Callable[[dict, GradeContext], Any]] = {
    "sql": _sql, "http": _http, "shell": _shell, "adb": _adb, "ui": _ui,
    "answer": lambda c, ctx: ctx.agent_answer,
    "agent_status": lambda c, ctx: ctx.agent_status,
}


def load_callable(spec: str) -> Callable:
    file, func = spec.rsplit(":", 1)
    name = f"bench_grader_{Path(file).stem}"
    mod_spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(mod_spec)
    sys.modules[name] = module          # dataclasses in the grader resolve their module through sys.modules
    mod_spec.loader.exec_module(module)
    return getattr(module, func)


def run_checks(checks: list[dict], ctx: GradeContext, kind: str) -> list[dict]:
    results = []
    for raw in checks:
        check = render(raw, ctx.vars)
        cid = check.get("id") or f"{kind}_{len(results) + 1}"
        ctype = check.get("type", "sql")
        base = {"id": cid, "kind": kind, "type": ctype, "description": check.get("description", "")}
        if ctype == "python":
            results += [{**base, **r, "kind": r.get("kind", kind)} for r in _python(check, ctx)]
            continue
        expect = check.get("expect", True if ctype == "ui" else {"not_empty": True})
        try:
            actual = CHECKS[ctype](check, ctx)
            passed, detail = compare(actual, expect)
            results.append({**base, "passed": passed, "actual": _short(actual), "expected": expect, "detail": detail})
        except KeyError as e:
            results.append({**base, "passed": False, "error": f"unknown check type or missing field: {e}"})
        except Exception as e:  # noqa: BLE001 - a broken check must fail the check, not the run
            results.append({**base, "passed": False, "error": f"{type(e).__name__}: {e}"})
    return results


def _python(check: dict, ctx: GradeContext) -> list[dict]:
    try:
        out = load_callable(check["fn"])(ctx, **(check.get("args") or {}))
    except Exception as e:  # noqa: BLE001
        return [{"id": check.get("id", "python"), "passed": False, "error": f"{type(e).__name__}: {e}"}]
    return normalize_grader_output(out)


def normalize_grader_output(out: Any) -> list[dict]:
    """Python graders may return [{id, passed, detail}], or {success_checks, prohibition_checks, diagnostics}."""
    if isinstance(out, dict):
        rows = [{**c, "kind": "success"} for c in out.get("success_checks", [])]
        rows += [{**c, "kind": "prohibited"} for c in out.get("prohibition_checks", [])]
        if out.get("diagnostics") is not None:
            rows.append({"id": "_diagnostics", "kind": "info", "passed": True, "detail": out["diagnostics"]})
        return rows
    return list(out or [])
