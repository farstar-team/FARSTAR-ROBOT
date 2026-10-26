import base64
import hashlib
import json
import secrets
import time

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
