import asyncio
import io
import json
import secrets
import hashlib
from typing import Annotated, Literal

from aiogram.types import BufferedInputFile
from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from .happ import catalog, send_guide
from .panel import PanelError
from .security import digest, new_app_session, rate_limit, validate_telegram_init_data
from .services import subscription
from .store import DomainError, audit, now
from .experience import order_progress, service_health
from .uploads import receipt_form

COOKIE = {'customer': 'farstar_customer', 'admin': 'farstar_admin'}


def app_session(db, settings, request, role):
    token = request.headers.get('x-mini-session') or request.cookies.get(COOKIE[role], '')
    row = db.one("SELECT s.*,u.banned FROM app_sessions s JOIN users u ON u.id=s.user_id "
                 "WHERE token_hash=? AND role=? AND expires_at>?", (digest(token), role, now()))
    if not row or row['banned'] or (role == 'admin' and row['user_id'] not in settings.admin_ids):
        return None
    return row


def check_origin(request):
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'مبدأ درخواست معتبر نیست.')


def mutation_guard(request, session):
    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        if not secrets.compare_digest(request.headers.get('x-csrf-token', ''), session['csrf']):
            raise HTTPException(403, 'شناسه امنیتی درخواست معتبر نیست.')
        check_origin(request)


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class AuthInput(Input):
    init_data: str = Field(min_length=1, max_length=12000)
    role: Literal['customer', 'admin']


class RoleInput(Input):
    role: Literal['customer', 'admin']


class OrderInput(Input):
    plan_id: int = Field(ge=1)
    coupon: str = Field(default='', max_length=32)
    service_id: str | None = Field(default=None, max_length=40)


class TicketInput(Input):
    text: str = Field(min_length=1, max_length=3000)
    ticket_id: int | None = Field(default=None, ge=1)


class GiftInput(Input):
    code: str = Field(min_length=3, max_length=32)


class GuideInput(Input):
    device: str = Field(max_length=20)


class LabelInput(Input):
    label: str = Field(max_length=40)


class PreferencesInput(Input):
    expiry_alerts: bool
    quota_alerts: bool


