# SyncPlay protocol specification

**Protocol versions covered:** 1 (Jellyfin 10.6+) and 2 (this specification).
**Status:** draft; describes the behaviour enforced by this repository's
conformance suite (§13).

SyncPlay keeps media playback synchronized across a group of sessions. The
server maintains authoritative group state and schedules playback commands
against its own clock; clients execute commands at the scheduled time and
continuously correct local playback drift. Synchronization quality is therefore
bounded by clock synchronization quality, which is why time sync is a
first-class part of the protocol.

This document is normative for client implementations: sections marked
**Client requirement** describe behaviour a conforming client MUST implement.

---

## 1. Architecture

Three planes, two transports:

| Plane | Transport | Direction | Purpose |
|---|---|---|---|
| Control | REST `POST /SyncPlay/*` | client → server | join/leave, playback requests, reports |
| Feedback | WebSocket `/socket` | server → client | commands, group updates, snapshots, beacons |
| Time | REST `GET /GetUtcTime` (v1), WebSocket `TimeSync` (v2, transport discovered per §2.1/§3.1) | round trip | clock offset + RTT measurement |

The feedback plane is **mandatory**: a client that can reach the REST API but
not the WebSocket can join groups but will never receive a command. Operators
must proxy WebSocket upgrades correctly (see `docs/operators.md`); clients
SHOULD detect a dead feedback plane (no message of
any kind within 120s) and surface it rather than fail silently.

### Conventions

- **Ticks**: positions and runtimes are in ticks; 1 tick = 100ns, 10,000,000
  ticks per second.
- **Timestamps**: ISO 8601 UTC strings in REST bodies and WebSocket JSON
  (`When`, `EmittedAt`, `LastUpdate`, …). The v2 `TimeSync` message uses
  integer milliseconds since the Unix epoch.
- **Authentication**: standard Jellyfin auth. SyncPlay endpoints additionally
  require SyncPlay access policies (`SyncPlayAccess` user policy); `Snapshot`,
  `Leave` and playback requests require being in a group (403 otherwise — see
  §10 for the required client reaction).

## 2. Version negotiation

- A client requests protocol version 2 by sending `"ProtocolVersion": 2` in the
  body of `POST /SyncPlay/New` or `POST /SyncPlay/Join`. Omitted = version 1.
- The server states its own protocol version in `GroupInfoDto.ProtocolVersion`
  (present in the `GroupJoined` update and `/SyncPlay/List`). Servers without
  the field are version 1.
- Version is a property of the **member**, not the group: v1 and v2 members
  coexist in one group. v2-only message types (`StateSnapshot`,
  `PositionBeacon`) are never sent to v1 members. All other messages are shared;
  v2 adds only fields (`StateVersion`), which v1 clients ignore.
- **Client requirement:** send `ProtocolVersion: 2` only after confirming
  server support, or unconditionally (unknown JSON fields are ignored by older
  servers) while treating the absence of `GroupInfoDto.ProtocolVersion` as v1.

### 2.1 Capability probe: `POST /SyncPlay/Hello`

One round trip answers "does this server speak v2, and where does time sync
live?" — and registers the caller's version at the same time:

- Request body: `{"ProtocolVersion": <int>}`. **The body is load-bearing**: the
  requested version is registered for the calling device, superseding any
  earlier registration — and an absent body registers version **1**, silently
  downgrading the device.
- Response: `{"ProtocolVersion": <highest supported>, "PluginVersion":
  "<implementation version>", "TimeSync": {"WebSocketPath":
  "/SyncPlay/TimeSync"}}`. A present `TimeSync.WebSocketPath` names a dedicated
  time-sync socket (§3.1); absent means the main `/socket` answers `TimeSync`.
- **`PluginVersion` is not ordered across server lines.** An implementation may
  build one source tree against several Jellyfin ABIs, stamping each build
  `<server major>.<server minor>.0.<build>` — the plugin does, so `12.0.0.4`
  and `10.11.0.4` are the same code and only the fourth component identifies
  the feature set. Compare that component, or nothing: a client that sorts the
  whole version string reads a Jellyfin 12 build as far newer than it is.
- **404 means the route does not exist.** That is how stock v1 servers answer —
  but also how a v2 server *without* the `Hello` binding answers (the
  integrated-fork implementation), so 404 means "no capability document", not
  "no v2": fall back to the §2 body negotiation and `GroupInfoDto` detection,
  and do not use a dedicated time-sync socket.

