"""Bounded, plain-text shop customization. No executable templates or remote logos."""
import base64
import io

from PIL import Image, ImageOps, UnidentifiedImageError

from .store import DomainError, audit

TEXTS = {
    'welcome': ('پیام شروع', 'اشتراکت، پرداختت و پشتیبانی؛ همه همین‌جاست.\nاز دکمه‌های زیر انتخاب کن.'),
    'terms': ('قوانین فروشگاه', ''),
    'support': ('متن پشتیبانی', 'پیامت را بفرست؛ تیم پشتیبانی همین‌جا پاسخ می‌دهد.'),
    'help': ('معرفی آموزش اتصال', 'دستگاهت را انتخاب کن؛ لینک رسمی دانلود Happ و GIF راهنما را می‌فرستم.'),
    'payment_note': ('توضیح پرداخت', 'فیش خوانا بفرست. پس از بررسی مدیر، اشتراک مستقیم برایت ارسال می‌شود.'),
    'delivery_note': ('متن همراه تحویل', 'لینک اشتراکت شخصی است؛ آن را در اختیار دیگران نگذار.'),
    'buy_button': ('دکمه خرید', '🛍 خرید اشتراک'),
    'services_button': ('دکمه سرویس‌ها', '📡 سرویس‌های من'),
    'wallet_button': ('دکمه پرداخت', '💳 کیف پول و پرداخت'),
    'help_button': ('دکمه آموزش', '📱 آموزش Happ'),
    'support_button': ('دکمه پشتیبانی', '💬 پشتیبانی'),
    'orders_button': ('دکمه سفارش‌ها', '🧾 سفارش‌های من'),
}


class Branding:
    def __init__(self, store):
        self.store, self.db, self.settings = store, store.db, store.settings
        saved = self.db.one("SELECT value FROM meta WHERE key='brand:app_name'")
        if saved:
            self.settings.app_name = saved['value']

    def text(self, key):
        row = self.db.one('SELECT value FROM meta WHERE key=?', ('brand:text:' + key,))
        if row:
            return row['value']
        if key == 'terms' and self.settings.terms_text:
            return self.settings.terms_text
        return TEXTS[key][1]

    def data(self):
        return {'app_name': self.settings.app_name,
                'logo_url': '/brand/logo' if self.db.one("SELECT 1 FROM meta WHERE key='brand:logo'") else '',
                'texts': {key: {'label': label, 'value': self.text(key)} for key, (label, _) in TEXTS.items()}}

    def save(self, app_name=None, texts=None, actor='admin'):
        values = {}
        if app_name is not None:
            app_name = app_name.strip()
            if not 2 <= len(app_name) <= 64 or any(ord(c) < 32 for c in app_name):
                raise DomainError('نام نمایشی بین ۲ تا ۶۴ نویسه و در یک خط باشد.')
            values['brand:app_name'] = app_name
        for key, value in (texts or {}).items():
            if key not in TEXTS or not isinstance(value, str):
                raise DomainError('کلید متن قابل ویرایش معتبر نیست.')
            value = value.strip()
            maximum = 48 if key.endswith('_button') else 2000
            if len(value) > maximum or any(ord(c) < 32 and c != '\n' for c in value):
                raise DomainError(f'متن حداکثر {maximum} نویسه باشد.')
            if key.endswith('_button') and (not value or '\n' in value):
                raise DomainError('عنوان دکمه باید در یک خط و غیرخالی باشد.')
            values['brand:text:' + key] = value
        with self.db.transaction() as con:
            for key, value in values.items():
                con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, value))
            audit(con, actor, 'branding.update', 'shop', {'keys': list(values)})
        if app_name is not None:
            self.settings.app_name = app_name
        return self.data()

    def save_logo(self, raw, actor='admin'):
        if not raw or len(raw) > 350000:
            raise DomainError('لوگو باید عکس و کوچک‌تر از ۳۵۰ کیلوبایت باشد.')
        try:
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {'PNG', 'JPEG', 'WEBP'} or source.width * source.height > 16000000:
                    raise ValueError()
                image = ImageOps.exif_transpose(source).convert('RGBA')
                image.thumbnail((256, 256))
                output = io.BytesIO()
                image.save(output, 'PNG', optimize=True)
        except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise DomainError('فرمت عکس لوگو معتبر نیست.') from exc
        if output.tell() > 260000:
            raise DomainError('عکس ساده‌تر یا کوچک‌تری انتخاب کن.')
        with self.db.transaction() as con:
            # Exactly one normalized image replaces the previous one; no file archive.
            con.execute("INSERT OR REPLACE INTO meta VALUES ('brand:logo',?)",
                        (base64.b64encode(output.getvalue()).decode(),))
            audit(con, actor, 'branding.logo', 'shop')

    def logo(self):
        row = self.db.one("SELECT value FROM meta WHERE key='brand:logo'")
        return base64.b64decode(row['value']) if row else None


def branding(store):
    if not hasattr(store, 'branding'):
        store.branding = Branding(store)
    return store.branding
