#!/usr/bin/env python3
"""syncplay-doctor: checks whether a Jellyfin deployment can support SyncPlay
through whatever proxy/tunnel sits in front of it.

Most "SyncPlay doesn't work" reports are a WebSocket that is never upgraded or
is cut by proxy idle timeouts — failures that are invisible in normal browsing.
Run this against the SAME public URL your users type into their clients:

    python tools/doctor.py --base https://jellyfin.example.com --user alice:secret
    python tools/doctor.py --base https://jellyfin.example.com --user alice:secret --long

Checks: REST reachability, authentication, SyncPlay access policy, WebSocket
upgrade, keep-alive round trip, TimeSync (protocol v2 servers) or HTTP clock
offset, and optionally (--long, ~100s) whether an idle socket survives proxy
timeouts.
"""
import argparse
import asyncio
import json
import sys
import time

import aiohttp
import websockets

RESULTS = []


def report(status, name, detail):
    RESULTS.append((status, name, detail))
    print(f"{status:4s}  {name}: {detail}")


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", required=True, help="public base URL, e.g. https://jellyfin.example.com")
    p.add_argument("--user", required=True, metavar="USER:PASSWORD")
    p.add_argument("--long", action="store_true", help="also test idle-timeout survival (~100s)")
    args = p.parse_args()

    base = args.base.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    user, pw = args.user.split(":", 1)
    auth = 'MediaBrowser Client="syncplay-doctor", Device="doctor", DeviceId="syncplay-doctor", Version="1.0"'

    # 1. REST reachability.
    async with aiohttp.ClientSession(headers={"Authorization": auth}) as s:
        try:
            t0 = time.time()
            r = await s.get(f"{base}/System/Info/Public", timeout=aiohttp.ClientTimeout(total=10))
            info = await r.json()
            report("PASS", "REST reachability",
                   f"{info.get('ServerName')} {info.get('Version')} in {(time.time()-t0)*1000:.0f}ms")
        except Exception as e:
            report("FAIL", "REST reachability", f"{e} - nothing else can work")
            return summary()

        # 2. Auth.
        r = await s.post(f"{base}/Users/AuthenticateByName", json={"Username": user, "Pw": pw})
        if r.status != 200:
            report("FAIL", "authentication", f"HTTP {r.status}")
            return summary()
        token = (await r.json())["AccessToken"]
        auth_t = f'{auth}, Token="{token}"'
        s.headers["Authorization"] = auth_t
        report("PASS", "authentication", f"user {user}")

        # 3. SyncPlay access policy.
        r = await s.get(f"{base}/SyncPlay/List")
        if r.status == 200:
            report("PASS", "SyncPlay access", f"{len(await r.json())} visible group(s)")
        else:
            report("FAIL", "SyncPlay access",
                   f"HTTP {r.status} - check the user's SyncPlayAccess policy in the dashboard")

        # 4. HTTP clock offset (works on all servers).
        t_req = time.time() * 1000
        r = await s.get(f"{base}/GetUtcTime")
        t_res = time.time() * 1000
        d = await r.json()
        from datetime import datetime
        def iso_ms(x):
            return datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp() * 1000
        http_rtt = t_res - t_req
        http_offset = ((iso_ms(d["RequestReceptionTime"]) - t_req) + (iso_ms(d["ResponseTransmissionTime"]) - t_res)) / 2
        report("PASS", "HTTP time sync", f"rtt={http_rtt:.0f}ms offset={http_offset:+.0f}ms")

    # 5. WebSocket upgrade — the check that catches most broken deployments.
    try:
        ws = await websockets.connect(
            f"{ws_base}/socket?deviceId=syncplay-doctor",
            additional_headers={"Authorization": auth_t}, open_timeout=10, max_size=2**24)
        report("PASS", "WebSocket upgrade", f"connected to {ws_base}/socket")
    except Exception as e:
        report("FAIL", "WebSocket upgrade",
               f"{type(e).__name__}: {e} - SyncPlay CANNOT work; fix the proxy (see docs/operators.md)")
        return summary()

    async def recv_until(match, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                return None
            m = json.loads(raw)
            if match(m):
                return m
        return None

    def closed_report(e):
        """Classify a close during a check that should NOT close the socket
        (keep-alive, idle survival). An abnormal 1006 (no close frame) is the
        transport/proxy severing the connection - the doctor's core concern, so
        a FAIL. A clean app-level close (1000 + reason) here means the server
        dropped a healthy idle socket: still not the proxy, but worth flagging."""
        code, reason = getattr(e, "code", None), (getattr(e, "reason", "") or "")
        if code == 1006:
            report("FAIL", "connection stability",
                   f"socket dropped abnormally ({code}) with no close frame - typically a "
                   "proxy/tunnel severing the connection; raise read/idle timeouts (docs/operators.md)")
        else:
            report("WARN", "connection stability",
                   f"server closed a healthy socket ({code} {reason or 'no reason'}) - not a proxy "
                   "fault; check the Jellyfin service for restarts.")

    try:
        # 6. Keep-alive round trip.
        t0 = time.time()
        await ws.send(json.dumps({"MessageType": "KeepAlive"}))
        m = await recv_until(lambda m: m.get("MessageType") == "KeepAlive", 10)
        if m:
            report("PASS", "keep-alive round trip", f"{(time.time()-t0)*1000:.0f}ms")
        else:
            report("FAIL", "keep-alive round trip", "no KeepAlive response in 10s - messages are not flowing")

        # 7. Idle survival (optional). Runs BEFORE the TimeSync probe because
        # that probe can itself close the socket (see step 8), which would
        # otherwise sabotage this check on a stock server.
        if args.long:
            print("      holding the socket idle for 100s (proxy idle-timeout test)...")
            await recv_until(lambda m: False, 100)  # consume anything, expect server keepalives
            await ws.send(json.dumps({"MessageType": "KeepAlive"}))
            m = await recv_until(lambda m: m.get("MessageType") == "KeepAlive", 10)
            if m:
                report("PASS", "idle survival (100s)", "socket alive after 100s idle")
            else:
                report("FAIL", "idle survival (100s)", "socket dead after idle - raise proxy read/idle timeouts")

        # 8. TimeSync (protocol v2 servers). LAST, because it is destructive on
        # servers that reject unknown WS message types: stock Jellyfin (<=10.11)
        # throws deserializing an unrecognised MessageType and tears the socket
        # down (close 1000 "System Shutdown", though the server stays up), so
        # this probe must not precede any other check.
        t0_ms = int(time.time() * 1000)
        try:
            await ws.send(json.dumps({"MessageType": "TimeSync", "Data": t0_ms}))
            m = await recv_until(lambda m: m.get("MessageType") == "TimeSync"
                                 and isinstance(m.get("Data"), dict) and m["Data"].get("T0") == t0_ms, 5)
            if m:
                t3 = time.time() * 1000
                d = m["Data"]
                rtt = (t3 - d["T0"]) - (d["T2"] - d["T1"])
                offset = ((d["T1"] - d["T0"]) + (d["T2"] - t3)) / 2
                report("PASS", "WebSocket TimeSync (v2)", f"rtt={rtt:.0f}ms offset={offset:+.0f}ms")
                if abs(offset - http_offset) > 250:
                    report("WARN", "offset asymmetry",
                           f"HTTP vs WS offset differ by {abs(offset-http_offset):.0f}ms - HTTP path is likely "
                           "queueing (H2 multiplexing/bufferbloat); v2 clients will be fine, v1 clients may drift")
            else:
                report("WARN", "WebSocket TimeSync (v2)",
                       "no response - server predates protocol v2; clients fall back to HTTP time sync")
        except websockets.ConnectionClosed as e:
            report("WARN", "WebSocket TimeSync (v2)",
                   f"server closed the socket on the probe ({e.code} {e.reason or 'no reason'}) - it "
                   "rejects unrecognised WS message types (stock Jellyfin <=10.11 does this); no "
                   "protocol v2. v1 clients that never send TimeSync are unaffected.")

        await ws.close()
    except websockets.ConnectionClosed as e:
        closed_report(e)

    return summary()


def summary():
    fails = [r for r in RESULTS if r[0] == "FAIL"]
    warns = [r for r in RESULTS if r[0] == "WARN"]
    print(f"\nverdict: {'BROKEN - SyncPlay will not work' if fails else 'OK for SyncPlay' + (' (with warnings)' if warns else '')}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