`Hello` and the `New`/`Join` bodies write the same per-device version record;
the most recent write wins, downgrades included. This makes `Hello` the
out-of-group (re)negotiation path: a device can switch itself to v1
(`{"ProtocolVersion": 1}`) or back before its next join, without joining.

**Client requirement:** probe `Hello` once per server connection before using
any v2 transport, and always send the version you intend to speak.

## 3. Time synchronization

Clients convert between local and server time using an offset estimated from
NTP-style exchanges:

```
offset = ((t1 - t0) + (t2 - t3)) / 2        rtt = (t3 - t0) - (t2 - t1)
t0 = client transmit    t1 = server receive
t2 = server transmit    t3 = client receive
```

Sources:

- **v1 (HTTP):** `GET /GetUtcTime` returns `{RequestReceptionTime,
  ResponseTransmissionTime}` (t1, t2).
- **v2 (WebSocket):** send `{"MessageType": "TimeSync", "Data": <t0 unix ms>}`;
  the server replies `{"MessageType": "TimeSync", "Data": {"T0": <echo>, "T1":
  <receive ms>, "T2": <transmit ms>}}`. Match responses to requests by the T0
  echo.

### 3.1 Where the v2 exchange runs

Two bindings exist; discover which one the server offers via `Hello` (§2.1):

- **Dedicated socket** (`TimeSync.WebSocketPath` present — the plugin
  implementation): connect a separate authenticated WebSocket to that path.
  `TimeSync` is the only message type it speaks; a synchronous send→recv round
  trip per measurement is sufficient, and stamping t3 at `recv()` keeps
  notification buses out of the measurement path. This socket carries no group
  feedback, and its traffic does **not** keep the main `/socket` alive — the
  main socket still needs its own `KeepAlive`s (§5.5).
- **Main socket** (no `WebSocketPath` — the integrated implementation): the
  `/socket` feedback connection answers `TimeSync` directly, and the exchange
  doubles as a keep-alive (§5.5).

**Never probe by sending `TimeSync` blind on the main socket.** Current stock
servers tear down the entire WebSocket on any message type they cannot parse
(an error-path bug in the connection handler), taking the feedback plane with
it. Discovery through `Hello` exists precisely so the shared socket is never
put at risk.

**Client requirement:**

- Keep a sliding window of at least 8 measurements and use the one with the
  smallest RTT (delay-based filtering); do not average raw offsets.
- Measure greedily on group join (≥3 exchanges at ~1s), then at least once per
  60s for the lifetime of the group membership.
- v2 clients SHOULD prefer the WebSocket exchange: HTTP measurements share the
  connection with media segment downloads under HTTP/2, and one-directional
  queueing produces a systematic offset error of ~half the queueing delay —
  routinely hundreds of ms on loaded WAN links. Fall back to HTTP when the
  socket is unavailable.
- After each accepted measurement, report the ping: `POST /SyncPlay/Ping`
  `{"Ping": <rtt/2 ms>}`. The server schedules group starts using reported
  pings (§7) and scales per-member tolerances with them (§8); a client that
  never reports is treated as having 500ms ping.

## 4. Control plane (REST)

All endpoints are `POST /SyncPlay/<name>` unless noted. Playback requests are
processed through the group's state machine — the same request can have
different effects depending on the group state (§7).

