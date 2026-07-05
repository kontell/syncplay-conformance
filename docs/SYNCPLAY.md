# SyncPlay protocol specification

**Protocol versions covered:** 1 (Jellyfin 10.6+) and 2 (this specification).
**Status:** draft, describes the server behaviour implemented in this repository.

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
| Time | REST `GET /GetUtcTime` (v1), WebSocket `TimeSync` (v2) | round trip | clock offset + RTT measurement |

The feedback plane is **mandatory**: a client that can reach the REST API but
not the WebSocket can join groups but will never receive a command. Operators
must proxy WebSocket upgrades correctly (see `docs/operators.md` in the
conformance kit); clients SHOULD detect a dead feedback plane (no message of
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
  echo. A TimeSync exchange also counts as a keep-alive.

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

### 5.5 Keep-alives and liveness

The server sends `ForceKeepAlive` with a timeout value on connect and
periodically; clients MUST send `{"MessageType": "KeepAlive"}` at no more than
half that interval (the server answers with `KeepAlive`). A socket that misses
keep-alives is aborted by the server (~60s), which ends the session when it was
the last socket — see §9 for what that means for group membership. v2 clients
using periodic `TimeSync` exchanges thereby also satisfy keep-alive.

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
state: entered on queue changes, seeks, joins during playback, and buffering;
left when no member is still expected (`IsBuffering && !IgnoreGroupWait`).

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
- **Spectators.** `SetIgnoreWait {IgnoreWait: true}` opts a member out of
  being waited on; it still receives all commands.

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
| Buffering grace period | 2s | server |
| Group-wait deadline | 10s | server |
| Disconnect grace window | 90s | server |
| Position beacon interval | 5s | server |
| Sweep resolution | 1s | server |
| Time sync window / cadence | best-of-8, 60s (greedy 1s ×3 on join) | client |
| Correction dead zone / rate cap / seek threshold | 60ms / ±5% / 1500ms | client |
| Auto-rejoin rate limit | 30s | client |
| Snapshot request rate limit | 5s | client |

## 13. Conformance

A protocol conformance kit (fake clients driving a real server, with fault
injection: zombie sockets, clean drops, delayed readies, biased clocks) lives
in the `syncplay-conformance` repository. Server changes touching SyncPlay
should pass its full suite; client implementations can reuse its Python
reference client core.
