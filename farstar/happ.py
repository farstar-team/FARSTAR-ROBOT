"""Official Happ download destinations, reviewed 2026-10-26."""
from html import escape
from pathlib import Path

from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

BASE = 'https://github.com/Happ-proxy/happ-desktop/releases/latest/download/'
ANDROID_APK = 'https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk'
STORE = 'https://apps.apple.com/us/app/happ-proxy-utility/id6504287215'
PLAY = 'https://play.google.com/store/apps/details?id=com.happproxy'
SOURCE = 'https://www.happ.su/main'

DEVICES = {
    'android': {'name': 'اندروید', 'icon': '🤖', 'links': [('Google Play', PLAY), ('APK رسمی', ANDROID_APK)],
                'steps': ['لینک اشتراک را از سرویس‌های من کپی کن.', 'Happ را باز کن و + را بزن.',
                          'گزینه افزودن از کلیپ‌بورد (Clipboard) را انتخاب کن.', 'سرور را انتخاب کن، اتصال را روشن کن و مجوز VPN را بپذیر.']},
    'ios': {'name': 'آیفون و آیپد', 'icon': '🍎', 'links': [('App Store', STORE)],
            'steps': ['Happ را از App Store نصب و لینک اشتراک را کپی کن.', 'در Happ دکمه + و سپس افزودن از کلیپ‌بورد را بزن.',
                      'اگر اجازه Paste خواست، تأیید کن.', 'سرور را انتخاب کن و اتصال و مجوز افزودن VPN را تأیید کن.']},
    'windows': {'name': 'ویندوز', 'icon': '🪟',
                'links': [('Windows x64', BASE + 'setup-Happ.x64.exe'), ('Windows ARM64', BASE + 'setup-Happ.arm64.exe')],
                'steps': ['نسخه مناسب پردازنده‌ات را نصب و لینک اشتراک را کپی کن.', 'Happ را باز کن و دکمه + را بزن.',
                          'از Clipboard اشتراک را اضافه کن و منتظر بارگذاری سرورها بمان.', 'یک سرور را انتخاب و دکمه اتصال را روشن کن.']},
    'macos': {'name': 'مک', 'icon': '💻',
              'links': [('App Store', STORE), ('DMG رسمی', BASE + 'Happ.macOS.universal.dmg')],
              'steps': ['Happ را نصب و لینک اشتراک را کپی کن.', 'در Happ دکمه + را انتخاب کن.',
                        'اشتراک را از کلیپ‌بورد اضافه و یک سرور انتخاب کن.', 'اتصال را روشن کن و در صورت نمایش، مجوز سیستم را تأیید کن.']},
    'linux': {'name': 'لینوکس', 'icon': '🐧',
              'links': [(f'{kind.upper()} {arch}', BASE + f'Happ.linux.{arch}.{suffix}')
                        for arch in ['x64', 'arm64'] for kind, suffix in [('deb', 'deb'), ('rpm', 'rpm'), ('pkg', 'pkg.tar.zst')]],
              'steps': ['بسته مناسب توزیع و پردازنده را از لینک رسمی نصب کن.', 'لینک اشتراک را کپی و Happ را باز کن.',
                        'با + و افزودن از Clipboard، اشتراک را وارد کن.', 'سرور را انتخاب کن و اتصال را روشن کن.']},
    'androidtv': {'name': 'تلویزیون اندرویدی', 'icon': '📺', 'links': [('Google Play TV', PLAY), ('APK رسمی', ANDROID_APK)],
                  'steps': ['Happ را روی تلویزیون نصب کن.', 'صفحه افزودن اشتراک و Web Import را باز نگه دار.',
                            'در گوشی tv.happ.su را باز کن و کد موقت تلویزیون را وارد کن.', 'اشتراک خودت را ارسال کن؛ سپس در تلویزیون سرور و اتصال را انتخاب کن.']},
    'appletv': {'name': 'اپل تی‌وی', 'icon': '📺',
                'links': [('App Store tvOS', 'https://apps.apple.com/us/app/happ-proxy-utility-for-tv/id6748297274')],
                'steps': ['Happ مخصوص TV را روی Apple TV نصب کن.', 'در صفحه افزودن، Web Import را انتخاب و صفحه را باز نگه دار.',
                          'با گوشی به tv.happ.su برو، کد موقت TV و اشتراک را وارد کن.', 'بعد از وارد شدن سرورها، یکی را انتخاب و اتصال را روشن کن.']},
}


def catalog():
    return [dict(id=key, **value, animation=f'/static/happ-{key}.gif') for key, value in DEVICES.items()]


def device_keyboard():
    buttons = [InlineKeyboardButton(text=d['icon'] + ' ' + d['name'], callback_data='guide:' + key)
               for key, d in DEVICES.items()]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i:i+2] for i in range(0, len(buttons), 2)] + [
        [InlineKeyboardButton(text='بازگشت به منوی اصلی', callback_data='menu:home')]])


async def send_guide(bot, store, user_id, device):
    if device not in DEVICES:
        from .store import DomainError
        raise DomainError('نوع دستگاه معتبر نیست.')
    data = DEVICES[device]
    buttons = [[InlineKeyboardButton(text='دانلود Happ · ' + label, url=url)] for label, url in data['links']]
    if device in {'androidtv', 'appletv'}:
        buttons.append([InlineKeyboardButton(text='انتقال اشتراک به TV', url='https://tv.happ.su/')])
    buttons += [[InlineKeyboardButton(text='انتخاب دستگاه دیگر', callback_data='menu:help')],
                [InlineKeyboardButton(text='سرویس‌های من', callback_data='menu:services')]]
    caption = f"📱 <b>Happ · {escape(data['name'])}</b>\n\n" + '\n'.join(
        f'{i+1}. {escape(step)}' for i, step in enumerate(data['steps']))
    caption += '\n\nاین GIF راهنمای شماتیک است؛ ظاهر نسخه برنامه ممکن است کمی متفاوت باشد.'
    path = Path(__file__).parent / 'static' / f'happ-{device}.gif'
    cached = store.db.one("SELECT value FROM meta WHERE key=?", ('guide-file:' + device,))
    animation = cached['value'] if cached else FSInputFile(path)
    from aiogram.exceptions import TelegramBadRequest
    try:
        result = await bot.send_animation(user_id, animation, caption=caption, parse_mode='HTML',
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except TelegramBadRequest:
        if not cached:
            raise
        result = await bot.send_animation(user_id, FSInputFile(path), caption=caption, parse_mode='HTML',
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    if getattr(result, 'animation', None):
        with store.db.transaction() as con:
            con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ('guide-file:' + device, result.animation.file_id))
    return result
