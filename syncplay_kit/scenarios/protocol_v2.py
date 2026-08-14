"""Protocol v2 scenarios: negotiation, versioning, snapshots, beacons,
WebSocket time sync, adaptive tolerance and the reconnect contract.

These verify the behaviours specified in docs/SYNCPLAY.md sections 2, 3, 6,
8, 9 and 11.
"""
import asyncio
import time

from ..client import TICKS, cmd_pos_estimate, parse_iso_ms
from .common import make_group, start_playing, member_of


async def v2_negotiation(ctx):
    """Joining with ProtocolVersion 2 gets a GroupInfoDto stating the server's
    protocol version, and envelopes carry a StateVersion."""
    clients, gid, ga = await make_group(ctx, [2])
    ctx.check(
        "v2 negotiation",
        ga.get("Data", {}).get("ProtocolVersion", 1) >= 2 and isinstance(ga.get("StateVersion"), int),
        f"GroupJoined.Data.ProtocolVersion={ga.get('Data', {}).get('ProtocolVersion')}, "
        f"envelope StateVersion={ga.get('StateVersion')}")


async def state_version(ctx):
    """StateVersion is per group and non-decreasing across the messages a
    member receives, and grows across mutations."""
    clients, gid, cmd = await start_playing(ctx, [2, 2])
    a = clients[0]
    # Cause a few more mutations.
    await a.post("/SyncPlay/Seek", {"PositionTicks": 30 * TICKS})
    await asyncio.sleep(0.3)
    await a.ready(30 * TICKS)
    await clients[1].ready(30 * TICKS)
    await asyncio.sleep(1)

    versions = [d.get("StateVersion") for _, mt, d in a.msgs
                if mt in ("SyncPlayGroupUpdate", "SyncPlayCommand") and isinstance(d, dict)
                and d.get("GroupId") == gid and d.get("StateVersion") is not None]
    non_decreasing = all(x <= y for x, y in zip(versions, versions[1:]))
    ctx.check(
        "StateVersion on envelopes",
        len(versions) >= 4 and non_decreasing and versions[-1] > versions[0],
        f"{len(versions)} stamped messages, {versions[0] if versions else '-'}..{versions[-1] if versions else '-'}, "
        f"non-decreasing={non_decreasing}")


async def ws_timesync(ctx):
    """The TimeSync WebSocket exchange echoes T0 and returns sane T1/T2.

    Transport discovery first: a plugin-binding server advertises a dedicated
    time-sync socket via POST /SyncPlay/Hello (it cannot answer TimeSync on
    the shared /socket); an integrated v2 server answers on /socket itself."""
    c = await ctx.new_client(0)
    where = "/socket"
    try:
        path = await c.timesync_transport()
        if path:
            where = f"dedicated {path}"
            offset, rtt, d = await c.timesync_ws_dedicated(path)
        else:
            offset, rtt, d = await c.timesync_ws()
    except ConnectionError as e:
        ctx.check("WebSocket TimeSync", False,
                  f"server closed the socket on the TimeSync probe ({e}) - it rejects unknown "
                  "WS message types (stock Jellyfin <=10.11); no protocol v2 time sync")
        return
    except TimeoutError:
        ctx.check("WebSocket TimeSync", False, f"no TimeSync response within 5s ({where})")
        return
    ctx.check(
        "WebSocket TimeSync",
        d["T1"] <= d["T2"] and 0 <= rtt < 2000,
        f"rtt={rtt:.1f}ms offset={offset:.1f}ms via {where} (offset vs local clock; large "
        f"values are fine on remote servers, negative rtt is not)")


