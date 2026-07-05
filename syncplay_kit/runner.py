"""Conformance runner: drives scenarios against a live Jellyfin server."""
import argparse
import asyncio
import json
import secrets
import sys
import time
import traceback

from .client import SyncPlayClient
from .scenarios import SCENARIOS


class Ctx:
    """Per-run context handed to scenarios."""

    def __init__(self, base, users, movie_id, verbose):
        self.base = base
        self.users = users            # [(user, password), ...]
        self.movie_id = movie_id
        self.verbose = verbose
        self.run_nonce = secrets.token_hex(3)
        self.scenario_index = 0
        self.scenario_name = ""
        self.results = []             # (scenario, check name, ok, detail)
        self._clients = []
        self._t0 = time.time()

    def log(self, who, kind, **kw):
        if self.verbose:
            print(f"{time.time() - self._t0:9.3f} {who:10s} {kind:24s} "
                  + json.dumps(kw, default=str)[:200])

    def check(self, name, ok, detail):
        self.results.append((self.scenario_name, name, bool(ok), detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")

    async def new_client(self, i, connect=True):
        """Client for the i-th configured user, with a device id unique to
        this run and scenario so that stale sessions from earlier scenarios
        or runs cannot bleed in (their memberships live on server-side for
        the duration of the disconnect grace window)."""
        user, password = self.users[i]
        c = SyncPlayClient(
            self.base, f"bot{i}", user, password,
            app=f"kit-{self.run_nonce}-s{self.scenario_index}",
            log=self.log)
        await c.start()
        if connect:
            await c.ws_connect()
        self._clients.append(c)
        return c

    async def cleanup(self):
        for c in self._clients:
            try:
                await c.post("/SyncPlay/Leave")
            except Exception:
                pass
            try:
                await c.close()
            except Exception:
                pass
        self._clients = []


async def discover_movie(base, user, password):
    c = SyncPlayClient(base, "probe", user, password, app="kit-probe")
    await c.start()
    try:
        data = await c.get_json("/Items?IncludeItemTypes=Movie&Recursive=true&Limit=1")
        items = data.get("Items") or []
        if not items:
            raise RuntimeError("no movie found in the library; pass --movie <itemId> "
                               "or run 'python -m syncplay_kit bootstrap' first")
        return items[0]["Id"]
    finally:
        await c.close()


async def run(args):
    users = [tuple(u.split(":", 1)) for u in args.user]
    if not users:
        print("at least one --user user:password is required", file=sys.stderr)
        return 2

    movie_id = args.movie or await discover_movie(args.base, *users[0])

    selected = [s for s in SCENARIOS
                if (args.scenario and s.name == args.scenario)
                or (not args.scenario and (args.suite == "all" or s.suite == args.suite))]
    if not selected:
        print(f"no scenario matched (scenario={args.scenario!r}, suite={args.suite!r})", file=sys.stderr)
        return 2

    ctx = Ctx(args.base, users, movie_id, args.verbose)
    print(f"target={args.base} movie={movie_id} users={[u for u, _ in users]} "
          f"scenarios={[s.name for s in selected]}\n")

    for i, s in enumerate(selected):
        if len(users) < s.min_users:
            print(f"SKIP  {s.name} (needs {s.min_users} users, have {len(users)})")
            continue
        ctx.scenario_index = i
        ctx.scenario_name = s.name
        print(f"--- {s.name}")
        t0 = time.time()
        try:
            await asyncio.wait_for(s.func(ctx), timeout=s.timeout)
        except AssertionError as e:
            ctx.check(s.name, False, f"assertion failed: {e}")
        except asyncio.TimeoutError:
            ctx.check(s.name, False, f"scenario timed out after {s.timeout}s")
        except Exception:
            ctx.check(s.name, False, "crashed:\n" + traceback.format_exc(limit=4))
        finally:
            await ctx.cleanup()
        print(f"    ({time.time() - t0:.1f}s)\n")

    fails = [r for r in ctx.results if not r[2]]
    print("=== SUMMARY ===")
    for scenario, name, ok, _ in ctx.results:
        print(f"{'PASS' if ok else 'FAIL'}  [{scenario}] {name}")
    print(f"{len(ctx.results) - len(fails)}/{len(ctx.results)} checks passed")
    return 1 if fails else 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m syncplay_kit",
        description="SyncPlay protocol conformance kit")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run conformance scenarios against a server")
    r.add_argument("--base", required=True, help="server base URL, e.g. http://127.0.0.1:8097")
    r.add_argument("--user", action="append", default=[], metavar="USER:PASSWORD",
                   help="test user credentials (repeat; 3 users unlock every scenario)")
    r.add_argument("--movie", help="ItemId of a playable movie (default: first movie in the library)")
    r.add_argument("--suite", choices=["fast", "slow", "all"], default="fast",
                   help="fast ~2-3min; slow adds the disconnect-lifecycle scenarios (~5min)")
    r.add_argument("--scenario", help="run a single scenario by name")
    r.add_argument("-v", "--verbose", action="store_true", help="log every message")

    ls = sub.add_parser("list", help="list scenarios")

    b = sub.add_parser("bootstrap", help="set up a fresh test server (wizard, library, users)")
    b.add_argument("--base", required=True)
    b.add_argument("--admin", default="admin:admin-pw", metavar="USER:PASSWORD")
    b.add_argument("--bots", default="syncbot-a,syncbot-b", help="comma-separated bot user names")
    b.add_argument("--bot-password", default="sp-test")
    b.add_argument("--media-dir", required=False,
                   help="server-side path of a folder containing at least one video file")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "list":
        for s in SCENARIOS:
            print(f"{s.name:24s} suite={s.suite:5s} min_users={s.min_users} ({s.func.__doc__.strip().splitlines()[0]})")
        return 0
    if args.command == "bootstrap":
        from .bootstrap import bootstrap
        return asyncio.run(bootstrap(args))
    return asyncio.run(run(args))