| Endpoint | Body | Notes |
|---|---|---|
| `New` | `{GroupName, ProtocolVersion?}` | returns `GroupInfoDto`; leaves the current group first if any |
| `Join` | `{GroupId, ProtocolVersion?}` | idempotent for the same group (re-join re-attaches the session, §9) |
| `Leave` | — | explicit exit; immediate removal |
| `List` (GET) | — | `GroupInfoDto[]` of accessible groups |
| `/SyncPlay/{id}` (GET) | — | single `GroupInfoDto` |
| `Snapshot` | — | v2: asks the server to push a `StateSnapshot` over the WebSocket |
| `SetNewQueue` | `{PlayingQueue: [ItemId], PlayingItemPosition, StartPositionTicks}` | replaces the queue, group enters Waiting |
| `SetPlaylistItem` | `{PlaylistItemId}` | switch playing item |
| `Queue` | `{ItemIds, Mode: Queue\|QueueNext}` | append items |
| `RemoveFromPlaylist` | `{PlaylistItemIds, ClearPlaylist, ClearPlayingItem}` | |
| `MovePlaylistItem` | `{PlaylistItemId, NewIndex}` | |
| `NextItem` / `PreviousItem` | `{PlaylistItemId}` (the item the client believes is playing; stale requests are ignored) | |
| `Unpause` / `Pause` / `Stop` | — | |
| `Seek` | `{PositionTicks}` | group enters Waiting |
| `Buffering` | `{When, PositionTicks, IsPlaying, PlaylistItemId}` | "I cannot keep up" report |
| `Ready` | `{When, PositionTicks, IsPlaying, PlaylistItemId}` | "I can play from here" report |
| `SetIgnoreWait` | `{IgnoreWait}` | opt out of being waited on (spectator) |
| `SetRepeatMode` | `{Mode: RepeatOne\|RepeatAll\|RepeatNone}` | |
| `SetShuffleMode` | `{Mode: Sorted\|Shuffle}` | |
| `Ping` | `{Ping}` (ms) | see §3 |

`When` in `Buffering`/`Ready` is the client's estimate of **server time** at
the moment the report was generated; the server uses `now - When` to
extrapolate the client's position, ignoring the delta when it exceeds 2000ms
(a client that is not time-syncing properly loses extrapolation, not
membership).

## 5. Feedback plane (WebSocket)

Envelope: `{"MessageType": <type>, "Data": <payload>, "MessageId": <guid>}`.

### 5.1 `SyncPlayCommand`

```json
{
  "GroupId": "...", "PlaylistItemId": "...",
  "Command": "Unpause" | "Pause" | "Seek" | "Stop",
  "When": "<ISO8601 server time to execute at>",
  "PositionTicks": 1234, "EmittedAt": "<ISO8601>",
  "StateVersion": 42
}
```

**Client requirement:** execute the command at local time
`When - offset`. If that instant is already past for an `Unpause`, start
immediately at the extrapolated position `PositionTicks + (serverNow - When)`.
Discard commands whose `EmittedAt` predates the client's group join, and
commands whose `PlaylistItemId` does not match the locally playing item
(except `Stop`).

### 5.2 `SyncPlayGroupUpdate`

`{"GroupId", "Type", "Data", "StateVersion"}`. Types and payloads:

| Type | Data | Audience |
|---|---|---|
| `GroupJoined` | `GroupInfoDto` | joiner |
| `UserJoined` / `UserLeft` | username string | other members |
| `GroupLeft` | group id string | leaver |
| `StateUpdate` | `{State, Reason}` | all |
| `PlayQueue` | `PlayQueueUpdate` (§5.3) | all or one |
| `NotInGroup`, `GroupDoesNotExist`, `LibraryAccessDenied` | string | requester (errors) |
| `StateSnapshot` (v2) | `GroupSnapshotDto` (§5.4) | one v2 member |
| `PositionBeacon` (v2) | `{PlaylistItemId, PositionTicks, When}` | v2 members |

`GroupInfoDto`: `{GroupId, GroupName, State, Participants: [username],
LastUpdatedAt, Members: [{UserName, IsBuffering, IgnoreGroupWait, Ping,
IsConnected}], ProtocolVersion}`.

### 5.3 `PlayQueueUpdate`

`{Reason, LastUpdate, Playlist: [{ItemId, PlaylistItemId}], PlayingItemIndex,
StartPositionTicks, IsPlaying, ShuffleMode, RepeatMode}`.

**Client requirement:** ignore an update whose `LastUpdate` is not newer than
the last applied one (updates can arrive more than once, e.g. inside a
snapshot).

### 5.4 `GroupSnapshotDto` (v2)

`{GroupName, State, PlayQueue: PlayQueueUpdate, PositionTicks, When,
IsPlaying, Members}` — the complete group state at server time `When`.

**Client requirement:** applying a snapshot must be equivalent to having
received: a `GroupJoined` (group info), a `PlayQueue` update, and a synthetic
command (`Unpause` if `IsPlaying`, `Stop` if `State == "Idle"`, else `Pause`)
at `When`/`PositionTicks`. Snapshot application MUST be idempotent.

A snapshot is not only a join artefact. The server also pushes one unsolicited,
mid-group and mid-playback, to a member it has stopped waiting for (§7.2), so a
client must accept one in any state — including while it believes itself in
sync.

