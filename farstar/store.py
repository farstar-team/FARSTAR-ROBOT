import json
import secrets
import sqlite3
import time
import uuid
from html import escape

from .security import Vault


def now():
    return int(time.time())


def uid():
    return secrets.token_hex(8)


class DomainError(Exception):
    pass


def notify(con, user_id, body, dedupe):
    con.execute("INSERT OR IGNORE INTO outbox(user_id,body,dedupe) VALUES (?,?,?)",
                (user_id, body, dedupe))


def audit(con, actor, action, entity, details=None):
    con.execute("INSERT INTO audit(actor,action,entity,details,created_at) VALUES (?,?,?,?,?)",
                (str(actor), action, str(entity), json.dumps(details or {}, ensure_ascii=False), now()))


def credit(con, user_id, amount, kind, reference):
    if con.execute("SELECT 1 FROM ledger WHERE reference=?", (reference,)).fetchone():
        return False
    result = con.execute("UPDATE users SET balance=balance+? WHERE id=? AND balance+? >= 0",
                         (amount, user_id, amount))
    if result.rowcount != 1:
        raise DomainError("موجودی کافی نیست یا کاربر وجود ندارد.")
    con.execute("INSERT INTO ledger(user_id,amount,kind,reference,created_at) VALUES (?,?,?,?,?)",
                (user_id, amount, kind, reference, now()))
    return True


