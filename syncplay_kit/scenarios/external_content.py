"""External-content scenarios (SYNCPLAY.md §14).

The point of the family: the queue entry is NOT a library item, so nothing
here plays media anywhere — the server coordinates timing for content only
the members could resolve, and every group mechanism (barrier, scheduled
start, seeks, next-item) must work on it exactly as on a movie.
"""
import asyncio
import time

from ..client import TICKS
from .common import make_group

CAPABILITY = "ExternalContent"


def descriptor(key="ext-1", runtime_s=90 * 60, provider="conformance"):
    return {"Content": {
        "Provider": provider, "Key": key, "Name": f"External {key}",
        "RunTimeTicks": int(runtime_s * TICKS)}}


async def _capable_group(ctx, count):
    """A group whose members all declared the capability before joining."""
    clients, gid, ga = await make_group(ctx, [2] * count)
    for c in clients:
        doc = await c.hello(2, [CAPABILITY])
        assert doc is not None, "no Hello capability document"
    return clients, gid, ga


async def _set_descriptor_queue(ctx, clients, entries, expect=True, timeout=5):
    """SetNewQueueEx from clients[0]; returns the PlayQueue Data every client
    saw (or None per client when none arrived, which expect=False awaits)."""
    a = clients[0]
    mark = time.time()
    status = await a.set_new_queue_ex(entries)
    assert status in (200, 204), f"SetNewQueueEx answered {status}"
    seen = []
    for c in clients:
        _, q = await c.wait_for(
            "SyncPlayGroupUpdate", "PlayQueue",
            timeout if expect else 3, after=mark)
        seen.append(q["Data"] if q else None)
    if seen[0]:
        pid = seen[0]["Playlist"][seen[0]["PlayingItemIndex"]]["PlaylistItemId"]
        for c in clients:
            c.playlist_item_id = pid
    return seen


async def descriptor_queue_basic(ctx):
    """§14.1-14.3: a descriptor queue is delivered with Content to capability
    members, and the group plays it — barrier, ready gating, scheduled start —
    with no media on the server at all."""
    clients, gid, _ = await _capable_group(ctx, 2)
    a, b = clients

    doc = await a.hello(2, [CAPABILITY])
    advertises = CAPABILITY in (doc or {}).get("Capabilities", [])

    t_queue = time.time()
    seen = await _set_descriptor_queue(ctx, clients, [descriptor("basic-1")])
    entries = [(q or {}).get("Playlist", [{}])[0] for q in seen]
    content_ok = all(
        (e.get("Content") or {}).get("Provider") == "conformance"
        and (e.get("Content") or {}).get("Key") == "basic-1"
        for e in entries)
    sentinel_ok = all(e.get("ItemId") not in (None, ctx.movie_id) for e in entries)

    for c in clients:
        await c.ready(0)
    t_up, cmd = await a.wait_command("Unpause", t_queue, timeout=25)

    ctx.check(
        "descriptor queue plays",
        advertises and content_ok and sentinel_ok and cmd is not None,
        f"server advertises={advertises}, Content on both members={content_ok}, "
        f"sentinel ItemId={sentinel_ok}, scheduled start={'yes' if cmd else 'no'}")


async def descriptor_member_veto(ctx):
    """§14.4: with a capability-less member in the group, a descriptor queue
    is refused; the moment that member declares, the same request lands."""
    clients, gid, _ = await make_group(ctx, [2, 2])
    a, b = clients
    await a.hello(2, [CAPABILITY])
    await b.hello(2, [])  # explicitly no capability

    vetoed = await _set_descriptor_queue(ctx, clients, [descriptor("veto-1")], expect=False)
    veto_held = all(q is None for q in vetoed)

    await b.hello(2, [CAPABILITY])
    landed = await _set_descriptor_queue(ctx, clients, [descriptor("veto-2")])
    lands_after = all(q is not None for q in landed)

    ctx.check(
        "member without the capability vetoes",
        veto_held and lands_after,
        f"queue update while vetoed={'none' if veto_held else 'DELIVERED'}, "
        f"after declaring={'delivered' if lands_after else 'still refused'}")


