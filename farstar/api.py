import asyncio
import contextlib
import csv
import io
import secrets
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.background import BackgroundTask

from .db import Database
from . import __version__
from .panel import PanelError
from .miniapp import app_session, mount_miniapps
from .security import digest, new_session, rate_limit
from .services import Worker
from .store import DomainError, Store, audit, now

STATIC = Path(__file__).parent / "static"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Input):
    username: str = Field(max_length=128)
    password: str = Field(max_length=256)


class Credentials(Input):
    token: str = Field(default="", max_length=512)
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=256)


class ServerInput(Input):
    name: str = Field(min_length=2, max_length=80)
    location: str = Field(min_length=1, max_length=80)
    url: str = Field(max_length=512)
    inbound_id: int = Field(ge=1)
    subscription_url: str = Field(max_length=512)
    capacity: int = Field(default=1000, ge=1, le=1000000)
    active: bool = True
    trial: bool = True
    all_inbounds: bool = False
    credentials: Credentials | None = None

    @field_validator("url", "subscription_url")
    @classmethod
    def valid_url(cls, value):
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("یک آدرس http/https بدون query، رمز یا fragment وارد کنید.")
        return value.rstrip("/")


class PlanInput(Input):
    server_id: int = Field(ge=1)
    name: str = Field(min_length=2, max_length=80)
    days: int = Field(ge=1, le=1095)
    volume_gb: int = Field(ge=1, le=100000)
    price: int = Field(ge=1, le=100000000)
    ip_limit: int = Field(default=2, ge=0, le=100)
    active: bool = True
    description: str = Field(default="", max_length=500)


class TextInput(Input):
    text: str = Field(min_length=1, max_length=3000)


class Toggle(Input):
    enabled: bool


class BalanceInput(Input):
    amount: int = Field(ge=-100000000, le=100000000)
    reference: str = Field(min_length=8, max_length=100)


class ReviewInput(Input):
    approve: bool


class CardInput(Input):
    card_number: str = Field(min_length=16, max_length=24)
    card_holder: str = Field(min_length=3, max_length=80)

    @field_validator("card_number")
    @classmethod
    def normalize_card(cls, value):
        digits = ''.join(str(int(c)) for c in value if c.isdecimal())
        if len(digits) != 16 or any(not (c.isdecimal() or c in ' -') for c in value):
            raise ValueError("شماره کارت باید ۱۶ رقم باشد.")
        return digits


class ShopInput(Input):
    trial_enabled: bool
    trial_gb: int = Field(ge=1,le=10)
    trial_hours: int = Field(ge=1,le=72)
    referral_percent: int = Field(ge=0,le=50)


class CodeInput(Input):
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{3,32}$")
    max_uses: int = Field(ge=1, le=100000)
    expires_at: int = Field(gt=0)
    percent: int | None = Field(default=None, ge=1, le=100)
    amount: int | None = Field(default=None, ge=1, le=100000000)

    @model_validator(mode="after")
    def valid_expiry(self):
        if self.expires_at <= now():
            raise ValueError("تاریخ انقضا باید در آینده باشد.")
        return self