class Store:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.vault = Vault(settings.secret_key)
        with db.transaction() as con:
            previous = con.execute("SELECT value FROM meta WHERE key='currency'").fetchone()
            if previous and previous["value"] != settings.currency:
                business = con.execute("SELECT 1 FROM ledger UNION SELECT 1 FROM orders LIMIT 1").fetchone()
                if business:
                    raise ValueError("CURRENCY cannot change after transactions exist; restore the original currency.")
            con.execute("INSERT OR REPLACE INTO meta VALUES ('currency',?)", (settings.currency,))
            for key in ('card_number', 'card_holder'):
                saved = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                if saved:
                    setattr(settings, key, saved['value'])
            for key in ('trial_enabled','trial_gb','trial_hours','referral_percent'):
                saved = con.execute('SELECT value FROM meta WHERE key=?', ('shop:' + key,)).fetchone()
                if saved:
                    setattr(settings, key, json.loads(saved['value']))

    def save_payment_details(self, card_number, card_holder):
        with self.db.transaction() as con:
            for key, value in [('card_number', card_number), ('card_holder', card_holder)]:
                con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
            audit(con, "admin", "payment.details_save", "card")
        self.settings.card_number = card_number
        self.settings.card_holder = card_holder

    def save_shop_settings(self, values, actor='admin'):
        limits = {'trial_gb':(1,10),'trial_hours':(1,72),'referral_percent':(0,50)}
        for key,value in values.items():
            if key == 'trial_enabled':
                valid = type(value) is bool
            else:
                valid = key in limits and type(value) is int and limits[key][0] <= value <= limits[key][1]
            if not valid:
                raise DomainError('مقدار تنظیم فروشگاه معتبر نیست.')
        with self.db.transaction() as con:
            for key,value in values.items():
                con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('shop:' + key,json.dumps(value)))
            audit(con,actor,'shop.settings','shop',values)
        for key,value in values.items():
            setattr(self.settings,key,value)

    def admins(self, con, body, key):
        for admin_id in self.settings.admin_ids:
            notify(con, admin_id, body, f"admin:{admin_id}:{key}")

    def register(self, user_id, name, username="", referrer=None):
        with self.db.transaction() as con:
            exists = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not exists:
                valid = referrer and referrer != user_id and con.execute(
                    "SELECT 1 FROM users WHERE id=? AND banned=0", (referrer,)).fetchone()
                con.execute("INSERT INTO users(id,name,username,referrer_id,created_at) VALUES (?,?,?,?,?)",
                            (user_id, name[:128], username[:64], referrer if valid else None, now()))
            else:
                con.execute("UPDATE users SET name=?,username=? WHERE id=?",
                            (name[:128], username[:64], user_id))
        return self.user(user_id)

    def user(self, user_id):
        return self.db.one("SELECT * FROM users WHERE id=?", (user_id,))

    def notification_preferences(self, user_id):
        row = self.db.one('SELECT expiry_alerts,quota_alerts FROM user_preferences WHERE user_id=?', (user_id,))
        return {key: bool(row[key]) if row else True for key in ('expiry_alerts', 'quota_alerts')}

    def save_notification_preferences(self, user_id, values):
        if not values or set(values) - {'expiry_alerts', 'quota_alerts'} or any(type(v) is not bool for v in values.values()):
            raise DomainError('تنظیم اعلان معتبر نیست.')
        with self.db.transaction() as con:
            con.execute('INSERT OR IGNORE INTO user_preferences(user_id) VALUES (?)', (user_id,))
            con.execute('UPDATE user_preferences SET ' + ','.join(k + '=?' for k in values) + ' WHERE user_id=?',
                        (*values.values(), user_id))
        return self.notification_preferences(user_id)

    def owned_service(self, service_id, user_id):
        row = self.db.one("SELECT v.*,COALESCE(l.label,'') label FROM services v LEFT JOIN service_labels l "
                          'ON l.service_id=v.id WHERE v.id=? AND v.user_id=?', (service_id, user_id))
        if not row:
            raise DomainError('سرویس پیدا نشد.')
        return row

    def rename_service(self, service_id, user_id, label):
        label = label.strip()
        if len(label) > 40 or any(ord(c) < 32 or c in '\u202a\u202b\u202d\u202e\u202c\u2066\u2067\u2068\u2069' for c in label):
            raise DomainError('نام دلخواه حداکثر ۴۰ نویسه و در یک خط باشد.')
        with self.db.transaction() as con:
            if not con.execute('SELECT 1 FROM services WHERE id=? AND user_id=?', (service_id, user_id)).fetchone():
                raise DomainError('سرویس پیدا نشد.')
            con.execute('INSERT OR REPLACE INTO service_labels VALUES (?,?)', (service_id, label))
        return label

    def set_state(self, user_id, state="", data=None):
        with self.db.transaction() as con:
            con.execute("UPDATE users SET state=?,state_data=? WHERE id=?",
                        (state, json.dumps(data or {}), user_id))

    def accept_terms(self, user_id):
        with self.db.transaction() as con:
            con.execute("UPDATE users SET terms_at=? WHERE id=?", (now(), user_id))

    def plans(self):
        return self.db.all("""SELECT p.*,s.name server_name,s.location FROM plans p
            JOIN servers s ON s.id=p.server_id WHERE p.active=1 AND s.active=1 ORDER BY p.price""")

    def server(self, server_id):
        result = self.db.one("SELECT * FROM servers WHERE id=?", (server_id,))
        if not result:
            raise DomainError("سرور پیدا نشد.")
        for key in ('protocols', 'node_ids'):
            result[key] = json.loads(result.get(key) or '[]')
        return result

    def save_server(self, data, server_id=None, actor="admin"):
        values = dict(data)
        credentials = values.pop("credentials", None)
        from .panels import PROTOCOLS
        if values.get('panel_type', 'sanaei') != 'sanaei':
            raise DomainError('در حال حاضر فقط پنل ثنایی پشتیبانی می‌شود.')
        for key in ('protocols', 'node_ids'):
            if key in values:
                items = values[key]
                if (not isinstance(items, list) or len(items) > 100
                        or (key == 'protocols' and any(p not in PROTOCOLS for p in items))
                        or (key == 'node_ids' and any(type(n) is not int or n < 0 for n in items))):
                    raise DomainError('فیلتر پروتکل یا نود معتبر نیست.')
                values[key] = json.dumps(sorted(set(items)))
        with self.db.transaction() as con:
            if server_id:
                existing = con.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
                if not existing:
                    raise DomainError("سرور پیدا نشد.")
                # Changing an inbound or subscription host after selling would break existing services.
                in_use = con.execute("SELECT 1 FROM services WHERE server_id=? UNION SELECT 1 FROM orders WHERE server_id=? LIMIT 1",
                                     (server_id, server_id)).fetchone()
                if in_use and any(values.get(k, existing[k]) != existing[k] for k in ("inbound_id", "subscription_url", "panel_type", "protocols", "node_ids")):
                    raise DomainError("این سرور سفارش دارد؛ برای تغییر اینباند یا آدرس اشتراک، سرور تازه بسازید.")
                if credentials:
                    values["credentials"] = self.vault.seal(credentials)
                columns = ",".join(f"{k}=?" for k in values)
                con.execute(f"UPDATE servers SET {columns} WHERE id=?", (*values.values(), server_id))
            else:
                if not credentials or not (credentials.get("token") or
                                           (credentials.get("username") and credentials.get("password"))):
                    raise DomainError("توکن API یا نام کاربری و رمز پنل لازم است.")
                values["credentials"] = self.vault.seal(credentials)
                values["created_at"] = now()
                cols = ",".join(values)
                marks = ",".join("?" for _ in values)
                server_id = con.execute(f"INSERT INTO servers({cols}) VALUES ({marks})",
                                        tuple(values.values())).lastrowid
            audit(con, actor, "server.save", server_id)
        return server_id

    def save_plan(self, data, plan_id=None):
        values = dict(data)
        for key, upper in [('days', 1095), ('volume_gb', 100000), ('price', 100000000)]:
            if type(values.get(key)) is not int or not 1 <= values[key] <= upper:
                raise DomainError('حجم، مدت یا قیمت پلن معتبر نیست.')
        if not 2 <= len(values.get('name', '')) <= 80 or not 0 <= values.get('ip_limit', 2) <= 100:
            raise DomainError('نام پلن یا محدودیت IP معتبر نیست.')
        with self.db.transaction() as con:
            if not con.execute("SELECT 1 FROM servers WHERE id=?", (values["server_id"],)).fetchone():
                raise DomainError("ابتدا یک سرور اضافه کنید.")
            if plan_id:
                if not con.execute("SELECT 1 FROM plans WHERE id=?", (plan_id,)).fetchone():
                    raise DomainError("پلن پیدا نشد.")
                con.execute("UPDATE plans SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                            (*values.values(), plan_id))
            else:
                values["created_at"] = now()
                plan_id = con.execute("INSERT INTO plans(" + ",".join(values) + ") VALUES (" +
                                      ",".join("?" for _ in values) + ")", tuple(values.values())).lastrowid
            audit(con, "admin", "plan.save", plan_id)
        return plan_id

    def create_code(self, kind, code, value, max_uses, expires_at, actor='admin'):
        import re
        code = code.upper().strip()
        if not re.fullmatch(r'[A-Z0-9_-]{3,32}', code) or not 1 <= max_uses <= 100000 or expires_at <= now():
            raise DomainError('کد، تعداد استفاده یا تاریخ انقضا معتبر نیست.')
        if not 1 <= value <= (100 if kind == 'coupon' else 100000000):
            raise DomainError('مقدار کد معتبر نیست.')
        with self.db.transaction() as con:
            if kind == 'coupon':
                con.execute('INSERT INTO coupons(code,percent,max_uses,expires_at) VALUES (?,?,?,?)',
                            (code, value, max_uses, expires_at))
            elif kind == 'gift':
                con.execute('INSERT INTO gifts VALUES (?,?,?,?)', (code, value, max_uses, expires_at))
            else:
                raise DomainError('نوع کد معتبر نیست.')
            audit(con, actor, 'code.create', code)

    def create_order(self, user_id, plan_id=None, coupon="", service_id=None, trial=False):
        from .licensing import client
        client(self).require_sales()
        timestamp = now()
        order_id = uid()
        with self.db.transaction() as con:
            con.execute("UPDATE orders SET status='expired' WHERE status IN ('pending','checkout') AND expires_at < ?",
                        (timestamp,))
            user = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not user or user["banned"]:
                raise DomainError("حساب در دسترس نیست.")
            if not user["terms_at"]:
                raise DomainError("ابتدا قوانین فروشگاه را بپذیرید.")
            pending_count = con.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status IN ('pending','checkout','awaiting_review')",
                                        (user_id,)).fetchone()[0]
            if pending_count >= 3:
                raise DomainError("سه فاکتور باز دارید؛ ابتدا یکی را پرداخت کنید یا منتظر پایان مهلت بمانید.")
            if trial:
                if not self.settings.trial_enabled or user["trial_used"]:
                    raise DomainError("تست رایگان فقط یک بار برای هر حساب فعال است.")
                server = con.execute("SELECT * FROM servers WHERE active=1 AND trial=1 ORDER BY id LIMIT 1").fetchone()
                if not server:
                    raise DomainError("در حال حاضر سرور تست موجود نیست.")
                plan = {"name": "تست رایگان", "days": self.settings.trial_hours / 24,
                        "volume_gb": self.settings.trial_gb, "ip_limit": 1, "price": 0}
                con.execute("UPDATE users SET trial_used=1 WHERE id=?", (user_id,))
            else:
                plan = con.execute("SELECT * FROM plans WHERE id=? AND active=1", (plan_id,)).fetchone()
                if not plan:
                    raise DomainError("پلن موجود نیست.")
                server = con.execute("SELECT * FROM servers WHERE id=? AND active=1", (plan["server_id"],)).fetchone()
                if not server:
                    raise DomainError("فروش این سرور موقتاً متوقف است.")
            service = None
            if service_id:
                service = con.execute("SELECT * FROM services WHERE id=? AND user_id=?",
                                      (service_id, user_id)).fetchone()
                if not service or service["is_trial"] or service["server_id"] != server["id"]:
                    raise DomainError("این سرویس با پلن انتخابی قابل تمدید نیست.")
                if con.execute("SELECT 1 FROM orders WHERE service_id=? AND status IN ('pending','checkout','awaiting_review','paid','processing','failed','refund_pending')",
                               (service_id,)).fetchone():
                    raise DomainError("برای این سرویس یک سفارش تمدید باز دارید.")
            else:
                occupied = con.execute("SELECT COUNT(*) FROM services WHERE server_id=? AND expires_at>?",
                                       (server["id"], timestamp)).fetchone()[0]
                reserved = con.execute("SELECT COUNT(*) FROM orders WHERE server_id=? AND kind!='renew' AND status IN ('pending','checkout','awaiting_review','paid','processing','failed')",
                                       (server["id"],)).fetchone()[0]
                if occupied + reserved >= server["capacity"]:
                    raise DomainError("ظرفیت این سرور تکمیل است.")
            amount = plan["price"]
            code = coupon.strip().upper() or None
            if code:
                c = con.execute("SELECT * FROM coupons WHERE code=? AND active=1 AND expires_at>?",
                                (code, timestamp)).fetchone()
                used = con.execute("SELECT COUNT(*) FROM orders WHERE coupon=? AND status NOT IN ('expired','refunded','cancelled')",
                                   (code,)).fetchone()[0]
                own = con.execute("SELECT 1 FROM orders WHERE coupon=? AND user_id=? AND status NOT IN ('expired','refunded','cancelled')",
                                  (code, user_id)).fetchone()
                if not c or used >= c["max_uses"] or own:
                    raise DomainError("کد تخفیف نامعتبر، استفاده‌شده یا منقضی است.")
                amount = (amount * (100 - c["percent"]) + 99) // 100
            snapshot = {"name": plan["name"], "days": plan["days"], "volume_gb": plan["volume_gb"],
                        "ip_limit": plan["ip_limit"], "email": service["email"] if service else f"fs-{order_id}",
                        "sub_id": service["sub_id"] if service else secrets.token_urlsafe(24),
                        "client_id": str(uuid.uuid4()), "password": secrets.token_urlsafe(24)}
            status = "paid" if trial or amount == 0 else "pending"
            kind = "trial" if trial else ("renew" if service else "purchase")
            con.execute("""INSERT INTO orders(id,user_id,plan_id,server_id,service_id,kind,price,currency,
                status,coupon,snapshot,created_at,expires_at,paid_at,payment_method)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (order_id, user_id, plan_id, server["id"], service_id, kind, amount,
                         self.settings.currency, status, code, json.dumps(snapshot), timestamp, timestamp + 1800,
                         timestamp if status == "paid" else None, "free" if status == "paid" else None))
            audit(con, user_id, "order.create", order_id)
        return self.order(order_id, user_id)

    def order(self, order_id, user_id=None):
        row = self.db.one("SELECT * FROM orders WHERE id=?" + (" AND user_id=?" if user_id else ""),
                          (order_id, user_id) if user_id else (order_id,))
        if not row:
            raise DomainError("سفارش پیدا نشد.")
        return row

    def reserve_client_name(self, order_id, observed_max=0, advance=False):
        """Reserve at provisioning, under SQLite's write lock; retain across retries."""
        import re
        prefix = self.settings.client_name_prefix
        pattern = re.compile(re.escape(prefix) + r'(\d+)', re.I)
        key = 'client-sequence:' + prefix.lower()
        with self.db.transaction() as con:
            order = con.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            if not order or order['kind'] == 'renew' or order['status'] not in {'paid','processing'}:
                raise DomainError('رزرو نام فقط برای ساخت سفارش پرداخت‌شده ممکن است.')
            snapshot = json.loads(order['snapshot'])
            if order['target_json'] or (snapshot.get('name_reserved') and not advance):
                return snapshot
            counter = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            highest = max(observed_max, int(counter['value']) if counter else 0,
                          self.settings.client_number_start - 1)
            for row in con.execute("SELECT email FROM services UNION SELECT json_extract(snapshot,'$.email') email FROM orders"):
                match = pattern.fullmatch(row['email'] or '')
                if match:
                    highest = max(highest, int(match[1]))
            number = highest + 1
            snapshot.update(email=prefix + str(number), name_reserved=True)
            con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key,str(number)))
            con.execute('UPDATE orders SET snapshot=? WHERE id=?', (json.dumps(snapshot),order_id))
            audit(con,'worker','client.name_reserve',order_id,{'email':snapshot['email']})
            return snapshot

    def pay_wallet(self, order_id, user_id):
        with self.db.transaction() as con:
            order = con.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user_id)).fetchone()
            if not order:
                raise DomainError("سفارش پیدا نشد.")
            if order["status"] in {"paid", "processing", "completed"}:
                return
            if order["status"] != "pending" or order["expires_at"] < now():
                raise DomainError("مهلت پرداخت تمام شده است؛ سفارش تازه بسازید.")
            if con.execute("SELECT banned FROM users WHERE id=?", (user_id,)).fetchone()[0]:
                raise DomainError("حساب مسدود است.")
            credit(con, user_id, -order["price"], "purchase", "order:" + order_id)
            con.execute("UPDATE orders SET status='paid',paid_at=?,payment_method='wallet' WHERE id=?",
                        (now(), order_id))
            audit(con, user_id, "order.wallet_pay", order_id)

    def cancel_order(self, order_id, user_id):
        with self.db.transaction() as con:
            changed = con.execute("UPDATE orders SET status='cancelled' WHERE id=? AND user_id=? AND status='pending'",
                                  (order_id, user_id)).rowcount
            if not changed:
                raise DomainError("این فاکتور قابل لغو نیست؛ وضعیت پرداخت را بررسی کنید.")
            audit(con, user_id, "order.cancel", order_id)

    def precheckout(self, order_id, user_id, amount, currency):
        from .licensing import client
        client(self).require_sales()
        with self.db.transaction() as con:
            row = con.execute("SELECT o.*,u.banned FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=? AND o.user_id=?",
                              (order_id, user_id)).fetchone()
            if (not row or row["status"] != "pending" or row["expires_at"] < now()
                    or row["price"] != amount or row["currency"] != currency or currency != "XTR" or row["banned"]):
                raise DomainError("فاکتور قابل پرداخت نیست؛ از منوی خرید، فاکتور تازه بسازید.")
            con.execute("UPDATE orders SET status='checkout',expires_at=? WHERE id=?", (now() + 900, order_id))

    def settle_stars(self, order_id, user_id, amount, currency, charge_id):
        with self.db.transaction() as con:
            if con.execute("SELECT 1 FROM payments WHERE charge_id=?", (charge_id,)).fetchone():
                return
            order = con.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, user_id)).fetchone()
            if not order:
                raise DomainError("فاکتور پرداخت پیدا نشد؛ با پشتیبانی تماس بگیرید.")
            valid = (order["status"] in {"pending", "checkout", "expired"} and currency == "XTR"
                     and amount == order["price"] and currency == order["currency"])
            con.execute("INSERT INTO payments VALUES (?,?,?,?,?,?)",
                        (charge_id, order_id, user_id, amount, "settled" if valid else "refund_pending", now()))
            if valid:
                con.execute("UPDATE orders SET status='paid',paid_at=?,payment_method='stars',charge_id=? WHERE id=?",
                            (now(), charge_id, order_id))
            else:
                self.admins(con, "یک پرداخت تکراری یا نامطابق برای بازپرداخت ثبت شد.", "duplicate:" + charge_id)
            audit(con, user_id, "order.stars_settle", order_id, {"amount": amount, "valid": valid})

    def receipt_order(self, order_id, user_id):
        order = self.order(order_id,user_id)
        user = self.user(user_id)
        if (not user or user['banned'] or order['status'] != 'pending' or order['expires_at'] < now()
                or order['currency'] != 'IRT' or order['price'] <= 0):
            raise DomainError('این سفارش قابل پرداخت کارت‌به‌کارت نیست؛ وضعیت فاکتور را بررسی کنید.')
        return order

    def add_receipt(self, user_id, amount, file_id, order_id, file_unique_id=None, image_hash=None):
        if not self.settings.manual_payments or self.settings.currency != "IRT":
            raise DomainError("پرداخت دستی فعال نیست.")
        receipt_id = uid()
        try:
            with self.db.transaction() as con:
                order = con.execute('SELECT o.*,u.banned FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=? AND o.user_id=?',
                                    (order_id,user_id)).fetchone()
                if (not order or order['banned'] or order['status'] != 'pending' or order['expires_at'] < now()
                        or order['currency'] != 'IRT' or order['price'] != amount or amount <= 0):
                    raise DomainError('مبلغ یا وضعیت سفارش با رسید مطابقت ندارد؛ فاکتور را بررسی کنید.')
                con.execute("INSERT INTO receipts(id,user_id,amount,file_id,created_at,order_id,file_unique_id,image_hash) VALUES (?,?,?,?,?,?,?,?)",
                            (receipt_id, user_id, amount, file_id, now(),order_id,file_unique_id or None,image_hash or None))
                con.execute("UPDATE orders SET status='awaiting_review',payment_method='card' WHERE id=?",(order_id,))
                self.admins(con, f"🧾 رسید سفارش <code>{order_id}</code>\nمبلغ: {amount:,} تومان",
                            "receipt:" + receipt_id)
        except sqlite3.IntegrityError as exc:
            raise DomainError("این تصویر قبلاً ثبت شده یا سفارش یک رسید در انتظار بررسی دارد.") from exc
        return receipt_id

    def review_receipt(self, receipt_id, approve, actor):
        with self.db.transaction() as con:
            row = con.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise DomainError("رسید پیدا نشد یا قبلاً بررسی شده است.")
            if approve:
                order = con.execute('SELECT o.*,u.banned FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=?', (row['order_id'],)).fetchone()
                if (not order or order['user_id'] != row['user_id'] or order['status'] != 'awaiting_review'
                        or order['price'] != row['amount'] or order['currency'] != 'IRT' or order['banned']):
                    raise DomainError('این رسید به سفارش قابل تحویل متصل نیست؛ سفارش یا مسدودی کاربر را بررسی کنید.')
                con.execute("UPDATE orders SET status='paid',paid_at=?,payment_method='card',charge_id=?,last_error=NULL,next_attempt=0 WHERE id=?",
                            (now(),'receipt:'+receipt_id,row['order_id']))
            elif row['order_id']:
                con.execute("UPDATE orders SET status='pending',expires_at=?,payment_method=NULL WHERE id=? AND status='awaiting_review'",
                            (now()+1800,row['order_id']))
            status = "approved" if approve else "rejected"
            con.execute("UPDATE receipts SET status=?,reviewed_by=?,reviewed_at=? WHERE id=?", (status, str(actor), now(), receipt_id))
            notify(con, row["user_id"], "✅ پرداخت تأیید شد؛ اشتراک همراه لینک و QR همین‌جا برایت ارسال می‌شود." if approve else
                   "رسید تأیید نشد. از سفارش‌های من می‌توانی رسید درست را دوباره ارسال کنی یا به پشتیبانی پیام بدهی.", "receipt-result:" + receipt_id)
            audit(con, actor, "receipt." + status, receipt_id)

    def adjust_balance(self, user_id, amount, reference):
        with self.db.transaction() as con:
            credit(con, user_id, amount, "admin", "admin:" + reference)
            audit(con, "admin", "wallet.adjust", user_id, {"amount": amount})

    def redeem(self, user_id, code):
        code = code.strip().upper()
        with self.db.transaction() as con:
            gift = con.execute("SELECT * FROM gifts WHERE code=? AND expires_at>?", (code, now())).fetchone()
            used = con.execute("SELECT COUNT(*) FROM gift_claims WHERE code=?", (code,)).fetchone()[0]
            own = con.execute("SELECT 1 FROM gift_claims WHERE code=? AND user_id=?", (code, user_id)).fetchone()
            if not gift or used >= gift["max_uses"] or own:
                raise DomainError("کد هدیه نامعتبر، استفاده‌شده یا منقضی است.")
            credit(con, user_id, gift["amount"], "gift", f"gift:{code}:{user_id}")
            con.execute("INSERT INTO gift_claims VALUES (?,?)", (code, user_id))
            return gift["amount"]

    def ticket(self, user_id, text, ticket_id=None, admin=False):
        with self.db.transaction() as con:
            if ticket_id:
                ticket = con.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
                if not ticket or (not admin and ticket["user_id"] != user_id):
                    raise DomainError("تیکت پیدا نشد.")
                con.execute("UPDATE tickets SET status=?,updated_at=? WHERE id=?",
                            ("answered" if admin else "open", now(), ticket_id))
            else:
                if con.execute("SELECT COUNT(*) FROM tickets WHERE user_id=? AND status!='closed'", (user_id,)).fetchone()[0] >= 5:
                    raise DomainError("حداکثر پنج تیکت باز می‌توانید داشته باشید.")
                ticket_id = con.execute("INSERT INTO tickets(user_id,subject,created_at,updated_at) VALUES (?,?,?,?)",
                                        (user_id, text[:90], now(), now())).lastrowid
            con.execute("INSERT INTO ticket_messages(ticket_id,sender,body,created_at) VALUES (?,?,?,?)",
                        (ticket_id, "admin" if admin else "user", text, now()))
            if admin:
                notify(con, ticket["user_id"], f"💬 پاسخ پشتیبانی · تیکت {ticket_id}\n\n{escape(text)}",
                       f"ticket-reply:{ticket_id}:{secrets.token_hex(6)}")
                audit(con, user_id, "ticket.reply", ticket_id)
            else:
                self.admins(con, f"💬 تیکت {ticket_id}\n{escape(text[:1000])}\n/reply {ticket_id} متن پاسخ",
                            f"ticket:{ticket_id}:{secrets.token_hex(6)}")
            return ticket_id

    def retry(self, order_id):
        with self.db.transaction() as con:
            if con.execute("UPDATE orders SET status='paid',attempts=0,next_attempt=0,lease_until=0 WHERE id=? AND status='failed'",
                           (order_id,)).rowcount != 1:
                raise DomainError("فقط سفارش ناموفق قابل تلاش دوباره است.")
            audit(con, "admin", "order.retry", order_id)

    def broadcast(self, body, campaign_id=None):
        from .broadcasts import queue
        return queue(self.db, body, campaign_id)

    def stats(self):
        counts = self.db.one("""SELECT
          (SELECT COUNT(*) FROM users) users,
          (SELECT COUNT(*) FROM services WHERE enabled=1 AND expires_at>? AND used_bytes<total_bytes) active_services,
          (SELECT COUNT(*) FROM orders WHERE status='completed' AND price>0) sales,
          (SELECT COALESCE(SUM(price),0) FROM orders WHERE status='completed' AND price>0) revenue,
          (SELECT COALESCE(SUM(price),0) FROM orders WHERE status='completed' AND completed_at>=?) today_revenue,
          (SELECT COUNT(*) FROM orders WHERE status='failed') failed_orders,
          (SELECT COUNT(*) FROM receipts WHERE status='pending') pending_receipts,
          (SELECT COUNT(*) FROM tickets WHERE status='open') open_tickets,
          (SELECT COALESCE(SUM(used_bytes),0) FROM services) traffic_bytes""",
                             (now(), now() - 86400))
        counts["chart"] = self.db.all("""SELECT date(completed_at,'unixepoch','+3 hours','+30 minutes') day,
             SUM(price) amount,COUNT(*) orders FROM orders WHERE status='completed' AND price>0
             AND completed_at>=? GROUP BY day ORDER BY day""", (now() - 13 * 86400,))
        counts["recent_orders"] = self.db.all("""SELECT o.id,o.user_id,u.name,o.price,o.kind,o.status,o.created_at,
            json_extract(o.snapshot,'$.name') plan_name FROM orders o JOIN users u ON u.id=o.user_id
            ORDER BY o.created_at DESC LIMIT 7""")
        return counts
