"""Customer guidance and read-only shop reports. No panel credentials or subscription URLs."""
import contextlib
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from html import escape

from .store import now, notify

GIB = 1024 ** 3
TEHRAN = ZoneInfo('Asia/Tehran')


def service_health(service, timestamp=None):
    timestamp = now() if timestamp is None else timestamp
    remaining = max(0, service['total_bytes'] - service['used_bytes'])
    seconds = max(0, service['expires_at'] - timestamp)
    checks = [
        {'label': 'فعال بودن حساب', 'ok': bool(service['enabled'])},
        {'label': 'اعتبار زمانی', 'ok': seconds > 0},
        {'label': 'حجم باقی‌مانده', 'ok': remaining > 0},
    ]
    if not service['enabled']:
        status, title, advice = 'disabled', 'حساب غیرفعال است', 'از پشتیبانی بخواه وضعیت حساب را بررسی کند.'
    elif not seconds:
        status, title, advice = 'expired', 'اعتبار زمانی تمام شده', 'برای ادامه اتصال، همین اشتراک را تمدید کن.'
    elif not remaining:
        status, title, advice = 'exhausted', 'حجم اشتراک تمام شده', 'با تمدید، حجم تازه به همین اشتراک اضافه می‌شود.'
    elif remaining <= service['total_bytes'] * .05:
        status, title, advice = 'low_quota', 'کمتر از ۵٪ حجم باقی مانده', 'برای جلوگیری از قطع اتصال، تمدید را زودتر انجام بده.'
    elif seconds <= 86400:
        status, title, advice = 'expiring', 'کمتر از یک روز اعتبار باقی مانده', 'تمدید زودتر، زمان باقی‌مانده را حفظ می‌کند.'
    else:
        status, title, advice = 'healthy', 'حجم و اعتبار حساب آماده است', 'در Happ اشتراک را بروزرسانی کن، یک سرور دیگر انتخاب کن و دوباره متصل شو.'
    fresh = bool(service.get('synced_at') and timestamp - service['synced_at'] < 600)
    return {'status': status, 'title': title, 'advice': advice, 'checks': checks, 'fresh': fresh,
            'remaining_gb': round(remaining / GIB, 2), 'remaining_hours': round(seconds / 3600, 1),
            'synced_at': service.get('synced_at'),
            'note': 'این بررسی وضعیت حساب است؛ کیفیت مسیر اینترنت دستگاه را اندازه‌گیری نمی‌کند.'}


def order_progress(db, order):
    receipt = db.one('SELECT id,status,created_at,reviewed_at FROM receipts WHERE order_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
                     (order['id'],))
    paid = bool(order.get('paid_at'))
    if order['kind'] == 'trial':
        return {'receipt': None, 'steps': [
            {'label': 'درخواست تست رایگان', 'done': True, 'at': order['created_at']},
            {'label': 'آماده شدن اشتراک', 'done': order['status'] == 'completed', 'at': order.get('completed_at')}]}
    receipt_step = order['currency'] == 'IRT' and order.get('payment_method') in (None, 'card')
    return {'receipt': receipt, 'steps': [
        {'label': 'ثبت سفارش', 'done': True, 'at': order['created_at']},
        {'label': 'ارسال فیش' if receipt_step else 'پرداخت',
         'done': bool(receipt) or paid, 'at': receipt['created_at'] if receipt else order.get('paid_at')},
        {'label': 'تأیید پرداخت', 'done': paid, 'at': order.get('paid_at')},
        {'label': 'آماده شدن اشتراک', 'done': order['status'] == 'completed', 'at': order.get('completed_at')},
    ]}


def sales_report(db, days=7, timestamp=None):
    timestamp = now() if timestamp is None else timestamp
    days = int(days)
    if days not in (1, 7, 30):
        raise ValueError('Report period must be 1, 7 or 30 calendar days')
    local = datetime.fromtimestamp(timestamp, TEHRAN)
    start_date = local.date() - timedelta(days=days - 1)
    start = int(datetime.combine(start_date, datetime.min.time(), TEHRAN).timestamp())
    # Revenue means delivered sales, not pending invoices or collected-but-undelivered payments.
    totals = db.one("SELECT COUNT(*) sales,COALESCE(SUM(price),0) revenue,"
                    "COALESCE(SUM(CASE WHEN kind='renew' THEN price ELSE 0 END),0) renewal_revenue,"
                    "COUNT(DISTINCT user_id) buyers FROM orders WHERE status='completed' AND price>0 "
                    'AND completed_at>=? AND completed_at<=?', (start, timestamp))
    chart = db.all("SELECT date(completed_at,'unixepoch','+3 hours','+30 minutes') day,SUM(price) amount,COUNT(*) orders "
                   "FROM orders WHERE status='completed' AND price>0 AND completed_at BETWEEN ? AND ? GROUP BY day", (start, timestamp))
    by_day = {r['day']: r for r in chart}
    totals['chart'] = [by_day.get((start_date + timedelta(days=n)).isoformat(),
                      {'day': (start_date + timedelta(days=n)).isoformat(), 'amount': 0, 'orders': 0}) for n in range(days)]
    totals['top_plans'] = db.all("SELECT json_extract(snapshot,'$.name') name,COUNT(*) sales,SUM(price) amount FROM orders "
                                "WHERE status='completed' AND price>0 AND completed_at BETWEEN ? AND ? "
                                'GROUP BY name ORDER BY amount DESC LIMIT 5', (start, timestamp))
    totals.update(days=days, starts_at=start, ends_at=timestamp, timezone='Asia/Tehran')
    return totals


