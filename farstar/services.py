import asyncio
import json
import logging
from html import escape
from urllib.parse import quote

from .panel import Panel, PanelError
from .store import DomainError, audit, credit, notify, now
from .experience import housekeeping, queue_service_alert, service_alert_content

log = logging.getLogger(__name__)
GIB = 1024 ** 3
SUPPORTED_PROTOCOLS = {'vless', 'vmess', 'trojan', 'shadowsocks', 'hysteria', 'tuic'}


def client_payload(record):
    """Mirror 3x-ui ClientRecord.ToClient; GET and POST have different field types."""
    result = dict(record)
    if "uuid" in result:
        result["id"] = result.pop("uuid")
    if isinstance(result.get("allowedIPs"), str):
        result["allowedIPs"] = [part.strip() for part in result["allowedIPs"].split(",") if part.strip()]
    if isinstance(result.get("reverse"), str):
        try:
            result["reverse"] = json.loads(result["reverse"]) if result["reverse"] else None
        except ValueError as exc:
            raise DomainError("تنظیم reverse کاربر در پنل معتبر نیست؛ تمدید نیاز به بررسی دارد.") from exc
        if result["reverse"] is not None and not isinstance(result["reverse"], dict):
            raise DomainError("ساختار reverse کاربر در پنل معتبر نیست.")
    if "createdAt" in result:
        result["created_at"] = result["createdAt"]
    for key in ("createdAt", "updatedAt"):
        result.pop(key, None)
    return result


def subscription(server, sub_id):
    return server["subscription_url"].rstrip("/") + "/" + quote(sub_id, safe="")


