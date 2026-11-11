"""Harbor oracle for a Drizz mobile task: puts a known-correct end state on the phone, so `harbor run -a oracle` must
score 1. It checks the setup, the capture and the grader, not the agent.

The gold end state comes from the pack's own grader self-test (the state its author wrote by hand as the answer),
built here on this phone's own seeded databases, then written to the device through the phone service's oracle
route (real database pushes and real SMS rows). Nothing is graded here; the verifier grades what the device holds.

    python3 /solution/oracle.py            # PHONE_URL and ORACLE_TOKEN come from [solution].env
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "oracle.json").read_text())   # {"kind": "dyn5"|"d01b"|"n4", "tid": "d02"}
URL = os.environ.get("PHONE_URL", "http://android:8000")
TOKEN = os.environ["ORACLE_TOKEN"]


def call(path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(URL + path, method="POST", data=json.dumps(body or {}).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def gold(dbs: dict[str, Path]) -> list[dict]:
    """Apply the self-test's gold answer to copies of the seeded DBs (in place); returns the texts it implies."""
    kind, tid, root = CFG["kind"], CFG["tid"], HERE / "gold" / "docs"
    if kind == "dyn5":
        st = load("dyn5_selftest", root / "dyn5" / "selftest.py")
        return st.BUILDERS[tid](dbs, "gold")
    if kind == "d01b":
        # qualify.py defines D01b's builder and then runs its cases; only the definitions are needed.
        q = root / "dyn5_d01b" / "qualify.py"
        src = q.read_text().split("\nCASES = ")[0]
        ns = {"__file__": str(q), "__name__": "d01b_qualify"}
        exec(compile(src, str(q), "exec"), ns)
        return ns["builder"](dbs, "gold")
    if kind == "n4":
        st = load("n4_selftest", root / "n4" / "selftest.py")
        return st.GOLD[tid](dbs, st.G.ANSWERS[tid])
    raise ValueError(f"unknown oracle kind {kind}")


def main() -> int:
    before = call("/oracle/before")
    with tempfile.TemporaryDirectory() as td:
        dbs = {}
        for app, data in before.items():
            p = Path(td) / f"{app}.db"
            p.write_bytes(base64.b64decode(data))
            dbs[app] = p
        sms = gold(dbs)
        changed = {app: base64.b64encode(p.read_bytes()).decode() for app, p in dbs.items()
                   if p.read_bytes() != base64.b64decode(before[app])}
        for side in ("-wal", "-shm"):
            for p in dbs.values():
                Path(str(p) + side).unlink(missing_ok=True)
    fire = CFG.get("fire") or []
    if fire:   # these texts arrive for real when the phone fires the triggers; don't insert copies
        sms = [m for m in sms if m["type"] != "inbox"]
    res = call("/oracle/apply", {"dbs": changed, "sms": sms, "fire": fire})
    print(f"oracle {CFG['kind']} {CFG['tid']}: wrote {res['dbs']} and {res['sms']} texts; fired {res['fired']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
