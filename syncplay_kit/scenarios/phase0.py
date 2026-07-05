"""Robustness scenarios: bounded group-wait, buffering grace, member status.

These verify the behaviours specified in docs/SYNCPLAY.md sections 5.2 and 7.
"""
import asyncio
import time

from ..client import cmd_pos_estimate
from .common import make_group, set_queue, start_playing, member_of


async def group_info_members(ctx):
    """GroupInfoDto carries a Members[] list with per-member status."""
    clients, gid, ga = await make_group(ctx, [2])
    members = ga.get("Data", {}).get("Members")
    ctx.check(
        "Members list in GroupInfoDto",
        isinstance(members, list) and len(members) == 1 and "IsBuffering" in members[0]
        and "IgnoreGroupWait" in members[0] and "Ping" in members[0],
        f"GroupJoined.Data.Members={members}")


async def group_wait_deadline(ctx):
    """A member that keeps the group waiting is ignored after ~10s and the
    group proceeds; the member is flagged IgnoreGroupWait in the members list."""
    clients, gid, _ = await make_group(ctx, [2, 2])
    a, b = clients
    t_queue = await set_queue(ctx, clients)
    await a.ready(0)  # b stays silent

    t_up, cmd = await a.wait_command("Unpause", t_queue + 1.0, timeout=25)
    if not cmd:
        ctx.check("group-wait deadline", False, "no Unpause within 25s - group stuck")
        return
    dt = t_up - t_queue
    g, m_b = await member_of(a, b.user)
    ctx.check(
        "group-wait deadline",
        8.0 <= dt <= 16.0 and g.get("State") == "Playing" and m_b and m_b["IgnoreGroupWait"] is True,
        f"Unpause after {dt:.1f}s (expect ~10s), state={g.get('State')}, silent member={m_b}")


async def buffering_grace_absorb(ctx):
    """A buffering report followed by recovery within the grace period must
    not pause anyone else."""
    clients, gid, cmd = await start_playing(ctx, [2, 2])
    a, b = clients
    await asyncio.sleep(1)

    t0 = time.time()
    await b.buffering(cmd_pos_estimate(cmd))
    await asyncio.sleep(0.8)
    await b.ready(cmd_pos_estimate(cmd), playing=True)
    await asyncio.sleep(3.5)

    pauses = [d for _, d in a.count_since("SyncPlayCommand", t0) if d.get("Command") == "Pause"]
    ctx.check(
        "buffering grace absorbs short rebuffer",
        len(pauses) == 0,
        f"pauses to other members={len(pauses)} within {time.time() - t0:.1f}s of a 0.8s rebuffer")


async def buffering_grace_expiry(ctx):
    """A sustained buffering report pauses the group after the grace period;
    the member's Ready resumes it."""
    clients, gid, cmd = await start_playing(ctx, [2, 2])
    a, b = clients
    await asyncio.sleep(1)

    t0 = time.time()
    await b.buffering(cmd_pos_estimate(cmd))
    tp, dp = await a.wait_command("Pause", t0, timeout=8)
    if not dp:
        ctx.check("buffering grace expiry pauses group", False, "no Pause within 8s of Buffering")
        return
    dt = tp - t0
    await asyncio.sleep(0.3)
    await b.ready(dp.get("PositionTicks") or 0)
    tu, _ = await a.wait_command("Unpause", tp, timeout=8)
    ctx.check(
        "buffering grace expiry pauses group",
        1.5 <= dt <= 4.5 and tu is not None,
        f"Pause at +{dt:.2f}s after Buffering (expect ~2-3s); resumed on Ready={tu is not None}")
