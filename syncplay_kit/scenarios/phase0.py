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
    """A silent member loading a new item is rendezvoused after the load
    timeout; the group proceeds and the member gets a private start."""
    clients, gid, _ = await make_group(ctx, [2, 2])
    a, b = clients
    t_queue = await set_queue(ctx, clients)
    await a.ready(0)  # b stays silent

    t_up, cmd = await a.wait_command("Unpause", t_queue + 1.0,
                                     timeout=ctx.load_timeout + 8)
    if not cmd:
        ctx.check("load deadline", False,
                  f"no Unpause within {ctx.load_timeout + 8}s - group stuck")
        return
    dt = t_up - t_queue
    g, m_b = await member_of(a, b.user)
    ctx.check(
        "load deadline",
        ctx.load_timeout - 2 <= dt <= ctx.load_timeout + 6
        and g.get("State") == "Playing" and m_b
        and m_b.get("IgnoreGroupWait") is True,
        f"Unpause after {dt:.1f}s (expect ~{ctx.load_timeout}s), "
        f"state={g.get('State')}, ignored={m_b and m_b.get('IgnoreGroupWait')}")

    _, snap = await b.wait_for("SyncPlayGroupUpdate", "StateSnapshot", 2,
                               after=t_queue + 1.0)
    sd = (snap or {}).get("Data") or {}
    ctx.check("load deadline rendezvous", bool(snap) and sd.get("State") in ("Waiting", "Playing"),
              f"snapshot={bool(snap)}, state={sd.get('State')}")
    if snap:
        await b.ready(sd.get("PositionTicks") or 0)
        _, private = await b.wait_command("Unpause", t_up, timeout=6)
        ctx.check("load deadline private start", private is not None,
                  f"private Unpause={private is not None}")


async def stall_wait_deadline(ctx):
    """A member that stalls during playback is rendezvoused at the shorter
    stall timeout, after the buffering grace has paused the group."""
    clients, gid, cmd = await start_playing(ctx, [2, 2])
    a, b = clients
    t_stall = time.time()
    await b.buffering(cmd_pos_estimate(cmd))
    t_pause, pause = await a.wait_command("Pause", t_stall, timeout=8)
    if not pause:
        ctx.check("stall deadline", False, "no Pause after buffering grace")
        return

    t_up, up = await a.wait_command("Unpause", t_pause,
                                    timeout=ctx.stall_timeout + 8)
    if not up:
        ctx.check("stall deadline", False,
                  f"no Unpause within {ctx.stall_timeout + 8}s - group stuck")
        return
    dt = t_up - t_pause
    g, m_b = await member_of(a, b.user)
    ctx.check("stall deadline",
              ctx.stall_timeout - 2 <= dt <= ctx.stall_timeout + 6
              and g.get("State") == "Playing" and m_b
              and m_b.get("IgnoreGroupWait") is True,
              f"Unpause after {dt:.1f}s (expect ~{ctx.stall_timeout}s), "
              f"state={g.get('State')}, ignored={m_b and m_b.get('IgnoreGroupWait')}")
    _, snap = await b.wait_for("SyncPlayGroupUpdate", "StateSnapshot", 2,
                               after=t_stall)
    sd = (snap or {}).get("Data") or {}
    ctx.check("stall deadline rendezvous", bool(snap) and sd.get("State") in ("Waiting", "Playing"),
              f"snapshot={bool(snap)}, state={sd.get('State')}")
    if snap:
        await b.ready(sd.get("PositionTicks") or 0)
        _, private = await b.wait_command("Unpause", t_up, timeout=6)
        ctx.check("stall deadline private start", private is not None,
                  f"private Unpause={private is not None}")


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
