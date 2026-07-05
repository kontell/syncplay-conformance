"""One-time setup of a fresh Jellyfin test server: startup wizard, a movie
library, and SyncPlay-enabled bot users."""
import asyncio

import aiohttp

HDR = 'MediaBrowser Client="syncplay-kit", Device="bootstrap", DeviceId="kit-bootstrap", Version="1.0"'


async def bootstrap(args):
    base = args.base.rstrip("/")
    admin_user, admin_pw = args.admin.split(":", 1)
    bots = [b.strip() for b in args.bots.split(",") if b.strip()]

    async with aiohttp.ClientSession(headers={"Authorization": HDR}) as s:
        async def post(path, body=None, ok=(200, 204)):
            r = await s.post(f"{base}{path}", json=body)
            txt = await r.text()
            print(f"POST {path} -> {r.status}")
            assert r.status in ok, f"{path}: {r.status} {txt[:300]}"
            return txt

        # Startup wizard (idempotent on an already-configured server: the
        # endpoints 404 once the wizard is complete).
        r = await s.get(f"{base}/Startup/Configuration")
        if r.status == 200:
            await post("/Startup/Configuration", {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"})
            await s.get(f"{base}/Startup/User")
            await post("/Startup/User", {"Name": admin_user, "Password": admin_pw})
            await post("/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
            await post("/Startup/Complete")
        else:
            print("startup wizard already completed")

        # Authenticate admin.
        r = await s.post(f"{base}/Users/AuthenticateByName", json={"Username": admin_user, "Pw": admin_pw})
        auth = await r.json()
        assert r.status == 200, f"admin login failed: {r.status}"
        s.headers["Authorization"] = f'{HDR}, Token="{auth["AccessToken"]}"'

        # Library.
        if args.media_dir:
            lib = {"LibraryOptions": {"EnableRealtimeMonitor": False, "EnableInternetProviders": False,
                                      "SaveLocalMetadata": False, "PathInfos": [{"Path": args.media_dir}]}}
            r = await s.post(f"{base}/Library/VirtualFolders?name=Movies&collectionType=movies&refreshLibrary=true", json=lib)
            print(f"create library -> {r.status}")

        # Wait for a movie to appear.
        item = None
        for _ in range(60):
            r = await s.get(f"{base}/Items?IncludeItemTypes=Movie&Recursive=true&Fields=RunTimeTicks")
            d = await r.json()
            items = d.get("Items") or []
            if items and items[0].get("RunTimeTicks"):
                item = items[0]
                break
            await asyncio.sleep(2)
        if not item:
            print("WARNING: no movie with a known runtime appeared; scenarios need one")
        else:
            print(f"movie: {item['Id']} ({item['Name']}, {item['RunTimeTicks']} ticks)")

        # Bot users.
        for u in bots:
            r = await s.post(f"{base}/Users/New", json={"Name": u, "Password": args.bot_password})
            if r.status != 200:
                print(f"user {u}: {r.status} (may already exist)")
                continue
            uj = await r.json()
            pol = uj["Policy"]
            pol.update({"EnableAllFolders": True, "SyncPlayAccess": "CreateAndJoinGroups", "EnableMediaPlayback": True})
            r = await s.post(f"{base}/Users/{uj['Id']}/Policy", json=pol)
            print(f"user {u} policy -> {r.status}")

        print("\nbootstrap complete; run e.g.:")
        creds = " ".join(f"--user {u}:{args.bot_password}" for u in bots) + f" --user {admin_user}:{admin_pw}"
        print(f"  python -m syncplay_kit run --base {base} {creds} --suite all")
    return 0