### 5.5 Keep-alives and liveness

The server sends `ForceKeepAlive` with a timeout value on connect and
periodically; clients MUST send `{"MessageType": "KeepAlive"}` at no more than
half that interval (the server answers with `KeepAlive`). A socket that misses
keep-alives is aborted by the server (~60s), which ends the session when it was
the last socket — see §9 for what that means for group membership. v2 clients
using periodic `TimeSync` exchanges on the **main-socket binding** (§3.1)
thereby also satisfy keep-alive; dedicated-socket traffic does not count.

## 6. State versioning (v2)

Every group mutation — state transition, queue change, membership change,
settings change — increments the group's `StateVersion`. Every
`SyncPlayCommand` and `SyncPlayGroupUpdate` envelope carries the version
current at send time.

Properties: per-group, non-decreasing over time. **Gaps between consecutive
received messages are normal** (not every mutation broadcasts to every
member); version equality between two messages is normal (one mutation can
emit several messages).

**Client requirement (v2):**

- Track the highest version seen. Treat a message with a *lower* version than
  the highest seen as stale and do not regress state because of it.
- `PositionBeacon` never mutates the group, so a beacon carrying a version
  **greater** than the highest seen proves a missed update: request a snapshot
  (`POST /SyncPlay/Snapshot`), rate-limited (≥5s between requests).
- Reset tracking when leaving a group.

## 7. Group state machine

States: `Idle`, `Waiting`, `Playing`, `Paused`. `Waiting` is the barrier
state: entered on queue changes, seeks, joins during playback (v1 joiners —
v2 joiners hot-join instead, §7.1), and buffering; left when no member is
still expected (`IsBuffering && !IgnoreGroupWait`).

- **Ready gating.** After loading/seeking, a client reports `Ready` with its
  actual position and play state. The server compares the (extrapolated)
  reported position against the group position; within tolerance (§8) the
  member is marked ready, otherwise the server responds with a private `Seek`
  command and keeps the member in buffering.
- **Start scheduling.** When the last awaited member reports ready and
  playback should resume, the server broadcasts `Unpause` with
  `When = now + max(2 × highestPing, 500ms)` so all members start
  simultaneously despite differing latency.
- **Buffering.** A `Buffering` report while the group is `Playing` is held for
  a **2s grace period**; if the member's `Ready` arrives within it, nobody
  else is interrupted. Otherwise the group is paused (enters `Waiting`) and
  resumes when the member recovers.
- **Group-wait deadline.** A member that keeps the group `Waiting` for more
  than **10s** without reporting is flagged (`IgnoreGroupWait` +
  internal timeout mark) and the group proceeds without it. The flag clears
  automatically the next time one of the member's reports is processed —
  chronically slow members become spectators, but reintegrate by reporting.
  A **v2** member is *rendezvoused* at the deadline instead (§7.2): the group
  proceeds exactly as above, but the member is given the means to catch up
  rather than left where it stood. v1 members are flagged and abandoned as
  described.
- **Spectators.** `SetIgnoreWait {IgnoreWait: true}` opts a member out of
  being waited on; it still receives all commands.

### 7.1 Hot join (v2)

