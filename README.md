# syncplay-conformance

A protocol conformance kit for Jellyfin SyncPlay: fake clients drive a **real
server** through the documented protocol (see `docs/SYNCPLAY.md` in the
jellyfin repository) and assert the behaviours that make watch parties work —
bounded group-waits, buffering grace, snapshots, position beacons, WebSocket
time sync, adaptive tolerances and the disconnect/reconnect contract.

It reproduces, deterministically and in minutes, the failure modes that used
to take a watch party and a flaky hotel Wi-Fi to hit.

The kit doubles as a **Python reference client core**
(`syncplay_kit/client.py`): time sync, ready gating, command tracking and
position estimation, reusable as the starting point for Python-based clients
(e.g. jellyfin-kodi).

## Requirements

```
python >= 3.11
pip install -r requirements.txt   # aiohttp, websockets
```

## Quick start

Against an existing server with two or three test users:

```bash
python -m syncplay_kit run \
    --base http://127.0.0.1:8096 \
    --user syncbot-a:sp-test --user syncbot-b:sp-test --user admin:admin-pw \
    --suite all
```

- 3 users unlock every scenario (one plays a protocol-v1 member); with 2 the
  v1-isolation scenario is skipped.
- Users need `SyncPlayAccess: CreateAndJoinGroups` and access to at least one
  movie (`--movie <itemId>` to pick one explicitly).
- `--suite fast` (~2-3 min) covers everything except the disconnect-lifecycle
  scenarios; `--suite slow` (~5 min) runs just those; `--suite all` runs both.
- `--scenario <name>` runs one scenario; `python -m syncplay_kit list` shows all.

Setting up a **fresh throwaway server** (completes the startup wizard, creates
a movie library and bot users):

```bash
python -m syncplay_kit bootstrap --base http://127.0.0.1:8097 \
    --admin admin:test-pw --media-dir /path/on/server/with/a/video
```

## Scenarios

| Scenario | Suite | Verifies (spec §) |
|---|---|---|
| `group_info_members` | fast | Members[] status list (§5.2) |
| `v2_negotiation` | fast | version negotiation (§2) |
| `ws_timesync` | fast | TimeSync exchange (§3) |
| `group_wait_deadline` | fast | 10s group-wait bound (§7) |
| `buffering_grace_absorb` | fast | 2s grace hides short rebuffers (§7) |
| `buffering_grace_expiry` | fast | sustained buffering pauses group (§7) |
| `state_version` | fast | per-group monotonic versions (§6) |
| `position_beacons` | fast | 5s beacons, v1 isolation (§11, §2) |
| `snapshot_on_demand` | fast | `POST /SyncPlay/Snapshot` (§5.4) |
| `resync_per_version` | fast | resync payload per protocol version (§9) |
| `adaptive_tolerance` | fast | ping-scaled tolerance (§8) |
| `hot_join` | fast | v2 joiner never pauses a Playing group (§7.1) |
| `reconnect_grace` | slow | disconnect ≠ kick; snapshot on reconnect (§9) |
| `grace_expiry` | slow | removal after the 90s window (§9) |

Scenarios are fully isolated: each uses fresh device ids (fresh server
sessions) and its own group, so leftover state from aborted runs cannot bleed
in.

Not yet covered: **rendezvous** (§7.2), where a v2 member the group has given
up waiting for is pushed a snapshot and given a private scheduled start.
`group_wait_deadline` fires the deadline it hangs off and still passes — the
group proceeds and the member is flagged either way — but asserts only the v1
outcome of it.

## Operator self-test

`tools/doctor.py` checks a *deployment* rather than a server build: WebSocket
upgrade through the proxy, keep-alive round trip, clock offset over HTTP vs
WebSocket, optional idle-timeout survival. See `docs/operators.md` for proxy
templates (nginx / Caddy / Traefik / Cloudflare) and the symptom table.

```bash
python tools/doctor.py --base https://jellyfin.example.com --user alice:secret --long
```

## CI

The kit is a plain asyncio CLI with exit code 0/1 — wire it after a server
build. Sketch (GitHub Actions):

```yaml
- run: dotnet build Jellyfin.sln
- run: |
    ./Jellyfin.Server/bin/Debug/net*/jellyfin --nowebclient \
      --datadir /tmp/jf-data --cachedir /tmp/jf-cache --configdir /tmp/jf-config &
    sleep 20
- run: |
    ffmpeg -f lavfi -i testsrc=duration=120:size=640x360:rate=24 \
           -f lavfi -i sine=frequency=440:duration=120 /tmp/media/"Test Movie (2020).mp4"
    pip install -r requirements.txt
    python -m syncplay_kit bootstrap --base http://127.0.0.1:8096 \
        --admin admin:ci-pw --media-dir /tmp/media
    python -m syncplay_kit run --base http://127.0.0.1:8096 \
        --user syncbot-a:sp-test --user syncbot-b:sp-test --user admin:ci-pw \
        --suite all
```

## Compatibility

Scenarios marked v2 in the table require a server implementing SyncPlay
protocol v2 — the specification lives in this repository at
`docs/SYNCPLAY.md`. `hot_join` additionally requires a server implementing
§7.1 (the SyncPlay v2 plugin ≥ 10.11.0.2; the integrated fork barriers every
joiner and fails it by design). Against a v1-only server, run the phase-0
subset: `group_wait_deadline`, `buffering_grace_*` require the robustness
patches; stock 10.x servers fail them by design (that is the point).

Plugin version floors name the Jellyfin 10.11 build. One source tree ships
against several server ABIs and only the fourth component distinguishes
releases, so `12.0.0.4` (Jellyfin 12) is the same code as `10.11.0.4` — see
§2.1 before comparing the string. Rendezvous (§7.2) lands in 10.11.0.4;
10.11.0.3 shipped it on a branch that measurement showed is never reached.