class Provisioner:
    def __init__(self, store, panel_factory=Panel):
        self.store = store
        self.db = store.db
        self.panel_factory = panel_factory

    def panel(self, server):
        return self.panel_factory(server, self.store.vault.open(server["credentials"]))

    async def target_inbounds(self, panel, server):
        rows = await panel.inbounds() if server.get('all_inbounds') else [await panel.inbound()]
        ids = sorted({row['id'] for row in rows if row.get('enable') and row.get('protocol') in SUPPORTED_PROTOCOLS})
        if not ids:
            raise DomainError('اینباند فعال و سازگار برای این اشتراک پیدا نشد.')
        return ids

    async def attach_missing(self, panel, server, record, inbound_ids=None):
        """Only add memberships for this verified identity; never detach or change quota."""
        if server.get('all_inbounds'):
            required = inbound_ids if inbound_ids is not None else await self.target_inbounds(panel, server)
            missing = sorted(set(required) - set(record.get('inboundIds', [])))
            if missing:
                await panel.attach(record['client']['email'], missing)
                record = await panel.get_client(record['client']['email'])
                if not record or not set(required).issubset(record.get('inboundIds', [])):
                    raise PanelError('اتصال اشتراک به همه اینباندها تأیید نشد.')
        return record

    def claim(self):
        with self.db.transaction() as con:
            row = con.execute("""SELECT * FROM orders WHERE
                (status='paid' OR (status='processing' AND lease_until<?)) AND next_attempt<=?
                ORDER BY created_at LIMIT 1""", (now(), now())).fetchone()
            if not row:
                return None
            con.execute("UPDATE orders SET status='processing',lease_until=?,attempts=attempts+1 WHERE id=?",
                        (now() + 300, row["id"]))
            return dict(row)

    async def process_one(self):
        order = self.claim()
        if not order:
            return False
        try:
            await self.provision(order)
        except Exception as exc:
            message = str(exc) if isinstance(exc, (DomainError, PanelError)) else "خطای داخلی؛ گزارش سرور را بررسی کنید."
            # Tracebacks from HTTP exceptions may contain panel credentials in URLs.
            log.warning("Provision failed order=%s type=%s", order["id"], type(exc).__name__)
            with self.db.transaction() as con:
                current = con.execute("SELECT attempts FROM orders WHERE id=?", (order["id"],)).fetchone()
                failed = current["attempts"] >= 5
                con.execute("UPDATE orders SET status=?,last_error=?,lease_until=0,next_attempt=? WHERE id=?",
                            ("failed" if failed else "paid", message[:500], now() + min(3600, 15 * 2 ** current["attempts"]),
                             order["id"]))
                if failed:
                    self.store.admins(con, f"⚠️ سفارش <code>{order['id']}</code> نیاز به بررسی دارد.\n{escape(message)}",
                                      "failed:" + order["id"])
                    notify(con, order["user_id"], f"سفارش {order['id']} پرداخت شده و برای بررسی به پشتیبانی ارجاع شد. مبلغ دوباره پرداخت نکنید.",
                           "order-review:" + order["id"])
        return True

    async def provision(self, order):
        server = self.store.server(order["server_id"])
        snapshot = json.loads(order["snapshot"])
        async with self.panel(server) as panel:
            email = snapshot['email']
            current = await panel.get_client(email)
            stored = self.store.order(order["id"])
            naming_conflict = current and snapshot.get('name_reserved') and current['client'].get('subId') != snapshot['sub_id']
            if order['kind'] != 'renew' and not stored['target_json'] and (not current or naming_conflict):
                highest = await panel.highest_client_number(self.store.settings.client_name_prefix)
                for attempt in range(10):
                    snapshot = self.store.reserve_client_name(order['id'],highest,advance=attempt > 0 or bool(naming_conflict))
                    email = snapshot['email']
                    current = await panel.get_client(email)
                    if not current or current['client'].get('subId') == snapshot['sub_id']:
                        break
                else:
                    raise DomainError('تداخل هم‌زمان نام کاربران؛ ساخت به تلاش بعدی موکول شد.')
            required = await self.target_inbounds(panel,server)
            if stored["target_json"]:
                target = json.loads(stored["target_json"])
            elif order["kind"] == "renew":
                if not current or not current.get('inboundIds'):
                    raise DomainError("کاربر سرویس در اینباند پنل پیدا نشد.")
                target = client_payload(current["client"])
                if target.get("subId") != snapshot["sub_id"]:
                    raise DomainError("شناسه اشتراک پنل با سفارش تطابق ندارد.")
                if int(target.get("totalGB", 0)) <= 0 or int(target.get("expiryTime", 0)) <= 0:
                    raise DomainError("تمدید سرویس نامحدود یا شروع‌نشده باید در پنل بررسی شود.")
                target["totalGB"] = int(target["totalGB"]) + snapshot["volume_gb"] * GIB
                target["expiryTime"] = max(now() * 1000, int(target["expiryTime"])) + int(snapshot["days"] * 86400000)
                target["enable"] = True
            else:
                target = {
                    "email": email, "id": snapshot["client_id"], "password": snapshot["password"],
                    "auth": snapshot["password"], "subId": snapshot["sub_id"], "tgId": order["user_id"],
                    "totalGB": snapshot["volume_gb"] * GIB,
                    "expiryTime": now() * 1000 + int(snapshot["days"] * 86400000),
                    "limitIp": snapshot["ip_limit"], "limitHwid": 0, "enable": True,
                    "comment": "Farstar order " + order["id"], "reset": 0, "trafficReset": "never",
                }
            if not stored["target_json"]:
                with self.db.transaction() as con:
                    con.execute("UPDATE orders SET target_json=? WHERE id=?", (json.dumps(target), order["id"]))
            if current:
                if current["client"].get("subId") != snapshot["sub_id"]:
                    raise DomainError("تداخل شناسه کاربر؛ سفارش نیاز به بررسی دستی دارد.")
                if not server.get('all_inbounds') and server["inbound_id"] not in current["inboundIds"]:
                    raise DomainError("کاربر به اینباند سفارش متصل نیست.")
                missing = sorted(set(required) - set(current.get('inboundIds',[])))
                if missing:
                    await panel.attach(email, missing)
                if order["kind"] == "renew":
                    await panel.update(email, target)
                # Retrying an ambiguous create: never overwrite a newly generated protocol secret.
            else:
                if order["kind"] == "renew":
                    raise DomainError("سرویس اصلی حذف شده است؛ ساخت مجدد خودکار انجام نمی‌شود.")
                await panel.add(target,required)
            confirmed = await panel.get_client(email)
            if not confirmed or not set(required).issubset(confirmed.get('inboundIds',[])):
                raise PanelError("ساخت سرویس در پنل تأیید نشد.")
            client = confirmed["client"]
            if (client.get("subId") != snapshot["sub_id"] or not client.get("enable") or
                    int(client.get("totalGB", -1)) != target["totalGB"] or
                    int(client.get("expiryTime", -1)) != target["expiryTime"]):
                raise PanelError("مقادیر سرویس در پنل با سفارش تطابق ندارد.")
        self.complete(order, server, confirmed)

    def complete(self, order, server, confirmed):
        client = confirmed["client"]
        service_id = order["service_id"] or order["id"]
        with self.db.transaction() as con:
            row = con.execute("SELECT status FROM orders WHERE id=?", (order["id"],)).fetchone()
            if row["status"] == "completed":
                return
            con.execute("""INSERT INTO services(id,user_id,plan_id,server_id,email,client_json,sub_id,
              expires_at,total_bytes,used_bytes,enabled,is_trial,created_at,synced_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
              plan_id=excluded.plan_id,client_json=excluded.client_json,expires_at=excluded.expires_at,
              total_bytes=excluded.total_bytes,used_bytes=excluded.used_bytes,enabled=excluded.enabled,
              synced_at=excluded.synced_at""",
                        (service_id, order["user_id"], order["plan_id"], order["server_id"], client["email"],
                         json.dumps(client), client["subId"], client["expiryTime"] // 1000, client["totalGB"],
                         confirmed.get("usedTraffic", 0), int(client["enable"]), int(order["kind"] == "trial"), now(), now()))
            con.execute("UPDATE orders SET status='completed',completed_at=?,service_id=?,lease_until=0,last_error=NULL WHERE id=?",
                        (now(), service_id, order["id"]))
            if order["kind"] == "purchase" and order["price"] > 0:
                user = con.execute("SELECT referrer_id FROM users WHERE id=?", (order["user_id"],)).fetchone()
                if user["referrer_id"] and not con.execute("SELECT 1 FROM referral_rewards WHERE referred_id=?", (order["user_id"],)).fetchone():
                    amount = order["price"] * self.store.settings.referral_percent // 100
                    con.execute("INSERT INTO referral_rewards VALUES (?,?)", (order["user_id"], order["id"]))
                    if amount:
                        credit(con, user["referrer_id"], amount, "referral", "referral:" + order["id"])
                        notify(con, user["referrer_id"], f"🎁 پاداش دعوت شما: {amount:,} به کیف پول اضافه شد.",
                               "referral:" + order["id"])
            link = subscription(server, client["subId"])
            notify(con, order["user_id"], f"✅ {'تمدید' if order['kind'] == 'renew' else 'فعال‌سازی'} سرویس انجام شد.\n"
                   f"شماره سفارش: <code>{order['id']}</code>\n\nلینک اشتراک:\n<code>{escape(link)}</code>\n\n"
                   "از منوی «سرویس‌های من» QR و میزان مصرف را ببینید.", "delivery:" + order["id"])
            self.store.admins(con, f"🛍 سفارش {order['id']} تحویل شد؛ مبلغ {order['price']:,} {order['currency']}",
                              "sale:" + order["id"])
            audit(con, "worker", "order.complete", order["id"])

    async def sync_service(self, service, inbound_ids=None):
        server = self.store.server(service["server_id"])
        async with self.panel(server) as panel:
            record = await panel.get_client(service["email"])
            if not record:
                raise DomainError("سرویس در پنل پیدا نشد؛ پشتیبانی را مطلع کنید.")
            client = record["client"]
            if client.get("subId") != service["sub_id"]:
                raise DomainError("شناسه سرویس تغییر کرده است؛ پشتیبانی را مطلع کنید.")
            record = await self.attach_missing(panel,server,record,inbound_ids)
            client = record['client']
            if client.get('subId') != service['sub_id']:
                raise DomainError('شناسه سرویس هنگام اتصال اینباند تغییر کرد؛ بررسی پشتیبانی لازم است.')
            with self.db.transaction() as con:
                con.execute("UPDATE services SET client_json=?,expires_at=?,total_bytes=?,used_bytes=?,enabled=?,synced_at=? WHERE id=?",
                            (json.dumps(client), int(client["expiryTime"]) // 1000, client["totalGB"],
                             record.get("usedTraffic", 0), int(client["enable"]), now(), service["id"]))
            return record

    async def set_enabled(self, service_id, enabled):
        service = self.db.one("SELECT * FROM services WHERE id=?", (service_id,))
        if not service:
            raise DomainError("سرویس پیدا نشد.")
        server = self.store.server(service["server_id"])
        async with self.panel(server) as panel:
            record = await panel.get_client(service["email"])
            if not record or record["client"].get("subId") != service["sub_id"]:
                raise DomainError("سرویس اصلی در پنل پیدا نشد.")
            target = client_payload(record["client"])
            target["enable"] = enabled
            await panel.update(service["email"], target)
        with self.db.transaction() as con:
            con.execute("UPDATE services SET enabled=? WHERE id=?", (int(enabled), service_id))
            audit(con, "admin", "service.enable" if enabled else "service.disable", service_id)

    async def refund_failed(self, order_id):
        order = self.store.order(order_id)
        if order["status"] != "failed" or order["kind"] == "renew":
            raise DomainError("بازپرداخت خودکار فقط برای خرید ناموفق است؛ تمدید نیاز به بررسی پشتیبانی دارد.")
        server = self.store.server(order["server_id"])
        snapshot = json.loads(order["snapshot"])
        async with self.panel(server) as panel:
            remote = await panel.get_client(snapshot["email"])
            if remote:
                if remote["client"].get("subId") != snapshot["sub_id"]:
                    raise DomainError("هویت کاربر پنل متفاوت است؛ بازپرداخت متوقف شد.")
                target = client_payload(remote["client"])
                target["enable"] = False
                await panel.update(snapshot["email"], target)
                verify = await panel.get_client(snapshot["email"])
                if verify and verify["client"].get("enable"):
                    raise DomainError("غیرفعال‌سازی سرویس در پنل تأیید نشد.")
        with self.db.transaction() as con:
            current = con.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
            if current["status"] != "failed":
                raise DomainError("وضعیت سفارش تغییر کرده است.")
            if order["payment_method"] == "wallet":
                credit(con, order["user_id"], order["price"], "refund", "refund:" + order_id)
            stars = order["payment_method"] == "stars"
            if stars:
                con.execute("UPDATE payments SET status='refund_pending' WHERE charge_id=?", (order["charge_id"],))
            manual=order['payment_method']=='card'
            con.execute("UPDATE orders SET status=? WHERE id=?", ("refund_pending" if stars or manual else "refunded", order_id))
            audit(con, "admin", "order.refund_request", order_id)
            if manual:
                notify(con,order['user_id'],'درخواست بازپرداخت بانکی برای پشتیبانی ثبت شد؛ نتیجه را از پشتیبانی پیگیری کن.','refund:'+order_id)
                self.store.admins(con,f"💳 سفارش <code>{order_id}</code> به بازپرداخت بانکی دستی مبلغ {order['price']:,} تومان نیاز دارد.",'manual-refund:'+order_id)
            elif not stars:
                notify(con, order["user_id"], f"مبلغ سفارش {order_id} به کیف پول بازگشت.", "refund:" + order_id)


