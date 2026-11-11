"""Harness registry: the `type:` field of a harness YAML picks the class."""

from __future__ import annotations

from benchlib.config import HarnessSpec
from benchlib.harnesses.base import Harness, HarnessResult


class NoOp(Harness):
    """Does nothing. Run it once per task: a task that passes with no agent is broken."""

    def run(self, ctx):
        return HarnessResult("completed", agent_status="failure", agent_summary="no-op baseline")


def create(spec: HarnessSpec) -> Harness:
    if spec.type == "drizz_agent":
        from benchlib.harnesses.drizz_agent import DrizzAgent
        return DrizzAgent(spec)
    if spec.type == "artemis":
        from benchlib.harnesses.artemis import Artemis
        return Artemis(spec)
    if spec.type == "command":
        from benchlib.harnesses.command import Command
        return Command(spec)
    if spec.type == "noop":
        return NoOp(spec)
    raise ValueError(f"unknown harness type '{spec.type}' in {spec.path}")
