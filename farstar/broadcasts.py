"""Legacy broadcast recipients are contacts, not customer accounts or wallet balances."""
import hashlib
import json
import re
from html import escape

from .store import DomainError, audit, notify, now, uid

AUDIENCE = """SELECT id FROM users WHERE banned=0 UNION
 SELECT c.user_id id FROM broadcast_contacts c LEFT JOIN users u ON u.id=c.user_id
 WHERE u.id IS NULL OR u.banned=0"""


def audience(db):
    return db.one('SELECT COUNT(*) count FROM ('+AUDIENCE+')')['count']


def import_contacts(db, user_ids, source_sha256, actor='admin'):
    if not re.fullmatch(r'[a-f0-9]{64}', source_sha256):
        raise DomainError('شناسه فایل ورودی معتبر نیست.')
    if not user_ids or len(user_ids)>100000 or any(type(i) is not int or not 0<i<2**52 for i in user_ids):
        raise DomainError('فهرست شناسه‌های تلگرام معتبر نیست.')
    unique = set(user_ids)
    with db.transaction() as con:
        before = con.total_changes
        con.executemany('INSERT OR IGNORE INTO broadcast_contacts VALUES (?,?,?)',
                        ((i,source_sha256,now()) for i in sorted(unique)))
        added = con.total_changes-before
        count = con.execute('SELECT COUNT(*) FROM ('+AUDIENCE+')').fetchone()[0]
        audit(con,actor,'broadcast.contacts_import',source_sha256,{'unique':len(unique),'added':added,'recipients':count})
    return {'unique':len(unique),'added':added,'recipients':count}


def queue(db, body, campaign_id=None):
    body = body.strip()
    if not 1<=len(body)<=3000:
        raise DomainError('متن پیام باید بین ۱ تا ۳۰۰۰ نویسه باشد.')
    key = campaign_id or uid()
    if not re.fullmatch(r'[a-f0-9]{16}',key):
        raise DomainError('شناسه ارسال معتبر نیست.')
    checksum = hashlib.sha256(body.encode()).hexdigest()
    with db.transaction() as con:
        previous = con.execute("SELECT details FROM audit WHERE action='broadcast' AND entity=?",(key,)).fetchone()
        if previous:
            details = json.loads(previous['details'])
            if details.get('body_sha256')!=checksum:
                raise DomainError('این شناسه ارسال قبلاً برای متن دیگری استفاده شده است.')
            return details['recipients']
        recipients = con.execute(AUDIENCE).fetchall()
        for user in recipients:
            notify(con,user['id'],escape(body),f"broadcast:{key}:{user['id']}")
        audit(con,'admin','broadcast',key,{'recipients':len(recipients),'body_sha256':checksum})
    return len(recipients)


def status(db, campaign_id=None):
    if campaign_id is None:
        row = db.one("SELECT entity FROM audit WHERE action='broadcast' ORDER BY id DESC LIMIT 1")
        if not row:
            raise DomainError('هنوز پیام همگانی ارسال نشده است.')
        campaign_id = row['entity']
    if not re.fullmatch(r'[a-f0-9]{16}',campaign_id):
        raise DomainError('شناسه ارسال معتبر نیست.')
    event = db.one("SELECT created_at FROM audit WHERE action='broadcast' AND entity=?",(campaign_id,))
    if not event:
        raise DomainError('گزارش ارسال پیدا نشد.')
    counts = db.one("SELECT COUNT(*) total,COALESCE(SUM(sent_at IS NOT NULL),0) sent,"
                    "COALESCE(SUM(sent_at IS NULL AND attempts<10),0) pending,"
                    "COALESCE(SUM(sent_at IS NULL AND attempts>=10),0) failed "
                    'FROM outbox WHERE dedupe LIKE ?',(f'broadcast:{campaign_id}:%',))
    return {'id':campaign_id,'created_at':event['created_at'],**counts}