def attention(db):
    timestamp = now()
    receipts = db.all("SELECT r.id,r.order_id,r.amount,r.created_at,u.name FROM receipts r JOIN users u ON u.id=r.user_id "
                      "WHERE r.status='pending' ORDER BY r.created_at LIMIT 15")
    orders = db.all("SELECT o.id,o.status,o.price,o.created_at,u.name FROM orders o JOIN users u ON u.id=o.user_id "
                    "WHERE o.status IN ('failed','refund_pending') OR (o.status IN ('paid','processing') AND o.paid_at<?) "
                    'ORDER BY o.created_at LIMIT 15', (timestamp - 600,))
    tickets = db.all("SELECT t.id,t.subject,t.updated_at,u.name FROM tickets t JOIN users u ON u.id=t.user_id "
                     "WHERE t.status='open' ORDER BY t.updated_at LIMIT 15")
    counts = db.one("SELECT (SELECT COUNT(*) FROM receipts WHERE status='pending') receipts,"
                    "(SELECT COUNT(*) FROM orders WHERE status IN ('failed','refund_pending') OR "
                    "(status IN ('paid','processing') AND paid_at<?)) orders,"
                    "(SELECT COUNT(*) FROM tickets WHERE status='open') tickets,"
                    "(SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL AND attempts>=10) undelivered", (timestamp - 600,))
    return {'counts': counts, 'receipts': receipts, 'orders': orders, 'tickets': tickets,
            'oldest_receipt_minutes': (timestamp - receipts[0]['created_at']) // 60 if receipts else 0}


def storage_status(db):
    disk = shutil.disk_usage(db.path.parent)
    backups = list((db.path.parent / 'backups').glob('farstar-*.db'))
    def size(path):
        with contextlib.suppress(OSError):
            return path.stat().st_size
        return 0
    return {'disk_total_bytes': disk.total, 'disk_free_bytes': disk.free,
            'database_bytes': sum(size(db.path.with_name(db.path.name + suffix)) for suffix in ('', '-wal', '-shm')),
            'backup_bytes': sum(size(p) for p in backups), 'backup_count': len(backups),
            'receipt_image_bytes': 0, 'receipt_storage': 'telegram_file_id', 'backup_limit': 14,
            'upload_limit_mb': 5}


def housekeeping(db):
    # Keep all receipts, payments, ledger entries and outbox dedupe keys for accounting and retry safety.
    with db.transaction() as con:
        for table in ('sessions', 'app_sessions'):
            con.execute(f'DELETE FROM {table} WHERE expires_at<?', (now(),))
        con.execute('DELETE FROM rate_limits WHERE reset_at<?', (now() - 86400,))
        con.execute('DELETE FROM upload_requests WHERE receipt_id IS NULL AND lease_until<?', (now() - 86400,))


def service_alert_content(store, service):
    prefs = store.notification_preferences(service['user_id'])
    user = store.user(service['user_id'])
    if not user or user['banned'] or service['is_trial']:
        return 0, ''
    left = service['expires_at'] - now()
    ratio = service['used_bytes'] / service['total_bytes'] if service['total_bytes'] else 0
    time_level = (3 if left <= 0 else 2 if left <= 86400 else 1 if left <= 3 * 86400 else 0) if prefs['expiry_alerts'] else 0
    quota_level = (3 if ratio >= 1 else 2 if ratio >= .95 else 1 if ratio >= .8 else 0) if prefs['quota_alerts'] else 0
    level = max(time_level, quota_level)
    if not level:
        return 0, ''
    name = escape(service.get('label') or service['email'])
    lines = [f"{'🔴' if level == 3 else '⏳'} <b>یادآور اشتراک {name}</b>"]
    if time_level:
        lines.append('اعتبار زمانی تمام شده است.' if left <= 0 else f'زمان باقی‌مانده: {max(1, int(left / 3600))} ساعت')
    if quota_level:
        lines.append(f"حجم باقی‌مانده: {max(0,service['total_bytes']-service['used_bytes']) / GIB:.2f} گیگابایت")
    lines.append('با تمدید، همان لینک و حساب را نگه می‌داری. حجم و زمان تازه به اعتبار قبلی اضافه می‌شود.')
    return level, '\n'.join(lines)


def queue_service_alert(store, service):
    level, body = service_alert_content(store, service)
    if not level:
        return
    epoch = f"{service['expires_at']}:{service['total_bytes']}"
    with store.db.transaction() as con:
        previous = con.execute('SELECT * FROM service_alert_state WHERE service_id=?', (service['id'],)).fetchone()
        if previous and previous['epoch'] == epoch and previous['level'] >= level:
            return
        # Do not repeat the old 72h/80% reminder during upgrade.
        legacy_epoch = f"{service['expires_at'] * 1000}:{service['total_bytes']}"
        legacy = con.execute('SELECT 1 FROM outbox WHERE dedupe IN (?,?)',
                             (f"expiry:{service['id']}:{legacy_epoch}", f"quota:{service['id']}:{legacy_epoch}")).fetchone()
        con.execute('INSERT OR REPLACE INTO service_alert_state VALUES (?,?,?)', (service['id'], epoch, level))
        if level > 1 or not legacy:
            notify(con, service['user_id'], body, f"service-notice:{service['id']}:{epoch}:{level}")
