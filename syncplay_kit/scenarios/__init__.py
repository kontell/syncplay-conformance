"""Scenario registry."""
from dataclasses import dataclass
from typing import Callable

from . import phase0, protocol_v2


@dataclass
class Scenario:
    name: str
    func: Callable
    suite: str      # "fast" or "slow"
    min_users: int
    timeout: float  # seconds


SCENARIOS = [
    Scenario("group_info_members", phase0.group_info_members, "fast", 1, 60),
    Scenario("v2_negotiation", protocol_v2.v2_negotiation, "fast", 1, 60),
    Scenario("ws_timesync", protocol_v2.ws_timesync, "fast", 1, 60),
    Scenario("group_wait_deadline", phase0.group_wait_deadline, "fast", 2, 90),
    Scenario("buffering_grace_absorb", phase0.buffering_grace_absorb, "fast", 2, 90),
    Scenario("buffering_grace_expiry", phase0.buffering_grace_expiry, "fast", 2, 90),
    Scenario("state_version", protocol_v2.state_version, "fast", 2, 90),
    Scenario("position_beacons", protocol_v2.position_beacons, "fast", 3, 120),
    Scenario("snapshot_on_demand", protocol_v2.snapshot_on_demand, "fast", 2, 90),
    Scenario("resync_per_version", protocol_v2.resync_per_version, "fast", 2, 90),
    Scenario("adaptive_tolerance", protocol_v2.adaptive_tolerance, "fast", 2, 120),
    Scenario("hot_join", protocol_v2.hot_join, "fast", 3, 120),
    Scenario("reconnect_grace", protocol_v2.reconnect_grace, "slow", 2, 240),
    Scenario("grace_expiry", protocol_v2.grace_expiry, "slow", 2, 300),
]
