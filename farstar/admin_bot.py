import json
import secrets
import sqlite3
import tempfile
import contextlib
import csv
import io
from html import escape
from pathlib import Path

from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.exceptions import TelegramAPIError

from .store import DomainError, audit, now, uid
from .receipt_messages import receipt_message
from .experience import attention, sales_report, storage_status
from .broadcasts import audience as broadcast_audience, status as broadcast_status


def keys(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows])


BACK = [('↩ مدیریت', 'adm:home'), ('⌂ منوی مشتری', 'menu:home')]


def integer(text, minimum=None, maximum=None):
    raw = text.replace(',', '').replace('٬', '').replace(' ', '').replace('تومان', '')
    try:
        number = int(raw)
    except ValueError as exc:
        raise DomainError('فقط عدد وارد کنید؛ نمونه: ۲۰۰۰۰۰') from exc
    if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        raise DomainError('عدد خارج از محدوده مجاز است.')
    return number


class AdminBot:
    def __init__(self, store, provisioner, bot, send):
        self.store, self.db, self.settings = store, store.db, store.settings
        self.provisioner, self.bot, self.send = provisioner, bot, send

    def admin(self, actor):
        if actor not in self.settings.admin_ids:
            raise DomainError('این بخش فقط برای مدیر فروشگاه است.')

    def plans(self):
        return self.db.all('SELECT p.*,s.name server_name FROM plans p JOIN servers s ON s.id=p.server_id ORDER BY p.id')

    async def broadcast_preview(self, actor, text):
        self.admin(actor)
        if not 1<=len(text.strip())<=3000:
            raise DomainError('متن پیام حداکثر ۳۰۰۰ نویسه باشد.')
        campaign_id = uid()
        self.store.set_state(actor,'admin:broadcast-confirm',{'text':text.strip(),'campaign_id':campaign_id})
        count = broadcast_audience(self.db)
        markup = keys([[('📣 تأیید ارسال', 'adm:broadcastconfirm:'+campaign_id)],
                       [('✏️ ویرایش متن','adm:broadcast')],BACK])
        markup.inline_keyboard[0][0].style='success'
        return await self.send(actor,f'📣 <b>پیش‌نمایش پیام همگانی</b>\nگیرندگان فعلی: <b>{count:,}</b> نفر · شامل فهرست قدیمی\n\n'
                               +escape(text.strip())+'\n\nبا دکمه تأیید، ارسال مرحله‌ای شروع می‌شود. گزارش ارسال در مدیریت در دسترس است.',markup)

    async def home(self, actor):
        self.admin(actor)
        self.store.set_state(actor)
        rows = [[('📊 آمار فروش', 'adm:stats'), ('🛍 مدیریت پلن‌ها', 'adm:plans')],
                [('🚦 نیازمند رسیدگی', 'adm:attention'), ('📈 گزارش فروش', 'adm:report:7')],
                [('🧾 رسیدهای پرداخت', 'adm:receipts'), ('💬 تیکت‌ها', 'adm:tickets')],
                [('👤 کاربران و کیف پول', 'adm:users'), ('📦 سفارش‌ها', 'adm:orders')],
                [('🌐 سرورها', 'adm:servers'), ('📡 سرویس‌ها','adm:services')],
                [('💳 شماره کارت', 'adm:card'), ('⚙️ تنظیمات فروشگاه','adm:settings')],
                [('🎟 تخفیف و هدیه', 'adm:codes'), ('📣 پیام همگانی', 'adm:broadcast')],
                [('📬 گزارش پیام همگانی','adm:broadcaststatus')],
                [('💽 فضای سرور', 'adm:storage'), ('💾 پشتیبان ربات', 'adm:backup')],
                [('⌂ منوی مشتری', 'menu:home')]]
        markup = keys(rows)
        if self.settings.admin_mini_app_url:
            markup.inline_keyboard.insert(0, [InlineKeyboardButton(text='⚙️ مینی‌اپ مدیریت',
                                                  web_app=WebAppInfo(url=self.settings.admin_mini_app_url))])
        await self.send(actor, '⚙️ <b>مدیریت Farstar</b>\n\nهمه ابزارهای روزمره از همین ربات در دسترس است.', markup)

    async def plan_list(self, actor):
        plans = self.plans()
        rows = [[(f"{'🟢' if p['active'] else '⚪'} {p['name']} · {p['volume_gb']}GB · {p['price']:,}", f"adm:plan:{p['id']}")]
                for p in plans[:35]]
        rows += [[('➕ ساخت پلن', 'adm:newplan')], BACK]
        await self.send(actor, '🛍 <b>مدیریت پلن‌ها</b>\nپلن را برای ویرایش انتخاب کن.', keys(rows))

    async def plan(self, actor, plan_id):
        p = next((p for p in self.plans() if p['id'] == plan_id), None)
        if not p:
            raise DomainError('پلن پیدا نشد.')
        rows = [[('نام', f'adm:editplan:{plan_id}:name'), ('قیمت', f'adm:editplan:{plan_id}:price')],
                [('حجم', f'adm:editplan:{plan_id}:volume_gb'), ('مدت', f'adm:editplan:{plan_id}:days')],
                [('تعداد IP', f'adm:editplan:{plan_id}:ip_limit'), ('توضیحات', f'adm:editplan:{plan_id}:description')],
                [('⏸ توقف فروش' if p['active'] else '▶️ فعال کردن فروش', f'adm:plantoggle:{plan_id}')],
                [('↩ فهرست پلن‌ها', 'adm:plans')]]
        await self.send(actor, f"<b>{escape(p['name'])}</b>\nحجم: {p['volume_gb']}GB\nمدت: {p['days']} روز\n"
                        f"قیمت: {p['price']:,} {self.settings.currency}\nIP: {p['ip_limit']}\n"
                        f"سرور: {escape(p['server_name'])}", keys(rows))

    async def user(self, actor, user_id):
        u = self.store.user(user_id)
        if not u:
            raise DomainError('کاربر پیدا نشد.')
        rows = [[('💰 تغییر موجودی', f'adm:credit:{user_id}')]]
        if user_id not in self.settings.admin_ids:
            rows.append([('رفع مسدودی' if u['banned'] else 'مسدود کردن', f'adm:ban:{user_id}')])
        rows.append(BACK)
        await self.send(actor, f"👤 {escape(u['name'])}\nشناسه: <code>{u['id']}</code>\n"
                        f"موجودی: {u['balance']:,}\nوضعیت: {'مسدود' if u['banned'] else 'فعال'}", keys(rows))

    async def receipt(self, actor, receipt_id):
        row,caption,markup=receipt_message(self.store,receipt_id)
        await self.bot.send_photo(actor,row['file_id'],caption=caption,reply_markup=markup,parse_mode='HTML')

    async def ticket(self, actor, ticket_id):
        ticket = self.db.one('SELECT * FROM tickets WHERE id=?', (ticket_id,))
        if not ticket:
            raise DomainError('تیکت پیدا نشد.')
        messages = self.db.all('SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id DESC LIMIT 5', (ticket_id,))
        for message in reversed(messages):
            await self.send(actor, f"{'مدیر' if message['sender']=='admin' else 'کاربر'}:\n{escape(message['body'])}")
        await self.send(actor, f"💬 تیکت #{ticket_id} · {escape(ticket['subject'])}", keys([
            [('✍️ پاسخ', f'adm:reply:{ticket_id}'), ('بستن تیکت', f'adm:closeticket:{ticket_id}')], BACK]))

    async def callback(self, query):
        actor = query.from_user.id
        self.admin(actor)
        parts = (query.data or '').split(':')
        action = parts[1]
        if action == 'home':
            await self.home(actor)
        elif action == 'attention':
            report = attention(self.db)
            c = report['counts']
            rows = [[(f"🧾 {r['name'][:24]} · {r['amount']:,} · {(now()-r['created_at'])//60} دقیقه", 'adm:receipt:'+r['id'])]
                    for r in report['receipts']]
            rows += [[(f"📦 {r['name'][:24]} · {r['status']}", 'adm:order:'+r['id'])] for r in report['orders']]
            rows += [[(f"💬 #{r['id']} · {r['subject'][:30]}", f"adm:ticket:{r['id']}")] for r in report['tickets']]
            await self.send(actor, f"🚦 <b>نیازمند رسیدگی</b>\nرسید منتظر تأیید: {c['receipts']}\n"
                            f"قدیمی‌ترین رسید: {report['oldest_receipt_minutes']} دقیقه\n"
                            f"سفارش نیازمند بررسی: {c['orders']}\nتیکت بی‌پاسخ: {c['tickets']}\n"
                            f"پیام تحویل‌نشده پس از تلاش‌ها: {c['undelivered']}\n\nموارد قدیمی‌تر اول نمایش داده می‌شوند.", keys(rows+[BACK]))
        elif action in {'report','reportcsv'}:
            days = int(parts[2]) if len(parts)>2 else 7
            if days not in (1,7,30):
                raise DomainError('بازه گزارش معتبر نیست.')
            report = sales_report(self.db, days)
            if action == 'reportcsv':
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=['day','amount','orders'])
                writer.writeheader()
                writer.writerows(report['chart'])
                await self.bot.send_document(actor, BufferedInputFile(('\ufeff'+output.getvalue()).encode(), filename=f'farstar-sales-{days}d.csv'),
                                             caption='گزارش فروش تحویل‌شده · روزهای تقویمی به وقت تهران')
            else:
                top = '\n'.join(f"• {escape(p['name'])}: {p['sales']} فروش · {p['amount']:,}" for p in report['top_plans']) or 'هنوز فروش موفقی در این بازه نیست.'
                await self.send(actor, f"📈 <b>گزارش {'امروز' if days==1 else str(days)+' روز اخیر'}</b>\n"
                                f"فروش تحویل‌شده: {report['revenue']:,} {self.settings.currency}\n"
                                f"تعداد سفارش: {report['sales']} · خریدار: {report['buyers']}\n"
                                f"سهم تمدید: {report['renewal_revenue']:,}\n\nپلن‌های پرفروش:\n{top}\n\nروزهای تقویمی به وقت تهران؛ فاکتور پرداخت‌نشده در فروش محاسبه نمی‌شود.", keys([
                                    [('امروز','adm:report:1'),('۷ روز','adm:report:7'),('۳۰ روز','adm:report:30')],
                                    [('📥 خروجی CSV',f'adm:reportcsv:{days}')], BACK]))
        elif action == 'storage':
            data = storage_status(self.db)
            def mb(value):
                return value / 1024**2
            await self.send(actor, f"💽 <b>فضای سرور</b>\n\nفضای آزاد: {data['disk_free_bytes']/1024**3:.1f} گیگابایت\n"
                            f"دیتابیس ربات: {mb(data['database_bytes']):.2f} مگابایت\n"
                            f"بکاپ‌ها: {data['backup_count']} فایل · {mb(data['backup_bytes']):.2f} مگابایت\n"
                            'عکس فیش روی دیسک: ۰ بایت\n\nعکس‌ها در تلگرام می‌مانند؛ فقط شناسه و سابقه مالی نگهداری می‌شود. '
                            'آپلود عکس تا ۵ مگابایت و در حافظه موقت پردازش می‌شود. حداکثر ۱۴ بکاپ خودکار نگهداری می‌شود.', keys([BACK]))
        elif action == 'stats':
            s = self.store.stats()
            await self.send(actor, f"📊 <b>آمار فروشگاه</b>\nکاربران: {s['users']}\nسرویس فعال: {s['active_services']}\n"
                            f"فروش موفق: {s['sales']}\nدرآمد: {s['revenue']:,}\nرسید باز: {s['pending_receipts']}\n"
                            f"تیکت باز: {s['open_tickets']}\nسفارش نیازمند بررسی: {s['failed_orders']}", keys([BACK]))
        elif action == 'plans':
            await self.plan_list(actor)
        elif action == 'plan':
            await self.plan(actor, int(parts[2]))
        elif action == 'plantoggle':
            plan = next((p for p in self.plans() if p['id'] == int(parts[2])), None)
            if not plan:
                raise DomainError('پلن پیدا نشد.')
            data = {key: plan[key] for key in ['server_id','name','days','volume_gb','price','ip_limit','description','active']}
            data['active'] = not plan['active']
            self.store.save_plan(data, plan['id'])
            await self.plan(actor, plan['id'])
        elif action == 'editplan':
            field = parts[3]
            if field not in {'name','price','volume_gb','days','ip_limit','description'}:
                raise DomainError('فیلد معتبر نیست.')
            self.store.set_state(actor, 'admin:editplan', {'id':int(parts[2]),'field':field})
            await self.send(actor, 'مقدار جدید را در یک پیام بفرست.', keys([BACK]))
        elif action == 'newplan':
            servers = self.db.all('SELECT id,name FROM servers ORDER BY id')
            await self.send(actor, 'سرور پلن جدید را انتخاب کن.', keys([
                [(s['name'], f"adm:planserver:{s['id']}")] for s in servers] + [BACK]))
        elif action == 'planserver':
            self.store.server(int(parts[2]))
            self.store.set_state(actor, 'admin:newplan', {'step':0,'values':{'server_id':int(parts[2]),'active':True,'description':''}})
            await self.send(actor, 'نام پلن جدید را بفرست.', keys([BACK]))
        elif action == 'saveplan':
            user = self.store.user(actor)
            data = json.loads(user['state_data'])
            if user['state'] != 'admin:newplan-confirm':
                raise DomainError('این فرم قبلاً ثبت یا لغو شده است.')
            self.store.save_plan(data['values'])
            self.store.set_state(actor)
            await self.plan_list(actor)
        elif action == 'users':
            self.store.set_state(actor, 'admin:usersearch')
            await self.send(actor, 'نام، نام کاربری یا شناسه عددی مشتری را بفرست.', keys([BACK]))
        elif action == 'user':
            await self.user(actor, int(parts[2]))
        elif action == 'credit':
            self.store.set_state(actor, 'admin:credit', {'user_id':int(parts[2])})
            await self.send(actor, 'مبلغ تغییر موجودی را به تومان بفرست. برای برداشت، عدد منفی وارد کن.', keys([BACK]))
        elif action == 'creditconfirm':
            u = self.store.user(actor)
            if u['state'] != 'admin:credit-confirm':
                raise DomainError('این درخواست قبلاً ثبت یا لغو شده است.')
            data = json.loads(u['state_data'])
            self.store.adjust_balance(data['user_id'], data['amount'], data['reference'])
            self.store.set_state(actor)
            await self.user(actor, data['user_id'])
        elif action in {'ban','banconfirm'}:
            user_id = int(parts[2])
            u = self.store.user(user_id)
            if not u or user_id in self.settings.admin_ids:
                raise DomainError('این حساب قابل مسدود کردن نیست.')
            if action == 'ban':
                await self.send(actor, 'وضعیت مسدودی حساب تغییر کند؟ سرویس‌های قبلی جدا مدیریت می‌شوند.',
                                keys([[('تأیید', f'adm:banconfirm:{user_id}')],BACK]))
            else:
                with self.db.transaction() as con:
                    con.execute('UPDATE users SET banned=? WHERE id=?', (int(not u['banned']), user_id))
                    audit(con, actor, 'user.ban_toggle', user_id)
                await self.user(actor, user_id)
        elif action == 'receipts':
            rows = self.db.all("SELECT * FROM receipts WHERE status='pending' ORDER BY created_at LIMIT 25")
            await self.send(actor, '🧾 رسیدهای منتظر بررسی' if rows else 'رسید بررسی‌نشده‌ای نداریم.', keys([
                [(f"{r['amount']:,} تومان · {r['user_id']}", 'adm:receipt:'+r['id'])] for r in rows] + [BACK]))
        elif action == 'receipt':
            await self.receipt(actor, parts[2])
        elif action in {'receiptapprove','receiptreject'}:
            self.store.review_receipt(parts[2], action=='receiptapprove', actor)
            if query.message:
                _,caption,markup=receipt_message(self.store,parts[2])
                with contextlib.suppress(TelegramAPIError):
                    await self.bot.edit_message_caption(chat_id=actor,message_id=query.message.message_id,
                                                       caption=caption,reply_markup=markup,parse_mode='HTML')
            await self.send(actor, 'رسید بررسی و نتیجه برای کاربر ثبت شد.', keys([[('رسیدهای بعدی','adm:receipts')],BACK]))
        elif action == 'tickets':
            rows = self.db.all("SELECT * FROM tickets WHERE status!='closed' ORDER BY updated_at DESC LIMIT 25")
            await self.send(actor, '💬 تیکت‌ها' if rows else 'تیکت بازی نداریم.', keys([
                [(f"#{r['id']} · {r['subject'][:45]}",f"adm:ticket:{r['id']}")] for r in rows] + [BACK]))
        elif action == 'ticket':
            await self.ticket(actor, int(parts[2]))
        elif action == 'reply':
            self.store.set_state(actor, 'admin:reply', {'ticket_id':int(parts[2])})
            await self.send(actor, 'متن پاسخ پشتیبانی را بفرست.', keys([BACK]))
        elif action == 'closeticket':
            with self.db.transaction() as con:
                con.execute("UPDATE tickets SET status='closed',updated_at=? WHERE id=?", (now(), int(parts[2])))
                audit(con, actor, 'ticket.close', parts[2])
            await self.send(actor, 'تیکت بسته شد.', keys([BACK]))
        elif action == 'card':
            self.store.set_state(actor, 'admin:card-number')
            await self.send(actor, f"💳 کارت فعلی: <code>{escape(self.settings.card_number or 'تنظیم نشده')}</code>\n"
                            f"{escape(self.settings.card_holder)}\n\nشماره کارت ۱۶ رقمی جدید را بفرست.", keys([BACK]))
        elif action == 'cardsave':
            u = self.store.user(actor)
            if u['state'] != 'admin:card-confirm':
                raise DomainError('فرم کارت معتبر نیست.')
            data = json.loads(u['state_data'])
            self.store.save_payment_details(data['card_number'], data['card_holder'])
            self.store.set_state(actor)
            await self.send(actor, 'مشخصات کارت ذخیره شد و برای پرداخت‌های جدید نمایش داده می‌شود.', keys([BACK]))
        elif action == 'servers':
            rows = self.db.all('SELECT id,name,active,inbound_id FROM servers ORDER BY id')
            await self.send(actor, '🌐 سرورها · برای بررسی اتصال انتخاب کن.', keys([
                [(f"{'🟢' if s['active'] else '⚪'} {s['name']} · #{s['inbound_id']}", f"adm:servertest:{s['id']}")] for s in rows] + [BACK]))
        elif action == 'servertest':
            server = self.store.server(int(parts[2]))
            if self.settings.demo:
                await self.send(actor, 'این سرور نمایشی است؛ درخواست خارجی ارسال نشد.', keys([BACK]))
            else:
                async with self.provisioner.panel(server) as panel:
                    ids = await self.provisioner.target_inbounds(panel,server)
                await self.send(actor, f"اتصال پنل برقرار است.\nاینباندهای فروش: {', '.join(map(str,ids))}\n"
                                f"حالت: {'همه اینباندهای فعال' if server['all_inbounds'] else 'اینباند تکی'}", keys([
                                    [('توقف فروش' if server['active'] else 'فعال کردن فروش',f"adm:servertoggle:{server['id']}")],
                                    [('تست این سرور: '+('فعال' if server['trial'] else 'غیرفعال'),f"adm:servertrial:{server['id']}")],BACK]))
                await self.send(actor,'برای خرید و اتصال خودکار اینباندهای جدید:',keys([
                    [('همه اینباندها: '+('فعال' if server['all_inbounds'] else 'غیرفعال'),f"adm:serverall:{server['id']}")],BACK]))
        elif action == 'servertoggle':
            server=self.store.server(int(parts[2]))
            self.store.save_server({'active':not server['active']},server['id'],actor)
            await self.send(actor,'وضعیت فروش این سرور تغییر کرد؛ اینباند و مشتریان پنل تغییر نکردند.',keys([BACK]))
        elif action == 'servertrial':
            server=self.store.server(int(parts[2]))
            self.store.save_server({'trial':not server['trial']},server['id'],actor)
            await self.send(actor,'اجازه صدور تست روی این سرور تغییر کرد.',keys([BACK]))
        elif action == 'serverall':
            server=self.store.server(int(parts[2]))
            self.store.save_server({'all_inbounds':not server['all_inbounds']},server['id'],actor)
            await self.send(actor,'حالت اینباندهای فروش ذخیره شد. در حالت همه، اینباندهای فعال و سازگار جدید هم خودکار به اشتراک‌های ربات اضافه می‌شوند.',keys([BACK]))
        elif action == 'services':
            rows=self.db.all('SELECT id,email,user_id,enabled FROM services ORDER BY created_at DESC LIMIT 25')
            await self.send(actor,'📡 سرویس‌های صادرشده توسط فروشگاه' if rows else 'فروشگاه هنوز سرویسی صادر نکرده.',keys([
                [(f"{'🟢' if s['enabled'] else '⚪'} {s['email']} · {s['user_id']}",f"adm:service:{s['id']}")] for s in rows]+[BACK]))
        elif action == 'service':
            service=self.db.one('SELECT * FROM services WHERE id=?',(parts[2],))
            if not service:
                raise DomainError('سرویس پیدا نشد.')
            await self.send(actor,f"سرویس: {escape(service['email'])}\nکاربر: {service['user_id']}\n"
                            f"مصرف: {service['used_bytes']/1073741824:.1f} از {service['total_bytes']/1073741824:g} گیگ\n"
                            f"وضعیت: {'فعال' if service['enabled'] else 'غیرفعال'}",keys([
                                [('غیرفعال‌سازی' if service['enabled'] else 'فعال‌سازی',f"adm:servicetoggle:{service['id']}")],BACK]))
        elif action == 'servicetoggle':
            await self.send(actor,'وضعیت اتصال این سرویس در پنل تغییر کند؟',keys([
                [('تأیید تغییر وضعیت',f'adm:serviceconfirm:{parts[2]}')],BACK]))
        elif action == 'serviceconfirm':
            service=self.db.one('SELECT enabled FROM services WHERE id=?',(parts[2],))
            if not service or self.settings.demo:
                raise DomainError('سرویس واقعی در دسترس نیست.')
            await self.provisioner.set_enabled(parts[2],not service['enabled'])
            await self.send(actor,'وضعیت سرویس در پنل بروزرسانی شد.',keys([BACK]))
        elif action == 'settings':
            await self.send(actor,f"⚙️ تنظیمات فروشگاه\nتست رایگان: {'فعال' if self.settings.trial_enabled else 'غیرفعال'}\n"
                            f"حجم تست: {self.settings.trial_gb} گیگ · {self.settings.trial_hours} ساعت\n"
                            f"پاداش دعوت: {self.settings.referral_percent}٪",keys([
                                [('توقف تست رایگان' if self.settings.trial_enabled else 'فعال کردن تست رایگان','adm:trialtoggle')],
                                [('درصد پاداش دعوت','adm:shopedit:referral_percent'),('حجم تست','adm:shopedit:trial_gb')],
                                [('مدت تست (ساعت)','adm:shopedit:trial_hours')],BACK]))
        elif action == 'trialtoggle':
            self.store.save_shop_settings({'trial_enabled':not self.settings.trial_enabled},actor)
            await self.send(actor,'تنظیم تست رایگان ذخیره شد.',keys([[('تنظیمات','adm:settings')],BACK]))
        elif action == 'shopedit':
            if parts[2] not in {'trial_gb','trial_hours','referral_percent'}:
                raise DomainError('تنظیم معتبر نیست.')
            self.store.set_state(actor,'admin:shop-setting',{'key':parts[2]})
            await self.send(actor,'مقدار جدید را به صورت عدد بفرست.',keys([BACK]))
        elif action == 'orders':
            rows = self.db.all('SELECT id,price,status,user_id FROM orders ORDER BY created_at DESC LIMIT 20')
            await self.send(actor, '📦 سفارش‌ها' if rows else 'سفارشی ثبت نشده است.', keys([
                [(f"{r['id'][-6:]} · {r['price']:,} · {r['status']}", 'adm:order:'+r['id'])] for r in rows] + [BACK]))
        elif action == 'order':
            order = self.store.order(parts[2])
            rows = [[('🔁 تلاش دوباره','adm:retry:'+order['id']), ('بازپرداخت','adm:refund:'+order['id'])]] if order['status']=='failed' else []
            await self.send(actor, f"سفارش: <code>{order['id']}</code>\nکاربر: {order['user_id']}\n"
                            f"مبلغ: {order['price']:,}\nوضعیت: {escape(order['status'])}\n{escape(order['last_error'] or '')}", keys(rows+[BACK]))
        elif action == 'retry':
            self.store.retry(parts[2])
            await self.send(actor, 'سفارش دوباره در صف بررسی و تحویل قرار گرفت.', keys([BACK]))
        elif action == 'refund':
            await self.send(actor, 'خرید ناموفق پس از بررسی و غیرفعال‌سازی حساب احتمالی در پنل بازپرداخت شود؟',
                            keys([[('تأیید بازپرداخت','adm:refundconfirm:'+parts[2])],BACK]))
        elif action == 'refundconfirm':
            if self.settings.demo:
                raise DomainError('بازپرداخت در حالت نمایشی غیرفعال است.')
            await self.provisioner.refund_failed(parts[2])
            await self.send(actor, 'درخواست بازپرداخت ثبت شد.', keys([BACK]))
        elif action == 'codes':
            await self.send(actor, '🎟 نوع کد را انتخاب کن.', keys([
                [('ساخت کد تخفیف','adm:code:coupon'),('ساخت کد هدیه','adm:code:gift')],BACK]))
        elif action == 'code':
            if parts[2] not in {'coupon','gift'}:
                raise DomainError('نوع کد معتبر نیست.')
            self.store.set_state(actor, 'admin:code', {'kind':parts[2]})
            unit = 'درصد' if parts[2]=='coupon' else 'مبلغ تومان'
            await self.send(actor, f'در یک پیام بفرست:\n<code>CODE {unit} تعداد روزاعتبار</code>\nنمونه: <code>WELCOME 10 100 30</code>', keys([BACK]))
        elif action == 'broadcast':
            self.store.set_state(actor, 'admin:broadcast')
            await self.send(actor, f'گیرندگان فعلی: {broadcast_audience(self.db):,} نفر، شامل فهرست قدیمی.\n'
                            'متن پیام همگانی را بفرست تا پیش‌نمایش آماده شود.', keys([BACK]))
        elif action == 'broadcastconfirm':
            u = self.store.user(actor)
            if u['state'] != 'admin:broadcast-confirm':
                raise DomainError('این پیام قبلاً ارسال یا لغو شده است.')
            data=json.loads(u['state_data'])
            campaign_id=data.get('campaign_id') or uid()
            if data.get('campaign_id') and (len(parts)<3 or parts[2]!=campaign_id):
                raise DomainError('این پیش‌نمایش قدیمی است؛ پیش‌نمایش تازه را تأیید کن.')
            count = self.store.broadcast(data['text'],campaign_id)
            self.store.set_state(actor)
            await self.send(actor, f'پیام برای {count:,} گیرنده در صف ارسال قرار گرفت.\n'
                            'خریدها، فیش‌ها و تحویل اشتراک در اولویت ارسال می‌مانند.',
                            keys([[('📬 گزارش ارسال','adm:broadcaststatus:'+campaign_id)],BACK]))
        elif action == 'broadcaststatus':
            report=broadcast_status(self.db,parts[2] if len(parts)>2 else None)
            await self.send(actor,f"📬 <b>گزارش پیام همگانی</b>\nکل گیرندگان: {report['total']:,}\n"
                            f"✅ ارسال موفق: {report['sent']:,}\n⏳ در صف یا منتظر تلاش دوباره: {report['pending']:,}\n"
                            f"⚪ ارسال ناموفق: {report['failed']:,}\n\nارسال موفق یعنی تلگرام پیام را پذیرفته؛ به معنی خوانده‌شدن نیست.",
                            keys([[('🔄 بروزرسانی گزارش','adm:broadcaststatus:'+report['id'])],BACK]))
        elif action == 'backup':
            handle = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
            path = Path(handle.name)
            handle.close()
            try:
                self.db.backup(path)
                await self.bot.send_document(actor, BufferedInputFile(path.read_bytes(), filename=f'farstar-{now()}.db'),
                                              caption='💾 پشتیبان ربات؛ این فایل را خصوصی نگهداری کن.')
            finally:
                path.unlink(missing_ok=True)
        else:
            raise DomainError('دکمه مدیریت معتبر نیست.')

    async def message(self, message):
        actor = message.from_user.id
        user = self.store.user(actor)
        if not user or not user['state'].startswith('admin:'):
            return False
        self.admin(actor)
        text = (message.text or '').strip()
        data = json.loads(user['state_data'])
        state = user['state']
        if not text:
            raise DomainError('پاسخ را به صورت متن بفرست یا از دکمه بازگشت استفاده کن.')
        if state == 'admin:editplan':
            p = next((p for p in self.plans() if p['id']==data['id']), None)
            if not p:
                raise DomainError('پلن پیدا نشد.')
            values = {key:p[key] for key in ['server_id','name','days','volume_gb','price','ip_limit','description','active']}
            values[data['field']] = integer(text,0,100000000) if data['field'] in {'price','volume_gb','days','ip_limit'} else text
            self.store.save_plan(values, p['id'])
            self.store.set_state(actor)
            await self.plan(actor, p['id'])
        elif state == 'admin:newplan':
            fields = ['name','volume_gb','days','price','ip_limit']
            prompts = ['حجم به گیگابایت؟','مدت اعتبار به روز؟','قیمت به تومان؟','تعداد IP هم‌زمان؟ (۰ = نامحدود)']
            key = fields[data['step']]
            data['values'][key] = text if key=='name' else integer(text,0 if key=='ip_limit' else 1,100000000)
            data['step'] += 1
            if data['step']==len(fields):
                self.store.set_state(actor,'admin:newplan-confirm',data)
                v=data['values']
                await self.send(actor,f"<b>{escape(v['name'])}</b>\n{v['volume_gb']}GB · {v['days']} روز · {v['price']:,} تومان\nپلن فعال شود؟",
                                keys([[('✅ ثبت پلن','adm:saveplan')],BACK]))
            else:
                self.store.set_state(actor,state,data)
                await self.send(actor,prompts[data['step']-1],keys([BACK]))
        elif state == 'admin:usersearch':
            pattern='%'+text[:100]+'%'
            rows=self.db.all('SELECT id,name FROM users WHERE name LIKE ? OR username LIKE ? OR CAST(id AS TEXT) LIKE ? LIMIT 15',
                             (pattern,pattern,pattern))
            await self.send(actor,'نتیجه جستجو' if rows else 'کاربری پیدا نشد.',keys([
                [(f"{u['name']} · {u['id']}",f"adm:user:{u['id']}")] for u in rows]+[BACK]))
        elif state == 'admin:credit':
            data.update(amount=integer(text,-100000000,100000000),reference=secrets.token_hex(16))
            self.store.set_state(actor,'admin:credit-confirm',data)
            await self.send(actor,f"تغییر موجودی کاربر {data['user_id']}: {data['amount']:+,} تومان\nتأیید می‌کنی؟",
                            keys([[('تأیید تغییر موجودی','adm:creditconfirm')],BACK]))
        elif state == 'admin:reply':
            self.store.ticket(actor,text[:3000],data['ticket_id'],admin=True)
            self.store.set_state(actor)
            await self.send(actor,'پاسخ ثبت شد.',keys([BACK]))
        elif state == 'admin:shop-setting':
            self.store.save_shop_settings({data['key']:integer(text,0,100)},actor)
            self.store.set_state(actor)
            await self.send(actor,'تنظیم ذخیره شد.',keys([[('تنظیمات','adm:settings')],BACK]))
        elif state == 'admin:card-number':
            number=''.join(str(int(c)) for c in text if c.isdecimal())
            if len(number)!=16 or any(not(c.isdecimal() or c in ' -') for c in text):
                raise DomainError('شماره کارت باید ۱۶ رقم باشد.')
            self.store.set_state(actor,'admin:card-holder',{'card_number':number})
            await self.send(actor,'نام صاحب حساب را بفرست.',keys([BACK]))
        elif state == 'admin:card-holder':
            if not 3<=len(text)<=80:
                raise DomainError('نام صاحب حساب باید بین ۳ تا ۸۰ نویسه باشد.')
            data['card_holder']=text
            self.store.set_state(actor,'admin:card-confirm',data)
            await self.send(actor,f"<code>{data['card_number']}</code>\n{escape(text)}\nذخیره شود؟",
                            keys([[('ذخیره مشخصات کارت','adm:cardsave')],BACK]))
        elif state == 'admin:code':
            values=text.split()
            if len(values)!=4:
                raise DomainError('فرمت: CODE مقدار تعداد روزاعتبار')
            try:
                self.store.create_code(data['kind'],values[0],integer(values[1],1),integer(values[2],1,100000),
                                       now()+integer(values[3],1,365)*86400,actor)
            except sqlite3.IntegrityError as exc:
                raise DomainError('این کد قبلاً ساخته شده است.') from exc
            self.store.set_state(actor)
            await self.send(actor,'کد ساخته شد.',keys([BACK]))
        elif state == 'admin:broadcast':
            if len(text)>3000:
                raise DomainError('متن حداکثر ۳۰۰۰ نویسه باشد.')
            await self.broadcast_preview(actor,text)
        else:
            await self.send(actor,'از دکمه تأیید یا بازگشت استفاده کن.',keys([BACK]))
        return True
