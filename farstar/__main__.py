import argparse
import json
import logging
import secrets
from pathlib import Path


def setup():
    target = Path(".env")
    if target.exists():
        print(".env already exists; existing configuration was preserved.")
        return
    content = Path(".env.example").read_text(encoding="utf-8") if Path(".env.example").exists() else (
        "SECRET_KEY=REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS\n"
        "ADMIN_PASSWORD=REPLACE_WITH_A_RANDOM_PASSWORD\nBOT_TOKEN=\nADMIN_IDS=[]\n")
    content = content.replace("REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS", secrets.token_urlsafe(48))
    content = content.replace("REPLACE_WITH_A_RANDOM_PASSWORD", secrets.token_urlsafe(18))
    target.write_text(content, encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    print("Created .env with generated secrets. Your admin password is in .env (ADMIN_PASSWORD).")


def main():
    parser = argparse.ArgumentParser(prog="farstar")
    parser.add_argument("command", choices=["setup", "run", "demo", "backup", "doctor"], nargs="?", default="run")
    args = parser.parse_args()
    if args.command == "setup":
        setup()
        return
    if args.command == "demo":
        setup()
    from .config import Settings
    from .db import Database
    settings = Settings(**({"demo": True, "database_path": Path("data/demo.db"), "bot_token": "",
                            "admin_username": "admin", "admin_password": "farstar-demo-2026",
                            "cookie_secure": False,
                            "host": "127.0.0.1", "port": 8081, "public_url": "http://localhost:8081"}
                           if args.command == "demo" else {}))
    if args.command == "backup":
        from .store import now
        db = Database(settings.database_path)
        db.initialize()
        destination = settings.database_path.parent / "backups" / f"farstar-{now()}.db"
        db.backup(destination)
        print(destination.resolve())
        return
    if args.command == "doctor":
        from .store import Store
        db = Database(settings.database_path)
        db.initialize()
        store = Store(db, settings)
        result = {"database": "ok", "bot_configured": bool(settings.bot_token),
                  "admin_ids_configured": bool(settings.admin_ids), "panel_target": "3.11.0",
                  "servers": db.one("SELECT COUNT(*) n FROM servers")["n"],
                  "plans": len(store.plans()), "currency": settings.currency,
                  "cookie_secure": settings.cookie_secure}
        print(json.dumps(result, indent=2))
        return
    import uvicorn
    from .api import create_app
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP client / polling logs can contain a bot token or private panel path.
    for logger in ("httpx", "httpcore", "aiogram.event", "aiogram.dispatcher"):
        logging.getLogger(logger).setLevel(logging.CRITICAL)
    app = create_app(settings)
    if args.command == "demo":
        from .demo import seed
        seed(app.state.store)
        print("DEMO: sample database; no external connections. Login: admin / farstar-demo-2026")
    print(f"Dashboard: http://{settings.host}:{settings.port}")
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
