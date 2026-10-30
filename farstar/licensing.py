"""Client of the FARSTAR license service. The private issuer is not distributed."""
import asyncio
import base64
import json
import logging
import hashlib
import socket
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .store import DomainError, now

ISSUER_URL = 'https://sub.mystics.top/license-api'
ISSUER_BOT = 'FARSTAR_VPN_BOT'
PUBLIC_KEY = '11fd735b8e7db7c4a76ea4fbc791a9c60739c7fdb7404b3493ee97548ba261ac'
INSTALL_COMMAND = 'bash <(curl -fsSL https://raw.githubusercontent.com/farstar-team/FARSTAR-ROBOT/main/install.sh)'
STATUS_LABELS = {'active':'فعال', 'pending':'منتظر اولین نصب', 'revoked':'غیرفعال شده', 'expired':'منقضی',
                 'issuer':'مرجع اصلی', 'development':'محیط توسعه', 'missing':'فعال نشده', 'invalid':'نامعتبر'}
log = logging.getLogger(__name__)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def machine_fingerprint():
    identity = Path('/etc/machine-id')
    raw = identity.read_bytes().strip() if identity.is_file() else socket.gethostname().encode()
    return hashlib.sha256(b'farstar-install-v1:' + raw).hexdigest()


def verify_envelope(envelope, installation_id, bot_id, timestamp=None):
    """Only a lease signed by our issuer and matching BOTH identities is usable."""
    try:
        payload = envelope['payload']
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY)).verify(
            base64.b64decode(envelope['signature'], validate=True), canonical(payload))
        stamp = now() if timestamp is None else timestamp
        if (payload.get('product', 'robot') != 'robot' or payload['installation_id'] != installation_id or payload['bot_id'] != bot_id
                or payload['machine_hash'] != machine_fingerprint()
                or payload['issued_at'] > stamp + 300
                or (payload['status'] == 'active' and (payload['issued_at'] > payload['lease_until']
                    or payload['lease_until'] > payload['grace_until']
                    or payload['grace_until'] > payload['expires_at']
                    or payload['grace_until'] - payload['issued_at'] > 72 * 3600))
                or payload['status'] not in {'active', 'revoked', 'expired'}):
            raise ValueError('Lease identity or dates mismatch')
        return payload
    except (KeyError, ValueError, TypeError, InvalidSignature) as exc:
        raise DomainError('امضای لایسنس یا هویت نصب معتبر نیست.') from exc


class LicenseClient:
    def __init__(self, store):
        self.store, self.settings, self.db = store, store.settings, store.db
        self.last_error = ''
        self.last_check = 0

    @property
    def bot_id(self):
        try:
            return int(self.settings.bot_token.split(':', 1)[0])
        except ValueError:
            return 0

    def status(self):
        if getattr(self.store, 'license_authority', None):
            return {'status': 'issuer', 'can_sell': True, 'issuer_bot': ISSUER_BOT}
        if self.settings.demo or not self.settings.license_required:
            return {'status': 'development', 'can_sell': True, 'issuer_bot': ISSUER_BOT}
        row = self.db.one("SELECT value FROM meta WHERE key='license:lease'")
        result = {'status': 'missing', 'can_sell': False, 'issuer_bot': ISSUER_BOT,
                  'last_check': self.last_check, 'last_error': self.last_error}
        if row:
            try:
                p = verify_envelope(json.loads(row['value']), self.settings.license_installation_id, self.bot_id)
                stamp = now()
                valid = p['status'] == 'active' and stamp <= min(p['grace_until'], p['expires_at'])
                result.update(status=p['status'] if p['status'] == 'revoked' or stamp <= p['expires_at'] else 'expired',
                              can_sell=valid, expires_at=p['expires_at'],
                              lease_until=p['lease_until'], grace_until=p['grace_until'],
                              offline_grace=valid and stamp > p['lease_until'], license_id=p['license_id'])
            except (DomainError, ValueError):
                result['status'] = 'invalid'
        return result

    def require_sales(self):
        if not self.status()['can_sell']:
            raise DomainError('فروش جدید موقتاً متوقف است؛ مدیر ربات باید لایسنس نصب را در @FARSTAR_VPN_BOT بررسی کند. '
                              'اشتراک قبلی و پیگیری سفارش پرداخت‌شده در دسترس است.')

    async def refresh(self, transport=None):
        if self.status()['status'] in {'issuer', 'development'}:
            return self.status()
        if not self.settings.license_installation_token or not self.settings.license_installation_id:
            return self.status()
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False, transport=transport) as http:
                response = await http.post(ISSUER_URL + '/heartbeat', json={
                    'installation_id': self.settings.license_installation_id,
                    'installation_token': self.settings.license_installation_token,
                    'bot_id': self.bot_id,
                    'machine_hash': machine_fingerprint(),
                })
                response.raise_for_status()
                envelope = response.json()
            verify_envelope(envelope, self.settings.license_installation_id, self.bot_id)
            with self.db.transaction() as con:
                con.execute("INSERT OR REPLACE INTO meta VALUES ('license:lease',?)", (json.dumps(envelope),))
            self.last_error = ''
            self.last_check = now()
        except (httpx.HTTPError, ValueError, DomainError) as exc:
            # Keep the last signed lease through a bounded outage; never trust an unsigned error body.
            self.last_error = type(exc).__name__
            log.warning('License check failed type=%s', type(exc).__name__)
        return self.status()

    async def run(self):
        while True:
            status = await self.refresh()
            if not status['can_sell'] and self.settings.license_required:
                from .store import notify
                with self.db.transaction() as con:
                    for admin_id in self.settings.admin_ids:
                        notify(con, admin_id, '⚠️ لایسنس نصب نیاز به بررسی دارد. فروش جدید متوقف شده؛ '
                               'اشتراک‌های قبلی حفظ شده‌اند. مرجع: @FARSTAR_VPN_BOT',
                               f'license-warning:{admin_id}:{now()//86400}')
            elif status.get('expires_at') and status['expires_at'] - now() <= 3 * 86400:
                from .store import notify
                with self.db.transaction() as con:
                    for admin_id in self.settings.admin_ids:
                        notify(con, admin_id, '🔑 کمتر از ۳ روز از لایسنس نصب باقی مانده. برای تمدید به @FARSTAR_VPN_BOT برو.',
                               f'license-expiring:{admin_id}:{status["expires_at"]}')
            await asyncio.sleep(6 * 3600 if not self.last_error else 300)


def client(store):
    if not hasattr(store, 'license_client'):
        store.license_client = LicenseClient(store)
    return store.license_client