async def descriptor_visibility(ctx):
    """§14.4: a descriptor group is invisible to a capability-less device,
    and its join is refused with LibraryAccessDenied."""
    clients, gid, _ = await _capable_group(ctx, 1)
    a = clients[0]
    await _set_descriptor_queue(ctx, clients, [descriptor("vis-1")])

    b = await ctx.new_client(1)
    await b.hello(2, [])
    groups = await b.get_json("/SyncPlay/List")
    listed = any(g.get("GroupId") == gid for g in (groups or []))

    mark = time.time()
    await b.post("/SyncPlay/Join", {"GroupId": gid, "ProtocolVersion": 2})
    _, denied = await b.wait_for(
        "SyncPlayGroupUpdate", "LibraryAccessDenied", 5, after=mark)
    _, joined = await b.wait_for("SyncPlayGroupUpdate", "GroupJoined", 2, after=mark)

    # And the same device becomes welcome the moment it declares.
    await b.hello(2, [CAPABILITY])
    await b.post("/SyncPlay/Join", {"GroupId": gid, "ProtocolVersion": 2})
    _, joined_after = await b.wait_for(
        "SyncPlayGroupUpdate", "GroupJoined", 5, after=mark)

    ctx.check(
        "descriptor group hidden without the capability",
        not listed and denied is not None and joined is None and joined_after is not None,
        f"listed={listed}, join denied={'yes' if denied else 'no'}, "
        f"joined anyway={'YES' if joined else 'no'}, joined after declaring="
        f"{'yes' if joined_after else 'no'}")


async def descriptor_no_clamp(ctx):
    """§14.2: a runtime-0 entry (live) is unbounded — a group seek far past
    zero keeps its position instead of clamping every report to 0."""
    clients, gid, _ = await _capable_group(ctx, 1)
    a = clients[0]
    t_queue = time.time()
    await _set_descriptor_queue(ctx, clients, [descriptor("live-1", runtime_s=0)])
    await a.ready(0)
    await a.wait_command("Unpause", t_queue, timeout=25)

    target = 90 * 60 * TICKS
    mark = time.time()
    await a.post("/SyncPlay/Seek", {"PositionTicks": target})
    _, seek = await a.wait_command("Seek", mark, timeout=10)
    kept = seek is not None and seek.get("PositionTicks") == target

    # The ready flow at the far position must not be "corrected" back to 0.
    await a.ready(target)
    _, correction = await a.wait_command("Seek", mark + 0.5, timeout=3)
    corrected_to_zero = correction is not None and (correction.get("PositionTicks") or 0) < TICKS

    ctx.check(
        "runtime 0 is unbounded",
        kept and not corrected_to_zero,
        f"seek broadcast PositionTicks={'kept' if kept else (seek or {}).get('PositionTicks')}, "
        f"ready at 90min {'corrected to ~0' if corrected_to_zero else 'accepted'}")


async def descriptor_mixed_queue(ctx):
    """§14.2-14.3: a library item and a descriptor share one queue — Content
    rides only the external entry, and NextItem advances between them."""
    clients, gid, _ = await _capable_group(ctx, 2)
    a, b = clients

    t_queue = time.time()
    seen = await _set_descriptor_queue(
        ctx, clients,
        [{"ItemId": ctx.movie_id}, descriptor("mixed-2", runtime_s=60 * 60)])
    q = seen[0] or {}
    playlist = q.get("Playlist", [])
    shape_ok = (
        len(playlist) == 2
        and playlist[0].get("ItemId") == ctx.movie_id
        and "Content" not in playlist[0]
        and (playlist[1].get("Content") or {}).get("Key") == "mixed-2")

    for c in clients:
        await c.ready(0)
    await a.wait_command("Unpause", t_queue, timeout=25)

    mark = time.time()
    await a.post("/SyncPlay/NextItem", {"PlaylistItemId": a.playlist_item_id})
    _, advanced = await a.wait_for("SyncPlayGroupUpdate", "PlayQueue", 10, after=mark)
    idx = (advanced or {}).get("Data", {}).get("PlayingItemIndex")

    ctx.check(
        "mixed queue advances onto the descriptor",
        shape_ok and idx == 1,
        f"shape ok={shape_ok}, PlayingItemIndex after NextItem={idx}")
