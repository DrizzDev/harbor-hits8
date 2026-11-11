"""Backend state: the app's own SQLite database (seed, push, pull, digest, diff) and HTTP APIs.

The SQLite path is generic over any debuggable app whose state lives in a database under its data
dir. RideSF-style extras (profiles.json with expected digests, digest-queries.json, Room schema
exports) are used when the app config points at them.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from benchlib.config import App
from benchlib.device import Device, DeviceError


class SeedError(RuntimeError):
    pass


class SqliteBackend:
    def __init__(self, app: App, device: Device):
        if app.sqlite is None:
            raise SeedError(f"app {app.id} has no state.sqlite section")
        self.app, self.dev, self.cfg = app, device, app.sqlite
        self.pkg = app.package
        self.db_dir, _, self.db_name = self.cfg.db.rpartition("/")
        self.db_dir = self.db_dir or "."

    # ---- device file transfer (run-as: the app must be debuggable) -----------------------------
    def _run_as(self, cmd: str, **kw):
        # adb shell flattens argv into one string, so the device command is passed pre-quoted.
        return self.dev.adb("shell", f"run-as {self.pkg} sh -c '{cmd}'", **kw)

    def exists_on_device(self) -> bool:
        listing = self._run_as(f"ls {self.db_dir}", check=False)
        return self.db_name in str(listing).split()

    def pull(self, dest_dir: Path) -> Path:
        """Copy the live db + WAL/SHM to dest_dir and fold the WAL in, so the file is self-contained."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        db = dest_dir / self.db_name
        for attempt in range(3):   # run-as occasionally returns nothing on a loaded emulator
            if attempt:
                time.sleep(3)
            listing = str(self._run_as(f"ls {self.db_dir}", check=False)).split()
            for suffix in ("", "-wal", "-shm"):
                name = f"{self.db_name}{suffix}"
                target = dest_dir / name
                target.unlink(missing_ok=True)
                if name in listing:
                    target.write_bytes(self.dev.adb("exec-out", f"run-as {self.pkg} cat {self.db_dir}/{name}",
                                                    binary=True, timeout=120))
            if db.exists() and db.stat().st_size > 0:
                break
        if not db.exists() or db.stat().st_size == 0:
            raise SeedError(f"could not pull {self.cfg.db} from {self.pkg} (is the app debuggable?)")
        con = sqlite3.connect(db)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        for suffix in ("-wal", "-shm"):
            (dest_dir / f"{self.db_name}{suffix}").unlink(missing_ok=True)
        return db

    def push(self, local: Path) -> None:
        self.dev.force_stop(self.pkg)
        n = self.db_name
        self._run_as(f"mkdir -p {self.db_dir} && rm -f {self.db_dir}/{n}-wal {self.db_dir}/{n}-shm "
                     f"{self.db_dir}/{n}-journal", check=True)
        data = local.read_bytes()
        self.dev.adb("exec-in", f"run-as {self.pkg} sh -c 'cat > {self.db_dir}/{n}'", input=data, timeout=120)
        # exec-in can return before the device side has finished writing; wait for the full file.
        size = ""
        for _ in range(20):
            size = str(self._run_as(f"stat -c %s {self.db_dir}/{n}", check=False)).strip()
            if size == str(len(data)):
                return
            time.sleep(0.5)
        raise SeedError(f"pushed {len(data)} bytes but the device file has {size or 'no'} bytes")

    # ---- seeds ---------------------------------------------------------------------------------
    def _profile(self, seed: str) -> dict | None:
        if self.cfg.profiles and self.cfg.profiles.exists():
            return (json.loads(self.cfg.profiles.read_text()).get("seeds") or {}).get(seed)
        return None

    def resolve_seed(self, seed: str) -> tuple[Path, dict | None]:
        profile = self._profile(seed)
        if profile:
            path = self.cfg.profiles.parent / profile["file"]
            data = path.read_bytes()
            if profile.get("sha256") and hashlib.sha256(data).hexdigest() != profile["sha256"]:
                raise SeedError(f"seed {seed}: file hash does not match {self.cfg.profiles.name}")
            return path, profile
        for cand in (Path(seed), (self.cfg.seeds_dir or Path(".")) / seed, (self.cfg.seeds_dir or Path(".")) / f"{seed}.sql"):
            if cand.is_file():
                return cand.resolve(), None
        raise SeedError(f"seed '{seed}' not found (profiles: {self.cfg.profiles}, seeds_dir: {self.cfg.seeds_dir})")

    def _base_db(self, out: Path) -> None:
        """A database at the app's current schema, before the seed is applied."""
        if self.cfg.room_schema_dir:
            self._build_from_room_schema(out)
        elif self.cfg.template_db:
            out.write_bytes(self.cfg.template_db.read_bytes())
        else:
            if not self.exists_on_device():
                # Let the app create its schema once.
                self.dev.launch(self.pkg, self.app.launch_activity)
                deadline = time.time() + 30
                while time.time() < deadline and not self.exists_on_device():
                    time.sleep(1)
                self.dev.force_stop(self.pkg)
            with tempfile.TemporaryDirectory() as tmp:
                out.write_bytes(self.pull(Path(tmp)).read_bytes())

    def _build_from_room_schema(self, out: Path) -> None:
        version = None
        if self.cfg.profiles and self.cfg.profiles.exists():
            version = json.loads(self.cfg.profiles.read_text()).get("schemaVersion")
        files = sorted(self.cfg.room_schema_dir.glob("*.json"), key=lambda f: int(f.stem))
        schema_file = self.cfg.room_schema_dir / f"{version}.json" if version else files[-1]
        schema = json.loads(schema_file.read_text())["database"]
        out.unlink(missing_ok=True)
        con = sqlite3.connect(out)
        try:
            for entity in schema["entities"]:
                con.execute(entity["createSql"].replace("${TABLE_NAME}", entity["tableName"]))
                for index in entity.get("indices", []):
                    con.execute(index["createSql"].replace("${TABLE_NAME}", entity["tableName"]))
            for view in schema.get("views", []):
                con.execute(view["createSql"].replace("${VIEW_NAME}", view["viewName"]))
            for query in schema["setupQueries"]:
                con.execute(query)
            con.execute(f"PRAGMA user_version = {schema['version']}")
            con.commit()
        finally:
            con.close()

    def build_seeded(self, seed: str, out: Path, overlays: list[Path] = ()) -> dict:
        """Write the seeded database to `out`; returns seed metadata (file, expected digest...).

        `overlays` are SQL files applied after the seed (per-task deltas on a shared base world)."""
        path, profile = self.resolve_seed(seed)
        out.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix in (".db", ".sqlite", ".sqlite3"):
            out.write_bytes(path.read_bytes())
        else:
            self._base_db(out)
            con = sqlite3.connect(out)
            try:
                con.executescript(path.read_text())
                con.commit()
            finally:
                con.close()
        for ov in overlays:
            con = sqlite3.connect(out)
            try:
                con.executescript(Path(ov).read_text())
                con.commit()
            except sqlite3.Error as e:
                raise SeedError(f"overlay {ov}: {e}") from e
            finally:
                con.close()
        meta = {"seed": seed, "file": str(path), "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if overlays:
            meta["overlays"] = [str(o) for o in overlays]
            profile = None              # the profile's expected digest describes the bare seed
        if profile:
            meta.update({k: v for k, v in profile.items() if k not in ("file", "sha256")})
        if self.cfg.digest_queries:
            meta["host_digest"] = self.digest(out)
            expected = (profile or {}).get("stateDigest")
            if expected and meta["host_digest"] != expected:
                raise SeedError(f"seed {seed}: host digest {meta['host_digest']} != expected {expected}")
        return meta

    def seed(self, seed: str, work_dir: Path, overlays: list[Path] = ()) -> dict:
        """Seed the device and read it back, so a failed copy can never pass silently."""
        local = work_dir / "seeded.db"
        meta = self.build_seeded(seed, local, overlays)
        with tempfile.TemporaryDirectory() as tmp:
            for attempt in range(2):   # one full re-push if run-as flakes during push or read-back
                try:
                    self.push(local)
                    on_device = self.pull(Path(tmp))
                    break
                except (SeedError, DeviceError, sqlite3.DatabaseError):
                    if attempt:
                        raise
                    time.sleep(5)
            if self.cfg.digest_queries:
                meta["device_digest"] = self.digest(on_device)
                ok = meta["device_digest"] == meta["host_digest"]
            else:
                ok = table_counts(on_device) == table_counts(local)
        if not ok:
            raise SeedError(f"seed {seed}: database read back from the device differs from the seeded file")
        meta["verified_on_device"] = True
        return meta

    def digest(self, db: Path) -> str:
        spec = json.loads(self.cfg.digest_queries.read_text())
        null = spec.get("nullToken", "\\N")
        con = sqlite3.connect(db)
        try:
            parts: list[str] = []
            for q in spec["queries"]:
                parts.append(f"## {q['name']}\n")
                for row in con.execute(q["sql"]):
                    parts.append("\t".join(null if v is None else str(v) for v in row) + "\n")
        finally:
            con.close()
        return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


# ---- generic sqlite helpers --------------------------------------------------------------------

def tables(db: Path) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT IN ('android_metadata','room_master_table') ORDER BY name")]
    finally:
        con.close()


def table_counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(db)
    try:
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables(db)}
    finally:
        con.close()


