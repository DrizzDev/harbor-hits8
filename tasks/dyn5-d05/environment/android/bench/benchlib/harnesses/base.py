"""Harness interface. A harness drives the device toward the task and reports what it did.

The runner owns setup, artifacts and grading; a harness only has to:
  * work until done, `ctx.deadline` passes or `ctx.max_steps` actions were taken;
  * write per-step records to `ctx.trajectory` (TrajectoryWriter) and screens under `ctx.screens_dir`;
  * return a HarnessResult (outcome, the agent's own claim, step and token counts).

Harnesses that cannot report steps (external CLIs) use ScreenRecorder for periodic screens.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from benchlib.config import App, HarnessSpec, ModelSpec, Task
from benchlib.device import Device


@dataclass
class RunContext:
    task: Task
    app: App
    harness: HarnessSpec
    model: ModelSpec
    device: Device
    instruction: str             # rendered prompt
    out_dir: Path
    deadline: float              # time.time() at which the harness must stop
    max_steps: int
    vars: dict
    log: "RunLog"

    @property
    def screens_dir(self) -> Path:
        d = self.out_dir / "screens"
        d.mkdir(exist_ok=True)
        return d

    def time_left(self) -> float:
        return self.deadline - time.time()


@dataclass
class Usage:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: "Usage") -> None:
        for k in vars(self):
            setattr(self, k, getattr(self, k) + getattr(other, k))


@dataclass
class HarnessResult:
    outcome: str                       # completed | timeout | max_steps | harness_error | refused
    agent_status: str | None = None    # what the agent claimed: success | failure | infeasible | None
    agent_answer: str | None = None
    agent_summary: str | None = None
    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
    extra: dict = field(default_factory=dict)


class RunLog:
    """Human-readable log for one run (harness.log), also echoed to the console when verbose."""

    def __init__(self, path: Path, prefix: str, echo: bool):
        self.path, self.prefix, self.echo = path, prefix, echo
        self._lock = threading.Lock()

    def __call__(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        with self._lock, self.path.open("a") as f:
            f.write(line + "\n")
        if self.echo:
            print(f"[{self.prefix}] {line}", flush=True)


class TrajectoryWriter:
    def __init__(self, path: Path):
        self.path = path

    def write(self, record: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")


class ScreenRecorder(threading.Thread):
    """Saves a screenshot every `interval_s` when the screen changed. For harnesses that run as black boxes."""

    def __init__(self, device: Device, out_dir: Path, interval_s: float = 3.0):
        super().__init__(daemon=True)
        self.device, self.out_dir, self.interval_s = device, out_dir, interval_s
        self._halt = threading.Event()
        self.frames: list[dict] = []
        self._last = None
        self._t0 = time.time()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                data = self.device.screenshot_png()
                digest = hashlib.sha1(data).hexdigest()
                if digest != self._last:
                    self._last = digest
                    name = f"t{len(self.frames):04d}-{int(time.time() - self._t0):05d}s.png"
                    (self.out_dir / name).write_bytes(data)
                    self.frames.append({"file": name, "t_s": round(time.time() - self._t0, 1)})
            except Exception:  # noqa: BLE001 - a missed frame must never break the run
                pass
            self._halt.wait(self.interval_s)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=10)


class VideoRecorder(threading.Thread):
    """adb screenrecord in 170 s chunks (Android caps one recording at 180 s), pulled at the end."""

    def __init__(self, device: Device, out_dir: Path, bit_rate: str = "4000000"):
        super().__init__(daemon=True)
        self.device, self.out_dir, self.bit_rate = device, out_dir, bit_rate
        self._halt = threading.Event()
        self._proc: subprocess.Popen | None = None
        self.chunks: list[str] = []

    def run(self) -> None:
        i = 0
        while not self._halt.is_set():
            remote = f"/sdcard/__bench_rec_{i:03d}.mp4"
            self._proc = subprocess.Popen(
                ["adb", "-s", self.device.serial, "shell", "screenrecord", "--time-limit", "170",
                 "--bit-rate", self.bit_rate, remote], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.chunks.append(remote)
            while self._proc.poll() is None and not self._halt.is_set():
                time.sleep(0.5)
            i += 1

    def stop(self) -> list[Path]:
        self._halt.set()
        # Ask screenrecord to finish the file cleanly (SIGINT), then pull every chunk.
        self.device.shell("pkill -INT screenrecord")
        if self._proc:
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self.join(timeout=10)
        time.sleep(1)
        files = []
        for n, remote in enumerate(self.chunks):
            local = self.out_dir / f"video-{n:03d}.mp4"
            self.device.adb("pull", remote, str(local), check=False, timeout=120)
            self.device.shell(f"rm -f {remote}")
            if local.exists() and local.stat().st_size > 0:
                files.append(local)
        return files


class Harness:
    """Subclasses implement run(); `parallel_safe` False means one run at a time across all devices."""

    def __init__(self, spec: HarnessSpec):
        self.spec = spec
        self.params = spec.params

    def check_model(self, model: ModelSpec) -> None:
        """Raise ValueError early when this harness cannot run `model`."""

    def run(self, ctx: RunContext) -> HarnessResult:
        raise NotImplementedError