async def position_beacons(ctx):
    """While playing, v2 members receive a PositionBeacon every ~5s with an
    accurate position; v1 members receive no v2-only message types."""
    clients, gid, cmd = await start_playing(ctx, [2, 2, None])  # third member is v1
    a, b, c_v1 = clients

    t0 = time.time()
    await asyncio.sleep(12.5)
    beacons_a = a.count_since("SyncPlayGroupUpdate", t0, sub="PositionBeacon")
    beacons_b = b.count_since("SyncPlayGroupUpdate", t0, sub="PositionBeacon")
    beacons_c = c_v1.count_since("SyncPlayGroupUpdate", 0, sub="PositionBeacon")
    snapshots_c = c_v1.count_since("SyncPlayGroupUpdate", 0, sub="StateSnapshot")

    gap_ok, pos_ok = False, False
    if len(beacons_a) >= 2:
        gaps = [t2 - t1 for (t1, _), (t2, _) in zip(beacons_a, beacons_a[1:])]
        gap_ok = all(3.5 <= g <= 6.5 for g in gaps)
        _, d_last = beacons_a[-1]
        expected = cmd_pos_estimate(cmd, at=parse_iso_ms(d_last["Data"]["When"]))
        pos_ok = abs(d_last["Data"]["PositionTicks"] - expected) < 1.5 * TICKS

    ctx.check(
        "position beacons while playing",
        len(beacons_a) >= 2 and len(beacons_b) >= 2 and gap_ok and pos_ok,
        f"{len(beacons_a)}/{len(beacons_b)} beacons in 12.5s, spacing ok={gap_ok}, position ok={pos_ok}")
    ctx.check(
        "no v2 messages to v1 members",
        len(beacons_c) == 0 and len(snapshots_c) == 0,
        f"v1 member received beacons={len(beacons_c)} snapshots={len(snapshots_c)} (expect 0/0)")


async def snapshot_on_demand(ctx):
    """POST /SyncPlay/Snapshot delivers a full StateSnapshot to the requester."""
    clients, gid, cmd = await start_playing(ctx, [2, 2])
    a, b = clients
    t0 = time.time()
    await b.post("/SyncPlay/Snapshot")
    _, snap = await b.wait_for("SyncPlayGroupUpdate", "StateSnapshot", 5, after=t0)
    ok = bool(snap) and snap["Data"].get("State") == "Playing" and snap["Data"].get("IsPlaying") is True \
        and isinstance(snap["Data"].get("PlayQueue"), dict) and len(snap["Data"].get("Members", [])) == 2
    ctx.check(
        "snapshot on demand",
        ok,
        f"State={snap and snap['Data'].get('State')}, members={snap and len(snap['Data'].get('Members', []))}, "
        f"queue present={snap and bool(snap['Data'].get('PlayQueue'))}")


async def resync_per_version(ctx):
    """A new socket on a member session gets one StateSnapshot for v2 members
    and the GroupJoined+PlayQueue+command triple for v1 members."""
    clients, gid, cmd = await start_playing(ctx, [2, None])
    v2c, v1c = clients
    t0 = time.time()
    await v2c.ws2_connect()
    await v1c.ws2_connect()

    _, v2_snap = await v2c.wait_for("SyncPlayGroupUpdate", "StateSnapshot", 5, after=t0, sink=v2c.ws2_msgs)
    _, v2_triple = await v2c.wait_for("SyncPlayGroupUpdate", "GroupJoined", 1, after=t0, sink=v2c.ws2_msgs)
    _, v1_gj = await v1c.wait_for("SyncPlayGroupUpdate", "GroupJoined", 5, after=t0, sink=v1c.ws2_msgs)
    _, v1_q = await v1c.wait_for("SyncPlayGroupUpdate", "PlayQueue", 5, after=t0, sink=v1c.ws2_msgs)
    _, v1_cmd = await v1c.wait_for("SyncPlayCommand", None, 5, after=t0, sink=v1c.ws2_msgs)
    _, v1_snap = await v1c.wait_for("SyncPlayGroupUpdate", "StateSnapshot", 1, after=t0, sink=v1c.ws2_msgs)

    ctx.check(
        "resync matches protocol version",
        bool(v2_snap) and not v2_triple and bool(v1_gj and v1_q and v1_cmd) and not v1_snap,
        f"v2: snapshot={bool(v2_snap)} triple={bool(v2_triple)}; "
        f"v1: triple={bool(v1_gj and v1_q and v1_cmd)} snapshot={bool(v1_snap)}")


