"""Reference SyncPlay client core.

A minimal, protocol-conforming SyncPlay client (see docs/SYNCPLAY.md in the
jellyfin repository): REST control plane, WebSocket feedback plane, NTP-style
time sync over the WebSocket with HTTP fallback, ready gating and position
estimation. The conformance scenarios drive real servers with instances of
this class; it is also intended as the starting point for Python-based client
implementations (e.g. jellyfin-kodi).
"""
import asyncio
import json
import time
from datetime import datetime

import aiohttp
import websockets

TICKS = 10_000_000  # ticks per second


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time() % 1) * 1000):03d}Z"


def parse_iso_ms(s):
    """ISO-8601 → unix seconds (float)."""
    s2 = s.replace("Z", "+00:00")
    head, _, rest = s2.partition("+")
    if "." in head:
        b, f = head.split(".")
        head = f"{b}.{(f + '000000')[:6]}"
    return datetime.fromisoformat(head + "+" + rest).timestamp()


def cmd_pos_estimate(cmd, at=None):
    """Estimated server-side position ticks now, extrapolated from a command."""
    at = at or time.time()
    when = parse_iso_ms(cmd["When"])
    base = cmd.get("PositionTicks") or 0
    return base + max(0.0, (at - when)) * TICKS


class SyncPlayClient:
    """A scriptable SyncPlay client speaking protocol v1 or v2."""

    def __init__(self, base, name, user, password, app="syncplay-kit", log=None):
        self.base = base.rstrip("/")
        self.ws_base = self.base.replace("https://", "wss://").replace("http://", "ws://")
        self.name = name
        self.user = user
        self.password = password
        self.app = app
        self.device_id = f"{user}-{app}"
        self.token = None
        self.session = None
        self.ws = None
        self.ws_task = None
        self.keepalive_task = None
        self.keepalive_enabled = True
        self.msgs = []          # (unix_time, MessageType, Data) of every WS message
        self.ws2 = None
        self.ws2_msgs = []
        self.closed_at = None
        self.playlist_item_id = None
        self._log = log or (lambda *a, **k: None)

    # --- lifecycle -----------------------------------------------------

    @property
    def auth(self):
        t = f', Token="{self.token}"' if self.token else ""
        return f'MediaBrowser Client="{self.app}", Device="{self.name}", DeviceId="{self.device_id}", Version="1.0"{t}'

    async def start(self):
        s = aiohttp.ClientSession(headers={"Authorization": self.auth})
        r = await s.post(f"{self.base}/Users/AuthenticateByName", json={"Username": self.user, "Pw": self.password})
        body = await r.json()
        await s.close()
        if r.status != 200:
            raise RuntimeError(f"login failed for {self.user}: {r.status} {body}")
        self.token = body["AccessToken"]
        self.session = aiohttp.ClientSession(headers={"Authorization": self.auth})
        self._log(self.name, "login")

    async def close(self):
        for task in (self.keepalive_task, self.ws_task):
            if task:
                task.cancel()
        for ws in (self.ws, self.ws2):
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
        if self.session:
            await self.session.close()

    # --- REST ----------------------------------------------------------

    async def post(self, path, body=None):
        r = await self.session.post(f"{self.base}{path}", json=body if body is not None else {})
        await r.text()
        self._log(self.name, "REST", path=path, status=r.status)
        return r.status

    async def get_json(self, path):
        r = await self.session.get(f"{self.base}{path}")
        return await r.json()

    async def ready(self, pos_ticks, playing=False):
        return await self.post("/SyncPlay/Ready", {
            "When": now_iso(), "PositionTicks": int(pos_ticks),
            "IsPlaying": playing, "PlaylistItemId": self.playlist_item_id})

    async def buffering(self, pos_ticks, playing=False):
        return await self.post("/SyncPlay/Buffering", {
            "When": now_iso(), "PositionTicks": int(pos_ticks),
            "IsPlaying": playing, "PlaylistItemId": self.playlist_item_id})

    # --- WebSocket -----------------------------------------------------

    async def ws_connect(self):
        url = f"{self.ws_base}/socket?deviceId={self.device_id}"
        self.ws = await websockets.connect(url, max_size=2**24, additional_headers={"Authorization": self.auth})
        self.ws_task = asyncio.create_task(self._reader(self.ws, self.msgs, "ws1"))
        if self.keepalive_task is None:
            self.keepalive_task = asyncio.create_task(self._keepaliver())
        self._log(self.name, "ws_connect")

    async def ws2_connect(self):
        """A second socket on the same session (multi-socket scenarios)."""
        url = f"{self.ws_base}/socket?deviceId={self.device_id}"
        self.ws2 = await websockets.connect(url, max_size=2**24, additional_headers={"Authorization": self.auth})
        asyncio.create_task(self._reader(self.ws2, self.ws2_msgs, "ws2"))
        self._log(self.name, "ws2_connect")

    async def _reader(self, ws, sink, tag):
        try:
            async for raw in ws:
                m = json.loads(raw)
                mt = m.get("MessageType")
                sink.append((time.time(), mt, m.get("Data")))
                if mt not in ("ForceKeepAlive", "KeepAlive"):
                    d = m.get("Data")
                    brief = {"Type": d.get("Type"), "V": d.get("StateVersion")} \
                        if mt == "SyncPlayGroupUpdate" and isinstance(d, dict) else \
                        ({"Command": d.get("Command"), "V": d.get("StateVersion")} if isinstance(d, dict) else d)
                    self._log(self.name, f"recv[{tag}]", type=mt, data=brief)
        except Exception as e:
            self.closed_at = time.time()
            self._log(self.name, f"ws_closed[{tag}]", err=str(e)[:90])

    async def _keepaliver(self):
        while True:
            await asyncio.sleep(15)
            if self.keepalive_enabled and self.ws is not None:
                try:
                    await self.ws.send(json.dumps({"MessageType": "KeepAlive"}))
                except Exception:
                    pass

    # --- time sync (protocol v2, WebSocket) -----------------------------

    async def timesync_ws(self, timeout=5):
        """One NTP exchange over the socket. Returns (offset_ms, rtt_ms, data)."""
        t0 = int(time.time() * 1000)
        sent_at = time.time()
        await self.ws.send(json.dumps({"MessageType": "TimeSync", "Data": t0}))
        t, d = await self.wait_for("TimeSync", timeout=timeout, after=sent_at - 0.001)
        if not d or d.get("T0") != t0:
            raise TimeoutError("no TimeSync response")
        t3 = t * 1000
        rtt = (t3 - d["T0"]) - (d["T2"] - d["T1"])
        offset = ((d["T1"] - d["T0"]) + (d["T2"] - t3)) / 2
        return offset, rtt, d

    # --- message inspection ---------------------------------------------

    async def wait_for(self, mtype, sub=None, timeout=10, after=0.0, sink=None):
        """First (time, Data) of a message of the given type (and GroupUpdate
        sub-Type) received after the given unix time; (None, None) on timeout."""
        sink = sink if sink is not None else self.msgs
        t_end = time.time() + timeout
        while time.time() < t_end:
            for t, mt, d in sink:
                if t > after and mt == mtype and (sub is None or (isinstance(d, dict) and d.get("Type") == sub)):
                    return t, d
            await asyncio.sleep(0.05)
        return None, None

    def count_since(self, mtype, after, sub=None, sink=None):
        sink = sink if sink is not None else self.msgs
        return [(t, d) for t, mt, d in sink
                if t > after and mt == mtype and (sub is None or (isinstance(d, dict) and d.get("Type") == sub))]

    async def wait_command(self, cmd_name, after, timeout=10):
        """First SyncPlayCommand with the given Command after the given time."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for t, d in self.count_since("SyncPlayCommand", after):
                if d.get("Command") == cmd_name:
                    return t, d
            await asyncio.sleep(0.05)
        return None, None