def mount_miniapps(app, store, worker, static):
    db, settings = store.db, store.settings
    image_lock = asyncio.Semaphore(1)

    def user_session(request: Request):
        session = app_session(db, settings, request, 'customer')
        if not session:
            raise HTTPException(401, 'مینی‌اپ را از دکمه داخل ربات باز کنید.')
        mutation_guard(request, session)
        if not rate_limit(db, f"mini:{session['user_id']}", 100, 60):
            raise HTTPException(429, 'درخواست‌ها زیاد است؛ کمی صبر کنید.')
        return session

    Customer = Annotated[dict, Depends(user_session)]

    def set_session(response, request, user_id, role):
        token, csrf = new_app_session(db, user_id, role)
        response.set_cookie(COOKIE[role], token, httponly=True, secure=request.url.scheme == 'https',
                            samesite='none' if request.url.scheme == 'https' else 'strict', path='/', max_age=7200)
        # An in-memory token also supports Telegram Web when third-party cookies are blocked.
        return {'csrf': csrf, 'role': role, 'user_id': user_id, 'session_token': token}

    @app.post('/mini-api/auth')
    def authenticate(data: AuthInput, request: Request, response: Response):
        check_origin(request)
        ip = request.client.host if request.client else 'unknown'
        if not rate_limit(db, 'mini-auth:' + ip, 60, 60):
            raise HTTPException(429, 'تلاش‌های ورود زیاد است؛ کمی صبر کنید.')
        try:
            actor = validate_telegram_init_data(data.init_data, settings.bot_token)
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc
        if data.role == 'admin' and actor['id'] not in settings.admin_ids:
            raise HTTPException(403, 'این بخش فقط برای مدیر فروشگاه است.')
        name = ' '.join(str(actor.get(key, '')) for key in ['first_name', 'last_name']).strip() or 'کاربر'
        user = store.register(actor['id'], name, str(actor.get('username', '')))
        if user['banned']:
            raise HTTPException(403, 'حساب شما مسدود است؛ از /support در ربات پیگیری کنید.')
        result = set_session(response, request, actor['id'], data.role)
        with db.transaction() as con:
            audit(con, actor['id'], 'mini.login', data.role)
        return result

    @app.get('/mini-api/context')
    def context():
        from .branding import branding
        brand = branding(store).data()
        return {'demo': settings.demo, 'name': settings.app_name, 'bot_username': settings.bot_username,
                'logo_url': brand['logo_url'], 'license_issuer': bool(getattr(store, 'license_authority', None))}

    @app.post('/mini-api/demo-login')
    def demo_login(data: RoleInput, request: Request, response: Response):
        if not settings.demo:
            raise HTTPException(404)
        check_origin(request)
        actor = settings.admin_ids[0] if data.role == 'admin' and settings.admin_ids else 999000000
        if data.role == 'customer':
            actor = (db.one('SELECT id FROM users ORDER BY created_at LIMIT 1') or {'id': 100000})['id']
        elif actor not in settings.admin_ids:
            settings.admin_ids.append(actor)
        store.register(actor, 'مدیر نمایشی' if data.role == 'admin' else 'مشتری نمایشی')
        return set_session(response, request, actor, data.role)

    @app.get('/mini-api/session')
    def session(request: Request, role: Literal['customer', 'admin']):
        current = app_session(db, settings, request, role)
        if not current:
            raise HTTPException(401, 'ورود به تلگرام لازم است.')
        return {'csrf': current['csrf'], 'role': role, 'user_id': current['user_id']}

    @app.post('/mini-api/logout')
    def logout(data: RoleInput, request: Request, response: Response):
        current = app_session(db, settings, request, data.role)
        if not current:
            raise HTTPException(401)
        mutation_guard(request, current)
        with db.transaction() as con:
            con.execute('DELETE FROM app_sessions WHERE token_hash=?', (current['token_hash'],))
        response.delete_cookie(COOKIE[data.role])
        return {'ok': True}

    @app.get('/mini-api/me')
    def me(auth: Customer):
        user = store.user(auth['user_id'])
        referrals = db.one('SELECT COUNT(*) n FROM users WHERE referrer_id=?', (user['id'],))['n']
        return {'user': {key: user[key] for key in ['id', 'name', 'username', 'balance', 'terms_at', 'trial_used']},
                'currency': settings.currency, 'name': settings.app_name, 'manual_payments': settings.manual_payments,
                'card_number': settings.card_number if settings.manual_payments else '',
                'card_holder': settings.card_holder if settings.manual_payments else '',
                'trial_enabled': settings.trial_enabled, 'referral_percent': settings.referral_percent,
                'referrals': referrals, 'demo': settings.demo}

    @app.get('/mini-api/plans')
    def plans(auth: Customer):
        return store.plans()

    @app.get('/mini-api/preferences')
    def preferences(auth: Customer):
        return store.notification_preferences(auth['user_id'])

    @app.put('/mini-api/preferences')
    def save_preferences(data: PreferencesInput, auth: Customer):
        return store.save_notification_preferences(auth['user_id'], data.model_dump())

    @app.get('/mini-api/terms')
    def terms(auth: Customer):
        from .bot import TERMS
        from .branding import branding
        return {'text': branding(store).text('terms') or TERMS}

    @app.post('/mini-api/terms/accept')
    def accept_terms(auth: Customer):
        store.accept_terms(auth['user_id'])
        return {'ok': True}

    async def membership(user_id):
        if not settings.required_channel or user_id in settings.admin_ids:
            return
        if not worker.bot:
            raise DomainError('بررسی عضویت موقتاً ممکن نیست؛ دوباره امتحان کنید.')
        member = await worker.bot.get_chat_member(settings.required_channel, user_id)
        joined = member.status in {'member', 'administrator', 'creator'} or (
            member.status == 'restricted' and getattr(member, 'is_member', False))
        if not joined:
            raise DomainError('ابتدا از منوی ربات عضو کانال فروشگاه شوید.')

    def order_view(row):
        result = {key: row[key] for key in ['id', 'price', 'currency', 'status', 'kind', 'created_at', 'expires_at', 'service_id']}
        result['plan'] = {key: json.loads(row['snapshot'])[key] for key in ['name', 'days', 'volume_gb']}
        result['progress'] = order_progress(db, row)
        return result

    @app.post('/mini-api/orders')
    async def order_create(data: OrderInput, auth: Customer):
        await membership(auth['user_id'])
        return order_view(store.create_order(auth['user_id'], data.plan_id, data.coupon, data.service_id))

    @app.get('/mini-api/orders')
    def orders(auth: Customer):
        rows = db.all('SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 50', (auth['user_id'],))
        return [order_view(row) for row in rows]

    @app.post('/mini-api/orders/{order_id}/wallet')
    async def pay(order_id: str, auth: Customer):
        await membership(auth['user_id'])
        store.pay_wallet(order_id, auth['user_id'])
        return {'ok': True, 'order': order_view(store.order(order_id, auth['user_id']))}

    @app.post('/mini-api/orders/{order_id}/cancel')
    def cancel(order_id: str, auth: Customer):
        store.cancel_order(order_id, auth['user_id'])
        return {'ok': True}

    @app.post('/mini-api/orders/{order_id}/invoice')
    async def invoice(order_id: str, auth: Customer):
        from aiogram.types import LabeledPrice
        order = store.order(order_id, auth['user_id'])
        if not worker.bot or order['currency'] != 'XTR' or order['status'] != 'pending' or order['expires_at'] < now():
            raise DomainError('این فاکتور با استار قابل پرداخت نیست.')
        url = await worker.bot.create_invoice_link(title='خرید سرویس Farstar',
                     description=json.loads(order['snapshot'])['name'], payload='order:' + order_id,
                     currency='XTR', prices=[LabeledPrice(label='سرویس', amount=order['price'])], provider_token='')
        return {'url': url}

    def owned(service_id, user_id):
        return store.owned_service(service_id, user_id)

    def service_view(service):
        result = {key: service[key] for key in ['id', 'plan_id', 'server_id', 'email', 'expires_at', 'total_bytes',
                  'used_bytes', 'enabled', 'is_trial', 'synced_at']}
        server = store.server(service['server_id'])
        result.update(server_name=server['name'], subscription=subscription(server, service['sub_id']))
        result.update(label=service.get('label', ''), health=service_health(service))
        return result

    @app.get('/mini-api/services')
    def services(auth: Customer):
        return [service_view(row) for row in db.all("SELECT v.*,COALESCE(l.label,'') label FROM services v LEFT JOIN service_labels l "
                                                  'ON l.service_id=v.id WHERE user_id=? ORDER BY created_at DESC',
                                                  (auth['user_id'],))]

    @app.put('/mini-api/services/{service_id}/label')
    def label(service_id: str, data: LabelInput, auth: Customer):
        return {'label': store.rename_service(service_id, auth['user_id'], data.label)}

    @app.post('/mini-api/services/{service_id}/diagnose')
    async def diagnose(service_id: str, auth: Customer):
        service = owned(service_id, auth['user_id'])
        error = False
        if not settings.demo:
            if not rate_limit(db, f"mini-sync:{auth['user_id']}", 6, 60):
                raise DomainError('برای بررسی دوباره کمی صبر کن.')
            try:
                await worker.provisioner.sync_service(service)
            except (DomainError, PanelError):
                error = True
        result = service_health(owned(service_id, auth['user_id']))
        result['panel_unavailable'] = error
        if error:
            result['fresh'] = False
        return result

    @app.post('/mini-api/services/{service_id}/sync')
    async def sync(service_id: str, auth: Customer):
        service = owned(service_id, auth['user_id'])
        if settings.demo:
            return service_view(service)
        if not rate_limit(db, f"mini-sync:{auth['user_id']}", 6, 60):
            raise DomainError('برای بروزرسانی کمی صبر کنید.')
        await worker.provisioner.sync_service(service)
        return service_view(owned(service_id, auth['user_id']))

    @app.get('/mini-api/services/{service_id}/qr')
    def qr(service_id: str, auth: Customer):
        from .qr import subscription_card
        service = owned(service_id, auth['user_id'])
        return Response(subscription_card(subscription(store.server(service['server_id']), service['sub_id']),service),media_type='image/png')

    @app.post('/mini-api/trial')
    async def trial(auth: Customer):
        await membership(auth['user_id'])
        return order_view(store.create_order(auth['user_id'], trial=True))

    @app.get('/mini-api/ledger')
    def ledger(auth: Customer):
        return db.all('SELECT amount,kind,created_at FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 50', (auth['user_id'],))

    @app.post('/mini-api/gift')
    def gift(data: GiftInput, auth: Customer):
        return {'amount': store.redeem(auth['user_id'], data.code)}

    @app.get('/mini-api/receipts')
    def receipts(auth: Customer):
        return db.all('SELECT id,order_id,amount,status,created_at FROM receipts WHERE user_id=? ORDER BY created_at DESC LIMIT 30',
                      (auth['user_id'],))

    def normalize_image(raw):
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 8000000 or image.width > 8000 or image.height > 8000:
                    raise DomainError('ابعاد تصویر رسید بیش از حد بزرگ است.')
                image.seek(0)
                rgb = image.convert('RGB')
                rgb.thumbnail((2400, 3200))
                output = io.BytesIO()
                rgb.save(output, 'JPEG', quality=90)
                return output.getvalue()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise DomainError('تصویر رسید معتبر نیست؛ عکس JPG یا PNG بفرستید.') from exc

    @app.post('/mini-api/receipts')
    async def receipt_upload(request: Request, auth: Customer):
        if not settings.manual_payments or settings.currency != 'IRT' or not settings.card_number:
            raise DomainError('پرداخت کارت‌به‌کارت هنوز آماده نیست.')
        if settings.demo or not worker.bot:
            raise DomainError('ارسال رسید فقط در ربات متصل فعال است.')
        if image_lock.locked():
            raise HTTPException(429, 'یک فیش در حال پردازش است؛ چند ثانیه بعد دوباره بفرست.')
        async with image_lock:
            async with receipt_form(request) as (fields, raw):
                return await save_receipt(auth, fields.amount, fields.request_id, fields.order_id, raw)

    async def save_receipt(auth, amount, request_id, order_id, raw):
            image = await asyncio.to_thread(normalize_image, raw)
            previous = db.one('SELECT * FROM upload_requests WHERE user_id=? AND request_id=?',
                              (auth['user_id'], request_id))
            if previous and previous['receipt_id']:
                receipt = db.one('SELECT amount,order_id FROM receipts WHERE id=?', (previous['receipt_id'],))
                if receipt['amount'] != amount or receipt['order_id'] != order_id:
                    raise DomainError('این شناسه رسید قبلاً برای سفارش یا مبلغ دیگری ثبت شده است.')
                return {'id': previous['receipt_id']}
            order=store.receipt_order(order_id,auth['user_id'])
            if amount != order['price']:
                raise DomainError('مبلغ رسید باید دقیقاً برابر مبلغ سفارش باشد.')
            image_hash=hashlib.sha256(image).hexdigest()
            if db.one('SELECT 1 FROM receipts WHERE image_hash=?',(image_hash,)):
                raise DomainError('این تصویر قبلاً برای یک رسید ثبت شده است.')
            with db.transaction() as con:
                row = con.execute('SELECT * FROM upload_requests WHERE user_id=? AND request_id=?',
                                  (auth['user_id'], request_id)).fetchone()
                if row and row['lease_until'] > now():
                    raise DomainError('این رسید در حال ارسال است؛ کمی صبر کنید.')
                con.execute('INSERT OR REPLACE INTO upload_requests VALUES (?,?,NULL,?)',
                            (auth['user_id'], request_id, now() + 120))
            try:
                sent = await worker.bot.send_photo(auth['user_id'], BufferedInputFile(image, filename='receipt.jpg'),
                                                   caption=f'🧾 رسید سفارش {order_id} · {amount:,} تومان · منتظر تأیید مدیر')
                file_id = sent.photo[-1].file_id
                existing = db.one('SELECT * FROM receipts WHERE file_id=?', (file_id,))
                if existing and existing['user_id'] == auth['user_id'] and existing['amount'] == amount and existing['order_id']==order_id:
                    receipt_id = existing['id']
                else:
                    receipt_id = store.add_receipt(auth['user_id'], amount, file_id,order_id,
                                file_unique_id=getattr(sent.photo[-1],'file_unique_id',None),image_hash=image_hash)
                with db.transaction() as con:
                    con.execute('UPDATE upload_requests SET receipt_id=?,lease_until=0 WHERE user_id=? AND request_id=?',
                                (receipt_id, auth['user_id'], request_id))
                return {'id': receipt_id}
            except Exception:
                with db.transaction() as con:
                    con.execute('UPDATE upload_requests SET lease_until=0 WHERE user_id=? AND request_id=?',
                                (auth['user_id'], request_id))
                raise

    @app.get('/mini-api/tickets')
    def tickets(auth: Customer):
        return db.all('SELECT * FROM tickets WHERE user_id=? ORDER BY updated_at DESC LIMIT 30', (auth['user_id'],))

    @app.get('/mini-api/tickets/{ticket_id}')
    def ticket(ticket_id: int, auth: Customer):
        ticket = db.one('SELECT * FROM tickets WHERE id=? AND user_id=?', (ticket_id, auth['user_id']))
        if not ticket:
            raise DomainError('تیکت پیدا نشد.')
        return {'ticket': ticket, 'messages': db.all('SELECT sender,body,created_at FROM ticket_messages WHERE ticket_id=? ORDER BY id', (ticket_id,))}

    @app.post('/mini-api/tickets')
    def ticket_create(data: TicketInput, auth: Customer):
        return {'id': store.ticket(auth['user_id'], data.text, data.ticket_id)}

    @app.get('/mini-api/help')
    def help_catalog(auth: Customer):
        return catalog()

    @app.post('/mini-api/help/send')
    async def help_send(data: GuideInput, auth: Customer):
        if not worker.bot or settings.demo:
            raise DomainError('GIF در محیط نمایشی ارسال نمی‌شود؛ آموزش در همین صفحه نمایش داده شده است.')
        await send_guide(worker.bot, store, auth['user_id'], data.device)
        return {'ok': True}

    @app.get('/app')
    @app.get('/admin-app')
    def miniapp_page():
        return FileResponse(static / 'mini.html')
