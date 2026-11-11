"""Load app, task and harness definitions (YAML) and render {{templates}}.

Paths inside a YAML file are resolved relative to that file, so configs can point at sibling repos
(for example ../drizz-uber-app/seeds) without caring where the runner is started from.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from benchlib import ROOT

APPS_DIR = ROOT / "apps"
TASKS_DIR = ROOT / "tasks"
HARNESSES_DIR = ROOT / "harnesses"
MODELS_FILE = ROOT / "models.yaml"

_TEMPLATE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def render(value: Any, ctx: dict) -> Any:
    """Replace {{name}} / {{a.b}} in strings (recursively through lists and dicts).

    A string that is exactly one placeholder keeps the value's type, so `expect: {eq: "{{n}}"}`
    compares numbers as numbers. Unknown names are left untouched.
    """
    if isinstance(value, str):
        whole = _TEMPLATE.fullmatch(value.strip())
        if whole:
            found, v = _lookup(ctx, whole[1])
            return v if found else value
        return _TEMPLATE.sub(lambda m: str(_lookup(ctx, m[1])[1]) if _lookup(ctx, m[1])[0] else m[0], value)
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    return value


def _lookup(ctx: dict, dotted: str) -> tuple[bool, Any]:
    cur: Any = ctx
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


def _load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at top level")
    return data


def _resolve(base: Path, p: str | None) -> Path | None:
    if not p:
        return None
    p = os.path.expanduser(str(p))
    return (base / p).resolve() if not os.path.isabs(p) else Path(p)


def _find(directory: Path, ref: str, kind: str) -> Path:
    """`ref` is a file path or an id; ids are matched against `id:` or the file stem."""
    p = Path(ref)
    if p.suffix in (".yaml", ".yml") and p.exists():
        return p.resolve()
    for f in sorted(directory.rglob("*.y*ml")):
        if f.stem == ref:
            return f
        try:
            if _load_yaml(f).get("id") == ref:
                return f
        except (yaml.YAMLError, ValueError):
            continue
    raise FileNotFoundError(f"no {kind} '{ref}' in {directory}")


# ---- apps ------------------------------------------------------------------------------------

@dataclass
class SqliteState:
    db: str                                  # path inside the app's data dir, e.g. databases/app.db
    seeds_dir: Path | None = None
    profiles: Path | None = None             # RideSF-style profiles.json: seed -> file, sha256, stateDigest
    digest_queries: Path | None = None       # RideSF-style digest-queries.json
    room_schema_dir: Path | None = None      # Room exported schemas: build the db on the host from scratch
    template_db: Path | None = None          # or start from this file instead of the device's db
    diff_exclude: list[str] = field(default_factory=list)


@dataclass
class HttpState:
    base_url: str
    headers: dict = field(default_factory=dict)
    adb_forward: str | None = None           # "tcp:8787" -> adb forward tcp:8787 tcp:8787


@dataclass
class App:
    id: str
    name: str
    package: str
    path: Path
    apk: Path | None = None
    launch_activity: str | None = None
    launch_wait_s: float = 5
    description: str = ""
    sqlite: SqliteState | None = None
    http: HttpState | None = None
    raw: dict = field(default_factory=dict)


def load_app(ref: str) -> App:
    path = _find(APPS_DIR, ref, "app")
    d = _load_yaml(path)
    base = path.parent
    state = d.get("state") or {}
    sqlite = None
    if s := state.get("sqlite"):
        sqlite = SqliteState(
            db=s["db"], seeds_dir=_resolve(base, s.get("seeds_dir")), profiles=_resolve(base, s.get("profiles")),
            digest_queries=_resolve(base, s.get("digest_queries")),
            room_schema_dir=_resolve(base, s.get("room_schema_dir")), template_db=_resolve(base, s.get("template_db")),
            diff_exclude=list(s.get("diff_exclude") or []))
    http = None
    if h := state.get("http"):
        http = HttpState(base_url=h["base_url"].rstrip("/"), headers=h.get("headers") or {},
                         adb_forward=h.get("adb_forward"))
    return App(id=d["id"], name=d.get("name", d["id"]), package=d["package"], path=path,
               apk=_resolve(base, d.get("apk")), launch_activity=d.get("launch_activity"),
               launch_wait_s=float(d.get("launch_wait_s", 5)), description=d.get("description", ""),
               sqlite=sqlite, http=http, raw=d)


# ---- tasks -----------------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    app: str
    prompt: str
    path: Path
    title: str = ""
    tags: list[str] = field(default_factory=list)
    difficulty: str | None = None
    vars: dict = field(default_factory=dict)
    setup: list = field(default_factory=list)
    validity: list = field(default_factory=list)
    success: list = field(default_factory=list)
    prohibited: list = field(default_factory=list)
    grader: str | None = None                # "path/to/file.py:function"
    cleanup: list = field(default_factory=list)
    timeout_s: int = 600
    max_steps: int = 50
    apps: list[str] = field(default_factory=list)      # every app whose state is seeded/pulled (primary first)
    events: list = field(default_factory=list)         # mid-run events: [{id, when: {...}, do: [steps]}]
    capture_device: dict | None = None                 # stock-app state to snapshot after the run
    raw: dict = field(default_factory=dict)


def load_task(ref: str | Path) -> Task:
    path = _find(TASKS_DIR, str(ref), "task")
    d = _load_yaml(path)
    limits = d.get("limits") or {}
    grading = d.get("grading") or {}
    grader = grading.get("grader")
    if grader and ":" in grader:
        file, func = grader.rsplit(":", 1)
        grader = f"{_resolve(path.parent, file)}:{func}"
    return Task(id=d["id"], app=d["app"], prompt=d["prompt"].strip(), path=path, title=d.get("title", ""),
                tags=d.get("tags") or [], difficulty=d.get("difficulty"), vars=d.get("vars") or {},
                setup=d.get("setup") or [], validity=d.get("validity") or [],
                success=grading.get("success") or [], prohibited=grading.get("prohibited") or [],
                grader=grader, cleanup=d.get("cleanup") or ["stop_app"],
                timeout_s=int(limits.get("timeout_s", 600)), max_steps=int(limits.get("max_steps", 50)),
                apps=[d["app"]] + [a for a in (d.get("apps") or []) if a != d["app"]],
                events=d.get("events") or [], capture_device=d.get("capture_device"), raw=d)


def discover_tasks(selectors: list[str]) -> list[Task]:
    """Each selector is a task file, a task id, a directory, or a glob relative to tasks/."""
    tasks: list[Task] = []
    for sel in selectors:
        p = Path(sel)
        if p.is_dir() or (TASKS_DIR / sel).is_dir():
            d = p if p.is_dir() else TASKS_DIR / sel
            tasks += [load_task(f) for f in sorted(d.rglob("*.y*ml"))]
        elif any(ch in sel for ch in "*?["):
            tasks += [load_task(f) for f in sorted(TASKS_DIR.glob(sel)) if f.suffix in (".yaml", ".yml")]
        else:
            tasks.append(load_task(sel))
    seen, unique = set(), []
    for t in tasks:
        if t.id not in seen:
            seen.add(t.id)
            unique.append(t)
    return unique


# ---- harnesses and models --------------------------------------------------------------------

@dataclass
class HarnessSpec:
    id: str
    type: str
    path: Path
    params: dict = field(default_factory=dict)
    parallel_safe: bool = True
    before_run: list = field(default_factory=list)   # setup-step syntax, run after task setup
    description: str = ""


def load_harness(ref: str, overrides: dict | None = None) -> HarnessSpec:
    path = _find(HARNESSES_DIR, ref, "harness")
    d = _load_yaml(path)
    params = dict(d.get("params") or {})
    for k, v in (overrides or {}).items():
        params[k] = v
    # Path-like params are resolved relative to the harness file.
    for k, v in list(params.items()):
        if k.endswith(("_dir", "_file", "_path")) and isinstance(v, str):
            params[k] = str(_resolve(path.parent, v))
    return HarnessSpec(id=d["id"], type=d["type"], path=path, params=params,
                       parallel_safe=bool(d.get("parallel_safe", True)), before_run=d.get("before_run") or [],
                       description=d.get("description", ""))


@dataclass
class ModelSpec:
    provider: str            # anthropic | openai | openai-chat | google | ... (harness-dependent)
    name: str
    alias: str
    params: dict = field(default_factory=dict)
    price_in: float | None = None     # USD per 1M input tokens
    price_out: float | None = None
    price_cached: float | None = None

    @property
    def id(self) -> str:
        return f"{self.provider}:{self.name}"


def load_model(ref: str) -> ModelSpec:
    """`ref` is an alias from models.yaml or provider:model."""
    table = (_load_yaml(MODELS_FILE).get("models") or {}) if MODELS_FILE.exists() else {}
    entry = table.get(ref)
    if entry is None:
        if ":" not in ref:
            raise ValueError(f"model '{ref}' is neither an alias in models.yaml nor provider:model")
        provider, name = ref.split(":", 1)
        entry = next((e for e in table.values() if e.get("provider") == provider and e.get("model") == name),
                     {"provider": provider, "model": name})
    price = entry.get("price") or {}
    return ModelSpec(provider=entry["provider"], name=entry["model"], alias=ref, params=entry.get("params") or {},
                     price_in=price.get("input"), price_out=price.get("output"), price_cached=price.get("cached_input"))
