"""Shared flows used by conformance scenarios."""
import time


async def make_group(ctx, versions):
    """Create a group with one client per requested protocol version
    (None/1 = v1 member, 2 = v2 member). Returns (clients, gid, joined_data)."""
    clients = [await ctx.new_client(i) for i in range(len(versions))]
    a = clients[0]

    body = {"GroupName": "conformance"}
    if versions[0] and versions[0] >= 2:
        body["ProtocolVersion"] = versions[0]
    await a.post("/SyncPlay/New", body)
    t, ga = await a.wait_for("SyncPlayGroupUpdate", "GroupJoined", 5)
    assert ga, "no GroupJoined for group creator"
    gid = ga["GroupId"]

    for c, v in zip(clients[1:], versions[1:]):
        jb = {"GroupId": gid}
        if v and v >= 2:
            jb["ProtocolVersion"] = v
        await c.post("/SyncPlay/Join", jb)
        joined = await c.wait_for("SyncPlayGroupUpdate", "GroupJoined", 5)
        assert joined[1], f"no GroupJoined for {c.name}"

    return clients, gid, ga


async def set_queue(ctx, clients):
    """Set the test movie as the queue; stamps PlaylistItemId on all clients.
    Returns the time the queue was set."""
    a = clients[0]
    mark = time.time()
    await a.post("/SyncPlay/SetNewQueue", {
        "PlayingQueue": [ctx.movie_id], "PlayingItemPosition": 0, "StartPositionTicks": 0})
    _, qa = await a.wait_for("SyncPlayGroupUpdate", "PlayQueue", 5, after=mark)
    assert qa, "no PlayQueue update after SetNewQueue"
    pid = qa["Data"]["Playlist"][0]["PlaylistItemId"]
    for c in clients:
        c.playlist_item_id = pid
    return time.time()


async def start_playing(ctx, versions):
    """Group of the given versions, queue set, everyone ready.
    Returns (clients, gid, last_unpause_command)."""
    clients, gid, _ = await make_group(ctx, versions)
    t_queue = await set_queue(ctx, clients)
    for c in clients:
        await c.ready(0)
    t_up, cmd = await clients[0].wait_command("Unpause", t_queue, timeout=25)
    assert cmd, "group never started playing"
    return clients, gid, cmd


async def member_of(client, username):
    """(group, member) from /SyncPlay/List for the given username."""
    groups = await client.get_json("/SyncPlay/List")
    g = groups[0] if groups else {}
    return g, next((m for m in g.get("Members", []) if m.get("UserName") == username), None)