def create_app(settings, run_background=True):
    db = Database(settings.database_path)
    db.initialize()
    store = Store(db, settings)
    worker = Worker(store)
    bot = None
    state = {"telegram": "disabled", "telegram_error": None}

    async def run_bot():
        nonlocal bot
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from .bot import build_dispatcher
        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
        try:
            while True:
                try:
                    identity = await bot.get_me()
                    settings.bot_username = identity.username or ''
                    dispatcher = build_dispatcher(store, worker.provisioner, bot)
                    # Keep queued updates. Never silently remove an existing webhook.
                    webhook = await bot.get_webhook_info()
                    if webhook.url:
                        state["telegram"] = "webhook_conflict"
                        return
                    worker.bot = bot
                    state["telegram"] = "connected"
                    state["telegram_error"] = None
                    from aiogram.types import BotCommand
                    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in [
                        ("start", "منوی اصلی"), ("services", "سرویس‌های من"), ("support", "پشتیبانی"),
                        ("paysupport", "پشتیبانی پرداخت"), ("help", "آموزش Happ"), ("terms", "قوانین"), ("cancel", "لغو عملیات"),
                    ]])
                    if settings.mini_app_url:
                        from aiogram.types import MenuButtonWebApp, WebAppInfo, BotCommandScopeChat
                        from aiogram.exceptions import TelegramAPIError
                        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text='فروشگاه من',web_app=WebAppInfo(url=settings.mini_app_url)))
                        for admin_id in settings.admin_ids:
                            try:
                                await bot.set_my_commands([BotCommand(command='start',description='منوی مشتری'),
                                    BotCommand(command='admin',description='مدیریت فروشگاه'),
                                    BotCommand(command='help',description='آموزش Happ')],scope=BotCommandScopeChat(chat_id=admin_id))
                                if settings.admin_mini_app_url:
                                    await bot.set_chat_menu_button(chat_id=admin_id,menu_button=MenuButtonWebApp(
                                        text='مدیریت',web_app=WebAppInfo(url=settings.admin_mini_app_url)))
                            except TelegramAPIError:
                                pass  # An administrator may not have started this bot yet.
                    await dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    state["telegram"] = "error"
                    state["telegram_error"] = type(exc).__name__
                    worker.bot = None
                await asyncio.sleep(30)
        finally:
            if bot:
                await bot.session.close()

    @asynccontextmanager
    async def lifespan(app):
        tasks = []
        if run_background:
            tasks.append(asyncio.create_task(worker.run()))
            if settings.bot_token and not settings.demo:
                tasks.append(asyncio.create_task(run_bot()))
        yield
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Farstar VPN", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.worker = store, worker

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        maximum = 6 * 1024 * 1024 if request.url.path == '/mini-api/receipts' else 65536
        if request.headers.get("content-length", "0").isdigit() and int(request.headers.get("content-length", "0")) > maximum:
            return JSONResponse({"detail": "درخواست بیش از حد بزرگ است."}, status_code=413)
        response = await call_next(request)
        response.headers.update({
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        })
        if request.url.path in {'/app', '/admin-app'}:
            del response.headers['X-Frame-Options']
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
                "img-src 'self' data: blob:; media-src 'self'; font-src 'self'; connect-src 'self'; "
                "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org; form-action 'self'; base-uri 'none'")
        return response

    @app.exception_handler(DomainError)
    @app.exception_handler(PanelError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse({"detail": "این اطلاعات تکراری است یا به رکورد دیگری وابسته است."}, status_code=409)

    def authorize(request: Request):
        token = request.cookies.get("farstar_session", "")
        session = db.one("SELECT * FROM sessions WHERE token_hash=? AND expires_at>?", (digest(token), now()))
        if not session:
            session = app_session(db, settings, request, 'admin')
        if not session:
            raise HTTPException(401, "دوباره وارد حساب مدیریت شوید.")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = request.headers.get("x-csrf-token", "")
            if not secrets.compare_digest(csrf, session["csrf"]):
                raise HTTPException(403, "شناسه امنیتی درخواست معتبر نیست.")
            origin = request.headers.get("origin")
            if origin and origin != settings.public_url.rstrip("/"):
                # Exact request origin also supports localhost preview without weakening same-origin checks.
                if origin != str(request.base_url).rstrip("/"):
                    raise HTTPException(403, "مبدأ درخواست معتبر نیست.")
        return session

    Auth = Annotated[dict, Depends(authorize)]

    @app.get("/health")
    def health():
        db.one("SELECT 1")
        return {"status": "ok", "version": __version__}

    @app.post("/api/auth/login")
    async def login(data: Login, request: Request, response: Response):
        ip = request.client.host if request.client else "unknown"
        if not rate_limit(db, "login:" + ip, 10, 900):
            raise HTTPException(429, "تلاش‌های ورود زیاد است؛ ۱۵ دقیقه دیگر امتحان کنید.")
        user_ok = secrets.compare_digest(data.username.encode(), settings.admin_username.encode())
        pass_ok = secrets.compare_digest(data.password.encode(), settings.admin_password.encode())
        if not user_ok or not pass_ok:
            await asyncio.sleep(0.4)
            raise HTTPException(401, "نام کاربری یا رمز عبور درست نیست.")
        token, csrf = new_session(db)
        response.set_cookie("farstar_session", token, httponly=True, secure=settings.cookie_secure,
                            samesite="strict", max_age=43200, path="/")
        with db.transaction() as con:
            audit(con, "admin", "auth.login", ip)
        return {"csrf": csrf}

    @app.get("/api/auth/session")
    def session(auth: Auth):
        return {"csrf": auth["csrf"], "username": settings.admin_username}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, auth: Auth):
        with db.transaction() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (digest(request.cookies.get("farstar_session", "")),))
            if auth.get('role') == 'admin':
                con.execute('DELETE FROM app_sessions WHERE token_hash=?', (auth['token_hash'],))
        response.delete_cookie("farstar_session")
        response.delete_cookie('farstar_admin')
        return {"ok": True}

    @app.get("/api/meta")
    def meta(auth: Auth):
        return {"name": settings.app_name, "demo": settings.demo, "currency": settings.currency,
                "telegram": state["telegram"], "telegram_error": state["telegram_error"],
                "bot_configured": bool(settings.bot_token), "worker_tick": worker.last_tick,
                "manual_payments": settings.manual_payments, "trial_enabled": settings.trial_enabled,
                "referral_percent": settings.referral_percent, "panel_version": "3.11.0",
                "setup": {"admin_ids": bool(settings.admin_ids), "servers": bool(db.one("SELECT 1 FROM servers")),
                          "plans": bool(db.one("SELECT 1 FROM plans")), "bot": bool(settings.bot_token)}}

    @app.get("/api/overview")
    def overview(auth: Auth):
        return store.stats()

    @app.get("/api/payment-settings")
    def payment_settings(auth: Auth):
        return {"currency": settings.currency, "manual_payments": settings.manual_payments,
                "card_number": settings.card_number, "card_holder": settings.card_holder}

    @app.get('/api/shop-settings')
    def shop_settings(auth: Auth):
        return {key:getattr(settings,key) for key in ['trial_enabled','trial_gb','trial_hours','referral_percent']}

    @app.put('/api/shop-settings')
    def shop_settings_update(data: ShopInput,auth: Auth):
        store.save_shop_settings(data.model_dump(),auth.get('user_id','admin'))
        return {'ok':True}

    @app.put("/api/payment-settings")
    def payment_settings_update(data: CardInput, auth: Auth):
        store.save_payment_details(data.card_number, data.card_holder)
        return {"ok": True}

    @app.get("/api/servers")
    def servers(auth: Auth):
        return db.all("""SELECT s.id,s.name,s.location,s.url,s.inbound_id,s.subscription_url,s.capacity,s.active,s.trial,s.all_inbounds,
             (SELECT COUNT(*) FROM services v WHERE v.server_id=s.id AND v.expires_at>?) clients
             FROM servers s ORDER BY s.id""", (now(),))

    @app.post("/api/servers")
    def server_create(data: ServerInput, auth: Auth):
        return {"id": store.save_server(data.model_dump())}

    @app.put("/api/servers/{server_id}")
    def server_update(server_id: int, data: ServerInput, auth: Auth):
        return {"id": store.save_server(data.model_dump(), server_id)}

    @app.post("/api/servers/{server_id}/test")
    async def server_test(server_id: int, auth: Auth):
        if settings.demo:
            raise DomainError("در حالت نمایشی اتصال خارجی انجام نمی‌شود. سرور واقعی را در اجرای اصلی اضافه کنید.")
        server = store.server(server_id)
        async with worker.provisioner.panel(server) as panel:
            ids = await worker.provisioner.target_inbounds(panel, server)
            inbound = await panel.inbound(ids[0])
            protocol = inbound.get("protocol", "unknown")
            supported = protocol in {"vless", "vmess", "trojan", "shadowsocks", "hysteria", "tuic"}
            return {"ok": bool(ids) and supported, "protocol": protocol, 'inbound_ids':ids,
                    "enabled": bool(inbound.get("enable")), "remark": inbound.get("remark", ""),
                    "message": f"اتصال موفق · {len(ids)} اینباند فعال" if supported else "پروتکل این اینباند برای فروش پشتیبانی نمی‌شود"}

    @app.get("/api/plans")
    def plans(auth: Auth):
        return db.all("SELECT p.*,s.name server_name,s.location FROM plans p JOIN servers s ON s.id=p.server_id ORDER BY p.id")

    @app.post("/api/plans")
    def plan_create(data: PlanInput, auth: Auth):
        return {"id": store.save_plan(data.model_dump())}

    @app.put("/api/plans/{plan_id}")
    def plan_update(plan_id: int, data: PlanInput, auth: Auth):
        return {"id": store.save_plan(data.model_dump(), plan_id)}

    @app.get("/api/users")
    def users(auth: Auth, q: str = "", offset: int = 0):
        pattern = "%" + q[:128] + "%"
        return db.all("""SELECT u.id,u.name,u.username,u.balance,u.banned,u.created_at,
            (SELECT COUNT(*) FROM services s WHERE s.user_id=u.id) services FROM users u
            WHERE u.name LIKE ? OR u.username LIKE ? OR CAST(u.id AS TEXT) LIKE ? ORDER BY u.created_at DESC LIMIT 50 OFFSET ?""",
                      (pattern, pattern, pattern, max(0, offset)))

    @app.get("/api/users/{user_id}")
    def user_detail(user_id: int, auth: Auth):
        user = store.user(user_id)
        if not user:
            raise DomainError("کاربر پیدا نشد.")
        return {"user": user, "ledger": db.all("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 100", (user_id,))}

    @app.post("/api/users/{user_id}/ban")
    def ban(user_id: int, data: Toggle, auth: Auth):
        with db.transaction() as con:
            if con.execute("UPDATE users SET banned=? WHERE id=?", (int(data.enabled), user_id)).rowcount != 1:
                raise DomainError("کاربر پیدا نشد.")
            audit(con, "admin", "user.ban" if data.enabled else "user.unban", user_id)
        return {"ok": True}

    @app.post("/api/users/{user_id}/balance")
    def balance(user_id: int, data: BalanceInput, auth: Auth):
        store.adjust_balance(user_id, data.amount, data.reference)
        return {"ok": True}

    @app.get("/api/orders")
    def orders(auth: Auth, status: str = "", q: str = "", offset: int = 0):
        return db.all("""SELECT o.*,u.name,json_extract(o.snapshot,'$.name') plan_name FROM orders o
          JOIN users u ON u.id=o.user_id WHERE (?='' OR o.status=?) AND
          (o.id LIKE ? OR u.name LIKE ? OR CAST(o.user_id AS TEXT) LIKE ?)
          ORDER BY o.created_at DESC LIMIT 50 OFFSET ?""",
                      (status, status, "%" + q[:128] + "%", "%" + q[:128] + "%", "%" + q[:128] + "%", max(0, offset)))

    @app.post("/api/orders/{order_id}/retry")
    def retry(order_id: str, auth: Auth):
        store.retry(order_id)
        return {"ok": True}

    @app.post("/api/orders/{order_id}/refund")
    async def refund(order_id: str, auth: Auth):
        if settings.demo:
            raise DomainError("بازپرداخت در حالت نمایشی غیرفعال است.")
        await worker.provisioner.refund_failed(order_id)
        return {"ok": True}

    @app.get("/api/services")
    def services(auth: Auth, q: str = "", offset: int = 0):
        return db.all("""SELECT v.id,v.user_id,v.email,v.expires_at,v.total_bytes,v.used_bytes,v.enabled,
          v.is_trial,v.synced_at,u.name,s.name server_name,p.name plan_name FROM services v
          JOIN users u ON u.id=v.user_id JOIN servers s ON s.id=v.server_id LEFT JOIN plans p ON p.id=v.plan_id
          WHERE v.email LIKE ? OR u.name LIKE ? OR CAST(v.user_id AS TEXT) LIKE ?
          ORDER BY v.created_at DESC LIMIT 50 OFFSET ?""",
                      ("%" + q[:128] + "%", "%" + q[:128] + "%", "%" + q[:128] + "%", max(0, offset)))

    @app.post("/api/services/{service_id}/toggle")
    async def toggle(service_id: str, data: Toggle, auth: Auth):
        if settings.demo:
            raise DomainError("تغییر سرویس واقعی در حالت نمایشی غیرفعال است.")
        await worker.provisioner.set_enabled(service_id, data.enabled)
        return {"ok": True}

    @app.get("/api/receipts")
    def receipts(auth: Auth, offset: int = 0):
        return db.all("SELECT r.*,u.name FROM receipts r JOIN users u ON u.id=r.user_id ORDER BY r.created_at DESC LIMIT 50 OFFSET ?", (max(0, offset),))

    @app.get("/api/receipts/{receipt_id}/image")
    async def receipt_image(receipt_id: str, auth: Auth):
        row = db.one("SELECT * FROM receipts WHERE id=?", (receipt_id,))
        if not row or not bot:
            raise DomainError("تصویر رسید در دسترس نیست.")
        f = await bot.get_file(row["file_id"])
        data = await bot.download_file(f.file_path)
        return Response(data.read(), media_type="image/jpeg", headers={"Content-Disposition": "inline"})

    @app.post("/api/receipts/{receipt_id}/review")
    def review(receipt_id: str, data: ReviewInput, auth: Auth):
        store.review_receipt(receipt_id, data.approve, "web-admin")
        return {"ok": True}

    @app.get("/api/tickets")
    def tickets(auth: Auth, offset: int = 0):
        return db.all("SELECT t.*,u.name FROM tickets t JOIN users u ON u.id=t.user_id ORDER BY t.updated_at DESC LIMIT 50 OFFSET ?", (max(0, offset),))

    @app.get("/api/tickets/{ticket_id}")
    def ticket(ticket_id: int, auth: Auth):
        return {"ticket": db.one("SELECT t.*,u.name FROM tickets t JOIN users u ON u.id=t.user_id WHERE t.id=?", (ticket_id,)),
                "messages": db.all("SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id", (ticket_id,))}

    @app.post("/api/tickets/{ticket_id}/reply")
    def ticket_reply(ticket_id: int, data: TextInput, auth: Auth):
        store.ticket(0, data.text, ticket_id, admin=True)
        return {"ok": True}

    @app.post("/api/tickets/{ticket_id}/close")
    def ticket_close(ticket_id: int, auth: Auth):
        with db.transaction() as con:
            con.execute("UPDATE tickets SET status='closed',updated_at=? WHERE id=?", (now(), ticket_id))
            audit(con, "admin", "ticket.close", ticket_id)
        return {"ok": True}

    @app.get("/api/codes")
    def codes(auth: Auth):
        return {"coupons": db.all("SELECT c.*,(SELECT COUNT(*) FROM orders o WHERE o.coupon=c.code AND o.status NOT IN ('expired','cancelled','refunded')) used FROM coupons c"),
                "gifts": db.all("SELECT g.*,(SELECT COUNT(*) FROM gift_claims x WHERE x.code=g.code) used FROM gifts g")}

    @app.post("/api/codes/{kind}")
    def code_create(kind: str, data: CodeInput, auth: Auth):
        with db.transaction() as con:
            if kind == "coupon" and data.percent:
                con.execute("INSERT INTO coupons(code,percent,max_uses,expires_at) VALUES (?,?,?,?)",
                            (data.code.upper(), data.percent, data.max_uses, data.expires_at))
            elif kind == "gift" and data.amount:
                con.execute("INSERT INTO gifts VALUES (?,?,?,?)",
                            (data.code.upper(), data.amount, data.max_uses, data.expires_at))
            else:
                raise DomainError("اطلاعات کد کامل نیست.")
            audit(con, "admin", "code.create", data.code)
        return {"ok": True}

    @app.post("/api/broadcast")
    def broadcast(data: TextInput, auth: Auth):
        if settings.demo:
            raise DomainError("ارسال همگانی در حالت نمایشی غیرفعال است.")
        return {"recipients": store.broadcast(data.text)}

    @app.get("/api/audit")
    def audits(auth: Auth, offset: int = 0):
        return db.all("SELECT * FROM audit ORDER BY id DESC LIMIT 50 OFFSET ?", (max(0, offset),))

    @app.get("/api/export/{kind}")
    def export(kind: str, auth: Auth):
        queries = {"users": "SELECT id,name,username,balance,banned,created_at FROM users",
                   "orders": "SELECT id,user_id,kind,price,currency,status,created_at,completed_at FROM orders",
                   "ledger": "SELECT * FROM ledger"}
        if kind not in queries:
            raise HTTPException(404)
        rows = db.all(queries[kind])
        stream = io.StringIO()
        if rows:
            writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
            writer.writeheader()
            for row in rows:
                writer.writerow({k: "'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v
                                 for k, v in row.items()})
        return Response(("\ufeff" + stream.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="farstar-{kind}.csv"'})

    @app.get("/api/backup")
    async def backup(auth: Auth):
        handle, name = tempfile.mkstemp(suffix=".db")
        import os
        os.close(handle)
        path = Path(name)
        await asyncio.to_thread(db.backup, path)
        return FileResponse(path, filename=f"farstar-backup-{now()}.db", background=BackgroundTask(path.unlink))

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    mount_miniapps(app, store, worker, STATIC)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
