from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from .config import Settings
from .db import Database
from .security import Auth


def listener_error(settings) -> str | None:
    """Reject public HTTP listeners. A private address may serve the home LAN."""
    from urllib.parse import urlsplit

    from .config import home_lan_host

    loopback = {"127.0.0.1", "localhost", "::1"}
    if settings.public_origin:
        origin = urlsplit(settings.public_origin)
        if origin.hostname not in loopback and (
            origin.scheme != "https" or not settings.secure_cookies
        ):
            return "A public origin requires HTTPS and CAMPIX_SECURE_COOKIES=true"
        return None
    if settings.host in loopback | {"0.0.0.0", "::"} or home_lan_host(settings.host):
        if settings.secure_cookies and settings.host not in loopback:
            return "Secure cookies on a LAN listener require HTTPS and CAMPIX_PUBLIC_ORIGIN"
        return None
    return "A public listener requires CAMPIX_PUBLIC_ORIGIN and a TLS proxy"


def serve_advertises(settings, config_path) -> bool:
    """Announce on the LAN unless discovery was explicitly disabled."""
    if "CAMPIX_MDNS" in os.environ:
        return bool(settings.mdns)
    if config_path is not None and Path(config_path).is_file():
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "mdns" in payload:
            return bool(settings.mdns)
    return True