class Worker:
    def __init__(self, store, bot=None):
        self.store, self.bot = store, bot
        self.db = store.db
        self.provisioner = Provisioner(store)
        self.last_sync = 0
        self.last_backup = 0
        self.last_tick = 0
        self.last_cleanup = 0

    async def deliver(self):
        if not self.bot:
            return
        from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton
        from aiogram.types import BufferedInputFile
        from .qr import subscription_card
        from .receipt_messages import receipt_message
        for row in self.db.all("SELECT * FROM outbox WHERE sent_at IS NULL AND attempts<10 AND next_attempt<=? ORDER BY id LIMIT 25", (now(),)):
            try:
                buttons = [[InlineKeyboardButton(text='⌂ منوی اصلی',callback_data='menu:home')]]
                if row['dedupe'].startswith('delivery:'):
                    buttons.insert(0,[InlineKeyboardButton(text='📡 سرویس‌های من',callback_data='menu:services')])
                if row['user_id'] in self.store.settings.admin_ids:
                    buttons.insert(0,[InlineKeyboardButton(text='⚙️ مدیریت ربات',callback_data='adm:home')])
                markup=InlineKeyboardMarkup(inline_keyboard=buttons)
                if row['dedupe'].startswith('service-notice:'):
                    _, sid, expiry, total, level = row['dedupe'].split(':')
                    service = self.db.one('SELECT * FROM services WHERE id=?', (sid,))
                    current_level, current_body = service_alert_content(self.store, service) if service else (0, '')
                    if (not service or service['expires_at'] != int(expiry) or service['total_bytes'] != int(total)
                            or not current_level or not service['enabled']):
                        with self.db.transaction() as con:
                            con.execute("UPDATE outbox SET sent_at=?,last_error='superseded' WHERE id=?", (now(), row['id']))
                        continue
                    row['body'] = current_body
                    markup.inline_keyboard.insert(0, [InlineKeyboardButton(text='♻️ تمدید همین اشتراک', callback_data='renew:'+sid, style='success')])
                    markup.inline_keyboard.insert(1, [InlineKeyboardButton(text='📡 جزئیات سرویس',callback_data='svc:'+sid),
                                                      InlineKeyboardButton(text='🔔 تنظیم هشدارها',callback_data='preferences')])
                if row['dedupe'].startswith('admin:') and ':receipt:' in row['dedupe']:
                    receipt,caption,markup=receipt_message(self.store,row['dedupe'].rsplit(':',1)[1])
                    await self.bot.send_photo(row['user_id'],receipt['file_id'],caption=caption,parse_mode='HTML',reply_markup=markup)
                elif row['dedupe'].startswith('delivery:'):
                    order=self.store.order(row['dedupe'].split(':',1)[1])
                    service=self.db.one('SELECT * FROM services WHERE id=?',(order['service_id'],))
                    if not service:
                        raise DomainError('سرویس تحویل پیدا نشد.')
                    link=subscription(self.store.server(service['server_id']),service['sub_id'])
                    if len(link) <= 256:
                        markup.inline_keyboard.insert(0, [InlineKeyboardButton(text='📋 کپی لینک اتصال',copy_text=CopyTextButton(text=link),style='primary')])
                    card=await asyncio.to_thread(subscription_card,link,service)
                    caption=(f"✅ <b>{'تمدید' if order['kind']=='renew' else 'اشتراک'} آماده است</b>\n"
                             f"نام حساب: <code>{escape(service['email'])}</code>\n"
                             f"حجم کل: {service['total_bytes']/GIB:g} گیگابایت\n\n"
                             f"لینک اشتراک:\n<code>{escape(link)}</code>\n\n"
                             'QR اختصاصی را در Happ اسکن کن یا لینک را اضافه کن. لینک و تصویر محرمانه‌اند.')
                    await self.bot.send_photo(row['user_id'],BufferedInputFile(card,filename=service['email']+'.png'),
                                              caption=caption,parse_mode='HTML',reply_markup=markup)
                else:
                    await self.bot.send_message(row["user_id"], row["body"], parse_mode="HTML",reply_markup=markup)
                with self.db.transaction() as con:
                    con.execute("UPDATE outbox SET sent_at=? WHERE id=?", (now(), row["id"]))
                await asyncio.sleep(0.06)
            except TelegramRetryAfter as exc:
                with self.db.transaction() as con:
                    con.execute("UPDATE outbox SET next_attempt=? WHERE id=?", (now() + exc.retry_after + 1, row["id"]))
                break
            except (TelegramForbiddenError, TelegramBadRequest):
                with self.db.transaction() as con:
                    con.execute("UPDATE outbox SET attempts=10,last_error='telegram_rejected' WHERE id=?", (row["id"],))
            except Exception:
                with self.db.transaction() as con:
                    con.execute("UPDATE outbox SET attempts=attempts+1,next_attempt=?,last_error='network' WHERE id=?",
                                (now() + min(3600, 10 * 2 ** row["attempts"]), row["id"]))

    async def refunds(self):
        if not self.bot:
            return
        from aiogram.exceptions import TelegramBadRequest
        for row in self.db.all("SELECT * FROM payments WHERE status='refund_pending' LIMIT 10"):
            try:
                await self.bot.refund_star_payment(user_id=row["user_id"], telegram_payment_charge_id=row["charge_id"])
            except TelegramBadRequest as exc:
                if "CHARGE_ALREADY_REFUNDED" not in str(exc):
                    continue
            except Exception:
                continue
            with self.db.transaction() as con:
                con.execute("UPDATE payments SET status='refunded' WHERE charge_id=?", (row["charge_id"],))
                con.execute("UPDATE orders SET status='refunded' WHERE id=? AND status='refund_pending'", (row["order_id"],))
                notify(con, row["user_id"], f"⭐ بازپرداخت {row['amount']} استار انجام شد.", "star-refund:" + row["charge_id"])

    async def monitor(self):
        # Bound each cycle; oldest snapshots first, so large installations rotate fairly.
        targets={}
        for service in self.db.all("SELECT v.*,COALESCE(l.label,'') label FROM services v LEFT JOIN service_labels l "
                                   "ON l.service_id=v.id WHERE enabled=1 ORDER BY COALESCE(synced_at,0) LIMIT 30"):
            try:
                server=self.store.server(service['server_id'])
                if server.get('all_inbounds') and server['id'] not in targets:
                    async with self.provisioner.panel(server) as panel:
                        targets[server['id']]=await self.provisioner.target_inbounds(panel,server)
                await self.provisioner.sync_service(service,targets.get(server['id']))
                queue_service_alert(self.store, self.store.owned_service(service['id'], service['user_id']))
            except Exception:
                log.warning("Service sync failed id=%s", service["id"])

    async def run(self):
        while True:
            try:
                self.last_tick = now()
                if not self.store.settings.demo:
                    for _ in range(5):
                        if not await self.provisioner.process_one():
                            break
                    await self.refunds()
                    await self.deliver()
                    if now() - self.last_sync >= 300:
                        await self.monitor()
                        self.last_sync = now()
                if now() - self.last_backup >= self.store.settings.backup_hours * 3600:
                    path = self.db.path.parent / "backups" / f"farstar-{now()}.db"
                    await asyncio.to_thread(self.db.backup, path)
                    for old in sorted(path.parent.glob("farstar-*.db"), reverse=True)[14:]:
                        old.unlink()
                    self.last_backup = now()
                with self.db.transaction() as con:
                    con.execute("UPDATE orders SET status='expired' WHERE status IN ('pending','checkout') AND expires_at<?", (now(),))
                if now() - self.last_cleanup >= 3600:
                    await asyncio.to_thread(housekeeping, self.db)
                    self.last_cleanup = now()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Worker cycle failed type=%s", type(exc).__name__)
            await asyncio.sleep(self.store.settings.worker_interval)