async def adaptive_tolerance(ctx):
    """The position tolerance scales with reported ping: a 1.5s offset is
    accepted at ping 1000 (tolerance 2000ms) but corrected at ping 50
    (tolerance 500ms)."""
    clients, gid, cmd = await start_playing(ctx, [2, 2])
    a, b = clients
    await asyncio.sleep(1)

    # High ping: no correction expected.
    await b.post("/SyncPlay/Ping", {"Ping": 1000})
    t0 = time.time()
    await b.buffering(cmd_pos_estimate(cmd), playing=True)
    tp, dp = await a.wait_command("Pause", t0, timeout=8)
    assert dp, "group did not pause on sustained buffering"
    t1 = time.time()
    await b.ready((dp.get("PositionTicks") or 0) + int(1.5 * TICKS))
    seek_hi = await b.wait_command("Seek", t1, timeout=4)
    tu, du = await a.wait_command("Unpause", t1, timeout=10)
    high_ok = seek_hi[0] is None and tu is not None
    if du:
        cmd = du

    # Low ping: correction expected.
    await b.post("/SyncPlay/Ping", {"Ping": 50})
    await asyncio.sleep(1)
    t2 = time.time()
    await b.buffering(cmd_pos_estimate(cmd), playing=True)
    tp2, dp2 = await a.wait_command("Pause", t2, timeout=8)
    assert dp2, "group did not pause on second sustained buffering"
    t3 = time.time()
    await b.ready((dp2.get("PositionTicks") or 0) + int(1.5 * TICKS))
    seek_lo = await b.wait_command("Seek", t3, timeout=4)
    await b.ready(dp2.get("PositionTicks") or 0)
    tu2, _ = await a.wait_command("Unpause", t3, timeout=10)

    ctx.check(
        "adaptive position tolerance",
        high_ok and seek_lo[0] is not None and tu2 is not None,
        f"1.5s offset: ping=1000 corrected={seek_hi[0] is not None} resumed={tu is not None}; "
        f"ping=50 corrected={seek_lo[0] is not None}; recovered={tu2 is not None}")


async def reconnect_grace(ctx):
    """[slow] A dead session does not kick the member: it is flagged
    IsConnected=false, no UserLeft is sent, and a reconnect within the grace
    window resumes with a pushed StateSnapshot."""
    clients, gid, cmd = await start_playing(ctx, [2, 2])
    a, b = clients

    b.keepalive_enabled = False
    t0 = time.time()
    ctx.log("scenario", "zombie_start", member=b.user, expect="disconnect flag in ~60s")

    t_disc = None
    deadline = time.time() + 100
    while time.time() < deadline and t_disc is None:
        g, m = await member_of(a, b.user)
        if m and m.get("IsConnected") is False:
            t_disc = time.time()
            break
        await asyncio.sleep(2)

    userlefts = a.count_since("SyncPlayGroupUpdate", t0, sub="UserLeft")
    disc_ok = t_disc is not None and len(userlefts) == 0

    # Reconnect within the grace window.
    b.keepalive_enabled = True
    t_rc = time.time()
    await b.ws_connect()
    _, rc_snap = await b.wait_for("SyncPlayGroupUpdate", "StateSnapshot", 6, after=t_rc)
    await asyncio.sleep(2)
    g2, m2 = await member_of(a, b.user)
    userlefts2 = a.count_since("SyncPlayGroupUpdate", t0, sub="UserLeft")

    ctx.check(
        "reconnect grace window",
        disc_ok and bool(rc_snap) and m2 and m2.get("IsConnected") is True and len(userlefts2) == 0,
        f"disconnected@+{t_disc and format(t_disc - t0, '.1f')}s, kicked={len(userlefts2) > 0}, "
        f"snapshot on reconnect={bool(rc_snap)}, reconnected={m2 and m2.get('IsConnected')}")


async def grace_expiry(ctx):
    """[slow] A member that never reconnects is removed ~(60s detection + 90s
    grace) after its socket dies, with a UserLeft broadcast."""
    clients, gid, cmd = await start_playing(ctx, [2, 2])
    a, b = clients

    b.keepalive_enabled = False
    t0 = time.time()
    ctx.log("scenario", "zombie_start", member=b.user, expect="UserLeft in ~150s")

    t_left, left = await a.wait_for("SyncPlayGroupUpdate", "UserLeft", timeout=200, after=t0)
    left_dt = (t_left - t0) if t_left else None
    g, m = await member_of(a, b.user)
    ctx.check(
        "grace expiry removes member",
        left is not None and 130 <= left_dt <= 180 and m is None,
        f"UserLeft +{left_dt and format(left_dt, '.1f')}s (expect ~150s), still a member={m is not None}")
