"""Isolated sample data. Never connects to Telegram or a panel."""
import json
import random
import uuid

from .services import GIB
from .store import now


def seed(store):
    if store.db.one("SELECT 1 FROM users"):
        return
    rng = random.Random(42)
    timestamp = now()
    names = ["آرمان محمدی", "نگار احمدی", "علی رضایی", "سارا کریمی", "امیر حسینی", "نازنین موسوی",
             "مهدی شریفی", "هستی امینی", "رضا صادقی", "کیانا مرادی", "پارسا نوری", "ترانه حیدری"]
    locations = [("🇩🇪", "آلمان", "Frankfurt"), ("🇫🇮", "فنلاند", "Helsinki"),
                 ("🇳🇱", "هلند", "Amsterdam"), ("🇹🇷", "ترکیه", "Istanbul")]
    with store.db.transaction() as con:
        for i, (flag, country, city) in enumerate(locations, 1):
            con.execute("INSERT INTO servers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (i, f"{flag} {country}", city, f"https://demo-{i}.invalid/panel",
                         store.vault.seal({"token": "demo-no-network"}), i, f"https://demo-{i}.invalid/sub",
                         100 if i != 1 else 50, 1, 1, timestamp))
        for i, (name, days, gb, price, server) in enumerate([
            ("شروع", 30, 30, 120, 1), ("محبوب", 30, 60, 210, 1), ("حرفه‌ای", 30, 100, 320, 2),
            ("سه‌ماهه", 90, 200, 790, 3), ("اقتصادی", 30, 20, 90, 4), ("پلاس", 60, 120, 490, 2),
        ], 1):
            con.execute("INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (i, server, name, days, gb, price, 2, 1, "اتصال پایدار · پشتیبانی همراه شما", timestamp))
        for i in range(48):
            user_id = 100000 + i
            con.execute("INSERT INTO users(id,name,username,balance,terms_at,created_at) VALUES (?,?,?,?,?,?)",
                        (user_id, names[i % len(names)], f"farstar_{i + 1}", rng.choice([0, 40, 100, 220]),
                         timestamp, timestamp - rng.randint(1, 50) * 86400))
        for i in range(112):
            user_id = 100000 + i % 48
            plan_id = rng.randint(1, 6)
            plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
            created = timestamp - rng.randint(0, 13) * 86400 - rng.randint(0, 20000)
            order_id = f"demo{i:012x}"
            snap = {"name": plan["name"], "days": plan["days"], "volume_gb": plan["volume_gb"],
                    "ip_limit": 2, "email": f"fs-demo-{i}", "sub_id": f"sample-{i}",
                    "client_id": str(uuid.uuid4()), "password": "sample"}
            status = "completed" if i < 104 else rng.choice(["pending", "failed", "paid"])
            con.execute("""INSERT INTO orders(id,user_id,plan_id,server_id,price,currency,status,snapshot,
                  payment_method,created_at,expires_at,paid_at,completed_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (order_id, user_id, plan_id, plan["server_id"], plan["price"], store.settings.currency,
                         status, json.dumps(snap), "stars", created, timestamp + 1800,
                         created if status != "pending" else None, created + 3 if status == "completed" else None))
            if status == "completed" and i >= 50:
                expiry = created + plan["days"] * 86400
                volume = plan["volume_gb"] * GIB
                used = int(volume * rng.uniform(0.05, 0.87))
                con.execute("INSERT INTO services VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (order_id, user_id, plan_id, plan["server_id"], snap["email"], "{}", snap["sub_id"],
                             expiry, volume, used, 1, 0, created, timestamp))
                con.execute("UPDATE orders SET service_id=? WHERE id=?", (order_id, order_id))
        for i, subject in enumerate(["راهنمای اتصال به آیفون", "سؤال درباره تمدید سرویس", "پیگیری پرداخت", "تغییر اپلیکیشن"], 1):
            con.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?)", (i, 100000 + i, subject, "open", timestamp - i * 3600, timestamp - i * 3600))
            con.execute("INSERT INTO ticket_messages(ticket_id,sender,body,created_at) VALUES (?,?,?,?)",
                        (i, "user", "سلام، لطفاً درباره " + subject + " راهنمایی‌ام کنید. ممنونم.", timestamp - i * 3600))
        con.execute("INSERT INTO coupons VALUES ('WELCOME',15,100,?,1)", (timestamp + 30 * 86400,))
        con.execute("INSERT INTO gifts VALUES ('FARSTAR',50,20,?)", (timestamp + 30 * 86400,))
