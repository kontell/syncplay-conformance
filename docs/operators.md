# Running SyncPlay behind a proxy — operator guide

SyncPlay needs two things from your reverse proxy that normal browsing does
not: **working WebSocket upgrades** and **generous idle timeouts**. Nearly
every "SyncPlay is broken" report traces back to one of these, and both fail
*silently* — browsing and playback work fine, but group members never receive
a single command.

Verify any setup with the doctor (run it against the **public** URL your
users actually use):

```
python tools/doctor.py --base https://jellyfin.example.com --user alice:secret --long
```

## The three rules

1. **Proxy the WebSocket.** Jellyfin's socket lives at `/socket` (any path —
   proxy upgrades for the whole vhost). If the upgrade is not forwarded, the
   REST API still works: users can create and join groups but never receive
   commands. Worst failure mode: silent.
2. **Idle/read timeouts ≥ 90s.** The server keep-alive cadence can leave up to
   ~60s between frames on a quiet socket. Proxies with 60s defaults (nginx)
   will cut the connection right at the edge; each cut is (best case) a
   reconnect and (worst case, pre-protocol-v2 servers) a silent kick from the
   group.
3. **Same URL for every participant.** Time sync and latency estimation assume
   comparable network paths. Mixing LAN URLs and public URLs in one group
   gives members different clock paths and sub-path WebSocket derivations;
   have everyone use the public address.

## nginx

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ""      close;
}

server {
    # ... listen/tls ...
    location / {
        proxy_pass http://127.0.0.1:8096;
        proxy_http_version 1.1;

        # WebSocket upgrade (rule 1)
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # Idle timeouts (rule 2) - default 60s is too tight
        proxy_read_timeout  120s;
        proxy_send_timeout  120s;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }
}
```

## Caddy

Caddy v2 proxies WebSockets automatically and holds idle sockets; the default
works:

```caddyfile
jellyfin.example.com {
    reverse_proxy 127.0.0.1:8096
}
```

If you set `stream_timeout` or run behind `transport http` tweaks, keep any
idle limits ≥ 120s.

## Traefik

WebSockets are proxied automatically. The one setting that bites is the
entrypoint's `readTimeout`/`idleTimeout` if you customized
`respondingTimeouts` — keep them ≥ 120s or 0 (unlimited):

```yaml
entryPoints:
  websecure:
    address: ":443"
    transport:
      respondingTimeouts:
        readTimeout: 0
        idleTimeout: 180s
```

## Cloudflare (proxied DNS)

Cloudflare passes WebSockets, but adds anycast jitter to every time-sync
measurement and enforces a ~100s idle limit on the free tier. It works, but:

- expect looser sync for remote members (protocol v2 clients compensate
  better because they measure over the WebSocket);
- keep client keep-alives well under 100s (Jellyfin's defaults are);
- for best results, bypass the Cloudflare proxy (grey-cloud) for the Jellyfin
  hostname and terminate TLS yourself.

## VPNs (WireGuard / Tailscale)

Usually the best case: symmetric paths, no TLS termination, no HTTP/2
multiplexing. Two caveats:

- MTU mismatches can fragment large WebSocket frames — if the doctor's
  keep-alive check passes but group joins hang, test with a lower tunnel MTU;
- Tailscale connections relayed through DERP (both peers behind hard NAT) add
  100-300ms of asymmetric jitter; sync tolerance handles it on protocol v2
  servers (per-member adaptive tolerance), but direct connections are better.

## Symptom → cause quick table

| Symptom | Likely cause | Doctor check |
|---|---|---|
| Can join groups, never receive play/pause | WS upgrade not proxied | `WebSocket upgrade` FAIL |
| Members drop out of groups every minute or two | proxy idle timeout | `idle survival` FAIL |
| One remote member constantly stalls/gets corrected | clock offset from queueing | `offset asymmetry` WARN |
| Everything works on LAN, breaks remotely | any of the above; different URLs | run doctor against the public URL |
