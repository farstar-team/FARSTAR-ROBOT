"""Native Telegram setup and branding tools available to each shop administrator."""
import io
import json
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .branding import branding, TEXTS
from .licensing import client, STATUS_LABELS
from .store import DomainError


def keys(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t, callback_data=c) for t, c in row] for row in rows])


class Management:
    def __init__(self, admin):
        self.admin = admin
        self.store, self.bot, self.send = admin.store, admin.bot, admin.send
        self.brand = branding(self.store)

    async def callback(self, actor, data):
        self.admin.admin(actor)
        parts = data.split(':')
        action = parts[1]
        back = [('↩ مدیریت', 'adm:home')]
        if action == 'branding':
            self.store.set_state(actor)
            rows = [[('نام فروشگاه و ربات', 'adm:brandname'), ('لوگوی مینی‌اپ', 'adm:brandlogo')]]
            rows += [[(label, 'adm:brandtext:' + key)] for key, (label, _) in TEXTS.items()]
            rows += [[('اعمال نام روی پروفایل تلگرام', 'adm:brandprofile')], back]
            await self.send(actor, '🎨 <b>نام، متن‌ها و لوگوی فروشگاه</b>\nتغییرات بلافاصله در همین فروشگاه اعمال می‌شود. '
                            'نام کاربری @ ربات را فقط BotFather تغییر می‌دهد.', keys(rows))
        elif action in {'brandname', 'brandlogo', 'brandtext'}:
            key = parts[2] if action == 'brandtext' else ''
            if action == 'brandtext' and key not in TEXTS:
                raise DomainError('کلید متن معتبر نیست.')
            self.store.set_state(actor, 'admin:' + action, {'key': key})
            prompt = ('نام تازه را بفرست (۲ تا ۶۴ نویسه).' if action == 'brandname' else
                      'لوگو را به صورت عکس کوچک‌تر از ۳۵۰ کیلوبایت بفرست. فقط یک نسخه بهینه از لوگو نگه داشته می‌شود.'
                      if action == 'brandlogo' else f'متن جدید «{TEXTS[key][0]}» را بفرست. برای بازگشت به پیش‌فرض /default را بفرست.\n\nمتن فعلی:\n' + escape(self.brand.text(key)))
            await self.send(actor, prompt, keys([[('بازگشت', 'adm:branding')]]))
        elif action == 'brandprofile':
            await self.bot.set_my_name(name=self.store.settings.app_name)
            await self.send(actor, 'نام نمایشی پروفایل تلگرام هم به‌روزرسانی شد.', keys([back]))
        elif action == 'licenseinfo':
            self.store.set_state(actor)
            result = client(self.store).status()
            await self.send(actor, '🔑 <b>لایسنس نصب</b>\nوضعیت: ' + escape(STATUS_LABELS.get(result['status'],result['status'])) +
                            '\nمرجع دریافت و تمدید: @FARSTAR_VPN_BOT\nفروش جدید: ' + ('فعال' if result['can_sell'] else 'متوقف'), keys([back]))
        elif action == 'newserver':
            self.store.set_state(actor, 'admin:serverwizard', {'step': 0, 'values': {}})
            await self.send(actor, '🌐 افزودن پنل ثنایی (API نسخه ۳٫۱۱)\nنام این سرور را بفرست.', keys([back]))
        else:
            return False
        return True

    async def message(self, message):
        actor = message.from_user.id
        self.admin.admin(actor)
        user = self.store.user(actor)
        if not user:
            return False
        state, data = user['state'], json.loads(user['state_data'])
        text = (message.text or '').strip()
        if state == 'admin:brandlogo':
            if not message.photo:
                raise DomainError('لوگو را به صورت عکس بفرست یا بازگشت را بزن.')
            photo = message.photo[-1]
            if (photo.file_size or 0) > 350000:
                raise DomainError('عکس لوگو کوچک‌تر از ۳۵۰ کیلوبایت باشد.')
            output = io.BytesIO()
            await self.bot.download(photo, destination=output)
            self.brand.save_logo(output.getvalue(), actor)
        elif state == 'admin:brandname':
            self.brand.save(app_name=text, actor=actor)
        elif state == 'admin:brandtext':
            key = data['key']
            self.brand.save(texts={key: TEXTS[key][1] if text == '/default' else text}, actor=actor)
        elif state == 'admin:serverwizard':
            fields = ['name', 'location', 'url', 'token', 'subscription_url', 'inbound_id', 'protocols', 'node_ids', 'capacity']
            prompts = ['موقعیت سرور؟ مثال: آلمان', 'آدرس کامل پنل همراه مسیر اختصاصی؟ مثال: https://panel.example.com:2053/private',
                       'توکن API با دسترسی admin را بفرست. پیام توکن پس از دریافت حذف می‌شود.',
                       'آدرس پایه Subscription پنل را بفرست؛ بدون شناسه کاربر.',
                       'شناسه عددی یک اینباند پایه فعال؟',
                       'پروتکل‌ها را با کاما بفرست؛ یا all برای همه: vless,vmess,trojan,shadowsocks,hysteria,tuic',
                       'شناسه نودها را با کاما بفرست. 0 یعنی نود محلی؛ all یعنی همه نودهای فعلی و آینده.',
                       'ظرفیت فروش این سرور؟ مثال: 1000']
            step = data['step']
            field = fields[step]
            if field in {'inbound_id', 'capacity'}:
                if not text.isdecimal() or not 1 <= int(text) <= 1000000:
                    raise DomainError('یک عدد مثبت معتبر بفرست.')
                value = int(text)
            elif field == 'protocols':
                from .panels import PROTOCOLS
                value = [] if text.lower() == 'all' else [s.strip().lower() for s in text.split(',')]
                if any(s not in PROTOCOLS for s in value):
                    raise DomainError('نام پروتکل معتبر نیست.')
            elif field == 'node_ids':
                try:
                    value = [] if text.lower() == 'all' else [int(s.strip()) for s in text.split(',')]
                    if any(n < 0 for n in value) or len(value) > 100:
                        raise ValueError()
                except ValueError as exc:
                    raise DomainError('شناسه نود باید عدد نامنفی یا all باشد.') from exc
            else:
                if not text or len(text) > 512:
                    raise DomainError('این مقدار معتبر نیست.')
                if field in {'url', 'subscription_url'}:
                    from .api import ServerInput
                    text = ServerInput.valid_url(text)
                value = text
            if field == 'token':
                data['credentials'] = self.store.vault.seal({'token': value})
                try:
                    await message.delete()
                except Exception:
                    pass
            else:
                data['values'][field] = value
            if step < len(fields) - 1:
                data['step'] += 1
                self.store.set_state(actor, state, data)
                await self.send(actor, prompts[step], keys([[('انصراف', 'adm:home')]]))
                return True
            from .api import ServerInput
            try:
                body = ServerInput(**data['values'], credentials=self.store.vault.open(data['credentials']), all_inbounds=True)
            except ValueError as exc:
                raise DomainError('اطلاعات سرور کامل یا معتبر نیست؛ دوباره از افزودن سرور شروع کن.') from exc
            server_id = self.store.save_server(body.model_dump(), actor=actor)
            self.store.set_state(actor)
            await self.send(actor, f'پنل #{server_id} ذخیره شد. ابتدا بررسی اتصال را بزن؛ سپس پلن بساز.',
                            keys([[('بررسی اتصال و اینباندها', 'adm:servertest:' + str(server_id))], [('سرورها', 'adm:servers')]]))
            return True
        else:
            return False
        self.store.set_state(actor)
        await self.send(actor, '✅ تغییر ذخیره شد.', keys([[('نام، متن و لوگو', 'adm:branding')], [('مدیریت', 'adm:home')]]))
        return True