A v2 member joining a group that is `Playing` does not drag it into `Waiting`
(servers gate this behind configuration — the plugin's `HotJoin`, default on):

- The joiner is admitted flagged as buffering and not-waited-on; the group
  stays `Playing`. Existing members see `UserJoined` and the membership
  update, and nothing else — no `Pause`, no `Unpause`, no state change.
- The server pushes the joiner a `StateSnapshot` (§5.4) carrying the live
  position; the joiner loads it and reports `Buffering`/`Ready` as usual. Its
  `Buffering` reports are absorbed without group effect.
- Its `Ready` is answered with a **private scheduled `Unpause`**:
  `When = now + max(2 × member ping, 500ms)`, `PositionTicks` extrapolated to
  `When` — under the §5.1 execution rules the joiner starts exactly where the
  group will be at that instant. Nothing is sent to anyone else.
- If the group leaves `Playing` before the joiner reports ready, the hot join
  is abandoned and the ordinary rules of §7 apply from the new state.

v1 joiners keep the classic barrier: the group enters `Waiting` as above.

Nothing in those three steps — snapshot, absorbed `Buffering`, private
scheduled `Unpause` — is specific to joining. They are equally how a server
recovers a member it has given up waiting for (§7.2).

**Client requirement (v2):** no new message handling is needed — a client
correctly implementing §5.1, §5.4 and §7 hot-joins unmodified. For a tight
arrival a client SHOULD seek to the command's exact `PositionTicks` when
*arming* the scheduled `Unpause` rather than starting and correcting
afterwards (§10): the private start is the joiner's only synchronization
point, and the same arm-time alignment equally tightens ordinary group
restarts after a barrier.

### 7.2 Rendezvous (v2)

Some members cannot be corrected into position. A transcoding client asked to
seek lands on the segment boundary its transport chose rather than the position
the group asked for, reports back still out of tolerance, and the server seeks it again.
Measured on a real deployment: four rounds, ~13s of the whole group held in
`Waiting`, and the member played on permanently adrift anyway.

Such a member is not late — its transport cannot express the position being
asked for — which puts it in exactly the position of one that has just walked
in. The server therefore hands it to the hot-join path of §7.1, a
**rendezvous**: the group stops waiting and carries on, the member is pushed a
`StateSnapshot` to reload from, its `Buffering` reports are absorbed, and its
next `Ready` is answered with the private scheduled `Unpause` at the live
position. Other members see the membership update and nothing else. Completing
the rendezvous clears the flags, so the member is waited on again from its next
report — the reintegration rule of §7, reached by reporting as usual.

Rendezvous is v2-only and gated on the same configuration as hot join (the
plugin's `HotJoin`): a v1 member cannot be told about snapshots or private
starts, so §7 abandonment remains all there is to do for it.

Two things trigger it:

- **The group-wait deadline** (§7) — the trigger that fires for the member this
  exists for. A client whose reload cycle is 6-7s answers a group `Seek` with a
  single correction, the 10s deadline arrives, and its next report lands after
  the group has already left `Waiting`.
- **Corrections that are not converging**, while the group is still `Waiting`.
  The first correction always gets its chance: most members are simply late and
  one seek fixes them. After that the server rendezvouses when a correction
  fails to close the gap by **250ms**, or on the **third** attempt, whichever
  comes first. Improvement is absolute — 3s behind becoming 3s ahead is the
  same gap, overshot — which is how a livelock would otherwise hide. The
  counters reset whenever the member stops buffering.

If the rendezvoused member was the only one the group was waiting on, the group
is released as though it had reported ready.

**Client requirement (v2):** none beyond §7.1 — a rendezvous *is* a hot join
and arrives as one. A client that serves the reload by restarting its stream
SHOULD aim that load ahead of the group by the load's own cost (§10), or it
lands exactly as far behind as the reload took and the rendezvous buys it
nothing.

## 8. Position tolerance

The maximum position error accepted from a member before correction is
per-member: `clamp(2 × ping, 500ms, 2000ms)`. Members that report no ping
(500ms default) get 1000ms. This prevents the correction loop failure mode
where a high-latency member is re-seeked on every report and the group never
leaves `Waiting`.

## 9. Membership lifecycle and reconnect contract

Member ≠ session transport:

- **Explicit leave** (`POST /SyncPlay/Leave`): immediate removal, `UserLeft`
  broadcast.
- **Session end** (socket lost, session expired): the member is marked
  **disconnected** for a **90s grace window**. During it: the group does not
  wait on the member, no messages are addressed to it, and other members see
  `IsConnected: false` in `Members`. No `UserLeft` is sent.
- **Reconnect** during the window — any of: a new WebSocket for the session, a
  playback request over REST, or an explicit re-`Join` — re-attaches the
  member and the server pushes the full state (v2: one `StateSnapshot`; v1:
  `GroupJoined` + `PlayQueue` + a state-appropriate command).
- **Expiry**: members disconnected longer than the window are removed
  (`UserLeft` broadcast).

**Client requirement:** on any `NotInGroup` update or 403 response to a
SyncPlay request while believing itself in a group, attempt one automatic
re-`Join` of the last known group (rate-limited, ≥30s between attempts) before
surfacing an error. On (re)connecting a WebSocket while in a group, expect and
apply the pushed state (do not treat it as spurious).

## 10. Playback correction (client)

**Client requirement** while the last command is `Unpause` and the player is
not buffering: estimate the server position
`cmd.PositionTicks + (localNow + offset - cmd.When)` (v2: refreshed by every
`PositionBeacon`, §11) and correct the local position by a ladder:

1. `|diff| < 60ms` — do nothing (dead zone).
2. `60ms ≤ |diff| < 1500ms` — playback-rate correction, rate change capped at
   **±5%**, stretching the correction window as needed. Larger rate changes
   are audible and historically caused this mechanism to be disabled.
3. `|diff| ≥ 1500ms` (and ≥400ms where rate control is unavailable) — a single
   seek to the estimated position, then report `Buffering`/`Ready` around it.

Clients whose runtime throttles timers in the background (browsers) MUST
re-check drift immediately when returning to the foreground.

**Client requirement** where a correction or a scheduled start is served by
*reloading* the stream rather than seeking it — a transcoded stream cannot seek
accurately, so a reload is the only way to reach a position: aim the load ahead
of the target by the load's own cost. The position goes stale while the server
negotiates and starts encoding, so a load aimed at where the group is *now*
lands one load-duration behind it. That cost cannot be predicted — whether the
server transcodes is settled by the negotiation inside the load — so measure
it. The previous load's duration is a good enough stand-in for the next one's
and converges after a single item: smooth it **50/50**, ignore it below
**250ms**, discard it above **15s** (that is a dialog or a stall, not a load),
and use zero while the group is paused, where there is no moving target to aim
at. Err high: a member that loads early is held by the ready flow until the
group's `Unpause`, while one that loads late is the failure being avoided.

## 11. Position beacons (v2)

While a group is `Playing`, the server broadcasts a `PositionBeacon` to v2
members every **5s** (and immediately after entering `Playing`):
`{PlaylistItemId, PositionTicks, When}` with the envelope `StateVersion`.

**Client requirement (v2):** if the beacon's `PlaylistItemId` matches the
playing item, adopt `PositionTicks @ When` as the new drift reference (§10).
Apply the version-gap rule of §6. Beacons are advisory position data — they
never change play state.

## 12. Constants

| Constant | Value | Where |
|---|---|---|
| Default member ping | 500ms | server |
| Report extrapolation limit (`TimeSyncOffset`) | 2000ms | server |
| Position tolerance | clamp(2×ping, 500ms, 2000ms) | server |
| Unpause scheduling delay | max(2 × highest ping, 500ms) | server |
| Hot-join private start lead | max(2 × member ping, 500ms) | server |
| Buffering grace period | 2s | server |
| Group-wait deadline | 10s | server |
| Correction progress threshold (§7.2) | 250ms | server |
| Corrections before rendezvous (§7.2) | 3 | server |
| Disconnect grace window | 90s | server |
| Position beacon interval | 5s | server |
| Sweep resolution | 1s | server |
| Time sync window / cadence | best-of-8, 60s (greedy 1s ×3 on join) | client |
| Correction dead zone / rate cap / seek threshold | 60ms / ±5% / 1500ms | client |
| Scheduled-start arm-time alignment band (§7.1) | 100ms | client |
| Load-ahead allowance (§10) | previous load, smoothed 50/50, 250ms floor, 15s ceiling | client |
| Auto-rejoin rate limit | 30s | client |
| Snapshot request rate limit | 5s | client |

## 13. Conformance

This repository is the conformance kit: fake clients driving a real server,
with fault injection (zombie sockets, clean drops, delayed readies, biased
clocks). The scenario table in the README maps each scenario to the section
it verifies. Server changes touching SyncPlay should pass the full suite;
client implementations can reuse the kit's Python reference client core.

Known conforming implementations:

| Implementation | Role | Protocol |
|---|---|---|
| `kontell/jellyfin-plugin-syncplayv2` on stock Jellyfin 10.11 / 12 | server | v1 + v2; `Hello`/dedicated socket; hot join from 10.11.0.2; rendezvous from 10.11.0.4 |
| `kontell/jellyfin` `integration/syncplay-phase1` | server | v1 + v2; main-socket bindings only (no `Hello`, no hot join, no rendezvous) |
| `kontell/plugin.video.kofin` ≥ 0.16.0 | client | v2; §10 load-ahead allowance from 0.19.0 |
| jellyfin-web (stock) | client | v1 |

Plugin versions above name the Jellyfin 10.11 build; the same code ships as
`12.0.0.N` for Jellyfin 12, since only the build component distinguishes
releases (§2.1).

Rendezvous (§7.2) has no scenario yet: the suite covers the deadline it fires
at, but only the v1 outcome of it.