def dump(db: Path, exclude: list[str] = ()) -> dict[str, list[dict]]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return {t: [dict(r) for r in con.execute(f'SELECT * FROM "{t}"')] for t in tables(db) if t not in exclude}
    finally:
        con.close()


def _primary_key(db: Path, table: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        cols = sorted((r[5], r[1]) for r in con.execute(f'PRAGMA table_info("{table}")') if r[5])
        return [c for _, c in cols]
    finally:
        con.close()


def state_diff(before_db: Path, after_db: Path, exclude: list[str] = ()) -> dict:
    """Row-level diff per table, keyed by primary key (whole row when there is none)."""
    before, after = dump(before_db, exclude), dump(after_db, exclude)
    diff: dict[str, dict] = {}
    for table in sorted(set(before) | set(after)):
        pk = _primary_key(after_db if table in after else before_db, table)

        def key(row: dict, pk: list[str] = pk) -> str:
            return json.dumps([row.get(c) for c in pk] if pk else row, sort_keys=True, default=str)

        b = {key(r): r for r in before.get(table, [])}
        a = {key(r): r for r in after.get(table, [])}
        changes = {
            "added": [a[k] for k in a.keys() - b.keys()],
            "removed": [b[k] for k in b.keys() - a.keys()],
            "changed": [{"before": b[k], "after": a[k],
                         "fields": sorted(c for c in a[k] if a[k].get(c) != b[k].get(c))}
                        for k in a.keys() & b.keys() if a[k] != b[k]],
        }
        if any(changes.values()):
            diff[table] = changes
    return diff


def diff_summary(diff: dict) -> dict[str, dict[str, int]]:
    return {t: {k: len(v) for k, v in c.items() if v} for t, c in diff.items()}


# ---- HTTP --------------------------------------------------------------------------------------

def http_request(method: str, url: str, headers: dict | None = None, body=None, timeout: float = 30) -> dict:
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode() if not isinstance(body, (bytes, str)) else (
            body.encode() if isinstance(body, str) else body)
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except urllib.error.URLError as e:
        return {"status": None, "error": str(e.reason), "url": url, "elapsed_ms": int((time.time() - started) * 1000)}
    text = raw.decode(errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    return {"status": status, "json": parsed, "text": text if parsed is None else None, "url": url,
            "elapsed_ms": int((time.time() - started) * 1000)}


def ensure_forward(dev: Device, spec: str | None) -> None:
    if spec:
        try:
            dev.adb("forward", spec, spec)
        except DeviceError:
            pass
