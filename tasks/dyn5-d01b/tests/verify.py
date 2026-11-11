"""Harbor verifier for a Drizz mobile task. Grades the phone's final state with the task's original grader.

    python3 verify.py                       # in Harbor: grades /state/capture, collected from the phone container
    python3 verify.py --from-dir <dir>      # replay: a saved bench trial folder or any capture folder

It reads only what the device holds: the app databases before and after, the SMS and contacts store, and the log of
which mid-run triggers fired. It never reads the agent's actions, messages or claims.

Reward: PASS -> 1, FAIL or VIOLATION -> 0, written to reward.json. INDETERMINATE (a DB or the SMS store could not be
read, a trigger's action failed, a trigger fired too early) and any capture error write no reward and exit non-zero:
Harbor then marks the reward invalid and leaves the trial out of the mean, instead of scoring a harness fault as 0.
checkpoints.json always holds every check, its detail and the verdict.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "grader.json").read_text())   # {"grader": "grade/D02/grade.py", "vars": {...}, "apps": {...}}


def context(root: Path) -> SimpleNamespace:
    """The bench's GradeContext fields that the graders use, built from a capture or a saved trial folder."""
    dbs = {}
    for app, name in CONFIG["apps"].items():
        d = {}
        for which in ("before", "after"):
            p = root / "backend" / app / which / name
            if p.exists():
                d[which] = p
        dbs[app] = d
    ds = root / "device_state.json"
    events = root / "events.json"
    if not events.exists() and (root / "result.json").exists():      # saved bench trials keep events in result.json
        events_list = json.loads((root / "result.json").read_text()).get("events") or []
    else:
        events_list = json.loads(events.read_text()) if events.exists() else []
    return SimpleNamespace(vars=dict(CONFIG["vars"]), dbs=dbs,
                           device_state=json.loads(ds.read_text()) if ds.exists() else None,
                           extra={"events": events_list})


def grade(root: Path) -> dict:
    spec = importlib.util.spec_from_file_location("task_grader", HERE / CONFIG["grader"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.grade(context(root))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-dir", default=os.environ.get("CAPTURE_DIR", "/state/capture"))
    ap.add_argument("--logs", default=os.environ.get("VERIFIER_LOGS", "/logs/verifier"))
    a = ap.parse_args()
    logs = Path(a.logs)
    logs.mkdir(parents=True, exist_ok=True)
    for stale in ("reward.json", "reward.txt"):
        (logs / stale).unlink(missing_ok=True)
    report = {"task_id": CONFIG["task_id"], "verdict": None, "reward": None}
    try:
        root = Path(a.from_dir)
        if not root.is_dir():
            raise FileNotFoundError(f"no captured device state at {root}")
        if (root / "capture.json").exists():
            report["capture"] = json.loads((root / "capture.json").read_text())
        out = grade(root)
        diag = out.get("diagnostics") or {}
        verdict = diag.get("verdict")
        report.update(verdict=verdict, score=diag.get("score"), diagnostics=diag,
                      success_checks={c["id"]: c["passed"] for c in out["success_checks"]},
                      prohibited_checks={c["id"]: not c["passed"] for c in out["prohibition_checks"]},
                      checks=out["success_checks"] + out["prohibition_checks"])
    except Exception as e:  # noqa: BLE001 - a verifier error never produces a reward
        report.update(verdict="ERROR", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc())
    if report["verdict"] in ("PASS", "FAIL", "VIOLATION"):
        report["reward"] = 1.0 if report["verdict"] == "PASS" else 0.0
        (logs / "reward.json").write_text(json.dumps({"reward": report["reward"]}))
    (logs / "checkpoints.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"{CONFIG['task_id']}: {report['verdict']} {report.get('score') or ''} reward={report['reward']}")
    return 0 if report["reward"] is not None else 2


if __name__ == "__main__":
    sys.exit(main())
