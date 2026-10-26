import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qsl

from cryptography.fernet import Fernet, InvalidToken


class Vault:
    def __init__(self, secret: str):
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))

    def seal(self, value: dict) -> str:
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def open(self, value: str) -> dict:
        try:
            return json.loads(self.cipher.decrypt(value.encode()))
        except InvalidToken as exc:
            raise ValueError("Server credential key mismatch; restore the original SECRET_KEY") from exc


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def new_session(db):
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    with db.transaction() as con:
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
        con.execute("INSERT INTO sessions VALUES (?,?,?)", (digest(token), csrf, int(time.time()) + 43200))
    return token, csrf


def rate_limit(db, key: str, maximum: int, window: int):
    now = int(time.time())
    with db.transaction() as con:
        row = con.execute("SELECT * FROM rate_limits WHERE key=?", (key,)).fetchone()
        if not row or row["reset_at"] <= now:
            con.execute("INSERT OR REPLACE INTO rate_limits VALUES (?,?,?)", (key, 1, now + window))
            return True
        if row["hits"] >= maximum:
            return False
        con.execute("UPDATE rate_limits SET hits=hits+1 WHERE key=?", (key,))
        return True


def validate_telegram_init_data(raw: str, bot_token: str, max_age: int = 600, timestamp=None):
    """Validate Telegram-signed WebAppData, never trust initDataUnsafe or a client role."""
    if not bot_token or len(raw) > 12000:
        raise ValueError("اطلاعات ورود تلگرام معتبر نیست.")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=30)
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError("duplicate fields")
        received = data.pop('hash')
        check = '\n'.join(f'{key}={value}' for key, value in sorted(data.items()))
        secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            raise ValueError("invalid signature")
        current = int(time.time()) if timestamp is None else timestamp
        age = current - int(data['auth_date'])
        if age < -30 or age > max_age:
            raise ValueError("expired")
        user = json.loads(data['user'])
        if not isinstance(user, dict) or type(user.get('id')) is not int or user['id'] <= 0 or user.get('is_bot'):
            raise ValueError("invalid user")
        return user
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("ورود تلگرام معتبر نیست یا منقضی شده؛ مینی‌اپ را از ربات دوباره باز کنید.") from exc


def new_app_session(db, user_id, role):
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    with db.transaction() as con:
        con.execute("DELETE FROM app_sessions WHERE expires_at<?", (int(time.time()),))
        con.execute("INSERT INTO app_sessions VALUES (?,?,?,?,?)",
                    (digest(token), user_id, role, csrf, int(time.time()) + 7200))
    return token, csrf