def main():
    parser = argparse.ArgumentParser(prog="campixd", description="CamPiX camera service")
    parser.add_argument("--config", type=Path, help="JSON settings file")
    parser.add_argument("--data-dir", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-admin", help="Create the first admin; reads password without echo")
    init.add_argument("--username", default="admin")
    init.add_argument(
        "--password-stdin", action="store_true", help="Read password from stdin (automation only)"
    )
    reset = sub.add_parser(
        "reset-password",
        help="Local account recovery; requires access to the private data directory",
    )
    reset.add_argument("username")
    serve = sub.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--demo", action="store_true", default=None)
    serve.add_argument("--mediamtx-binary")
    sub.add_parser("probe", help="Print detected capture devices and FFmpeg encoders")
    setup = sub.add_parser("setup", help="Create the first admin and add detected physical cameras")
    setup.add_argument("--username", help="First administrator username (prompted when omitted)")
    setup.add_argument(
        "--non-interactive", action="store_true", help="Require an existing administrator"
    )
    setup.add_argument("--json", action="store_true", help="Print the setup summary as JSON")
    backup = sub.add_parser(
        "backup", help="Consistent SQLite backup (contains credential hashes; keep private)"
    )
    backup.add_argument("destination", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    settings = Settings.load(
        args.config,
        data_dir=args.data_dir,
        host=getattr(args, "host", None),
        port=getattr(args, "port", None),
        demo=getattr(args, "demo", None),
        mediamtx_binary=getattr(args, "mediamtx_binary", None),
    )
    if args.command == "setup":
        from .provision import ProvisionError, admin_username_problem
        from .provision import setup as provision

        def require_terminal():
            if not sys.stdin.isatty():
                raise ProvisionError(
                    "Admin setup requires an interactive terminal; use init-admin --password-stdin for automation"
                )

        # A typing mistake must not abort an installation half-way, so offer retries.
        def read_username():
            require_terminal()
            for _ in range(3):
                username = input("Administrator username: ").strip()
                problem = admin_username_problem(username)
                if not problem:
                    return username
                print(problem, file=sys.stderr)
            raise ProvisionError("No valid administrator username was entered")

        def read_password():
            require_terminal()
            for _ in range(3):
                password = getpass.getpass("Admin password (12-128 characters): ")
                if not 12 <= len(password) <= 128:
                    print("The password must contain 12-128 characters", file=sys.stderr)
                elif password != getpass.getpass("Repeat password: "):
                    print("Passwords do not match", file=sys.stderr)
                else:
                    return password
            raise ProvisionError("No matching administrator password was entered")

        try:
            result = provision(
                settings,
                non_interactive=args.non_interactive,
                username=args.username,
                username_reader=read_username,
                password_reader=read_password,
            )
        except (ProvisionError, RuntimeError) as exc:
            parser.error(str(exc))
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result["admin_created"]:
                print("Administrator created.")
            print(
                f"Added {len(result['cameras_added'])} physical camera profile(s); existing profiles preserved."
            )
            print(
                "Detection does not qualify hardware. Verify capture, recording, and disconnect recovery on this device."
            )
            for diagnostic in result["diagnostics"]:
                print(f"{diagnostic['code']}: {diagnostic['message']}")
        return
    settings.prepare()
    if args.command == "probe":
        from .provision import discover_inventory
        from .recording import encoder_probe

        print(
            json.dumps(
                {
                    **discover_inventory(ffmpeg=settings.ffmpeg),
                    "encoding": encoder_probe(settings.ffmpeg),
                },
                indent=2,
            )
        )
        return
    if args.command == "serve":
        check = Database(settings.data_dir / "campix.sqlite3")
        ready = check.one("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0")["n"]
        check.close()
        if not ready:
            parser.error("Create an administrator first: campixd init-admin")
        message = listener_error(settings)
        if message:
            parser.error(message)
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
        # The media-plane supervisor polls MediaMTX every two seconds; log only problems.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        if settings.host not in {"127.0.0.1", "localhost", "::1"} and not settings.public_origin:
            logging.getLogger("campix.service").warning(
                "Serving HTTP on %s:%s for a private network. Do not publish this port.",
                settings.host,
                settings.port,
            )
        from . import processes

        try:
            processes.contain_children()
        except OSError as exc:
            # Serving still works; only cleanup after an abnormal exit is weakened.
            logging.getLogger("campix.service").warning(
                "Child-process containment is unavailable (Windows error %s); "
                "MediaMTX/FFmpeg can outlive an abnormal service exit",
                getattr(exc, "winerror", None),
            )
        import uvicorn

        from .app import create_app

        settings.mdns = serve_advertises(settings, args.config)
        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            loop="campix.loops:windows_selector" if os.name == "nt" else "auto",
            workers=1,
            proxy_headers=settings.trust_proxy,
            forwarded_allow_ips="127.0.0.1,::1",
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=40,
        )
        return
    db = Database(settings.data_dir / "campix.sqlite3")
    try:
        if args.command == "init-admin":
            if db.one("SELECT id FROM users WHERE role='admin' AND disabled=0"):
                parser.error("An administrator already exists; use the authenticated users API")
            if args.password_stdin:
                password = sys.stdin.readline().rstrip("\r\n")
            else:
                password = getpass.getpass("Admin password (at least 12 characters): ")
                if password != getpass.getpass("Repeat password: "):
                    parser.error("Passwords do not match")
            try:
                Auth(db, settings).create_user(args.username, password)
            except ValueError:
                parser.error(
                    "Invalid username/password; username must be safe characters and password 12–128 characters"
                )
            print("Administrator created. Start CamPiX with: campixd serve")
        elif args.command == "backup":
            destination = args.destination.resolve()
            if destination.exists():
                parser.error("Backup destination already exists")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb"):
                pass
            with db.lock, sqlite3.connect(destination) as target:
                db.conn.backup(target)
                target.execute("DELETE FROM sessions")
                target.commit()
            destination.chmod(0o600)
            print(
                f"Database backup created: {destination}. Media files must be backed up separately."
            )
        elif args.command == "reset-password":
            from .models import UserCreate
            from .security import HASHER

            row = db.one("SELECT * FROM users WHERE username=?", (args.username,))
            if not row:
                parser.error("Account not found")
            password = getpass.getpass("New password (at least 12 characters): ")
            if password != getpass.getpass("Repeat password: "):
                parser.error("Passwords do not match")
            try:
                UserCreate(username=row["username"], password=password, role=row["role"])
            except ValueError:
                parser.error("Password must contain 12–128 characters")
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE users SET password=?,disabled=0 WHERE id=?",
                    (HASHER.hash(password), row["id"]),
                )
                conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
            print("Password reset; all account sessions revoked.")
    finally:
        db.close()
