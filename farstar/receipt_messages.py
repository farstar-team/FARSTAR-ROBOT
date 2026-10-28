import json
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .store import DomainError


def receipt_message(store,receipt_id):
    row=store.db.one('SELECT r.*,u.name,u.username,o.snapshot FROM receipts r JOIN users u ON u.id=r.user_id '
                     'LEFT JOIN orders o ON o.id=r.order_id WHERE r.id=?',(receipt_id,))
    if not row:
        raise DomainError('رسید پیدا نشد.')
    plan=json.loads(row['snapshot']) if row['snapshot'] else None
    text=(f"🧾 <b>رسید خرید اشتراک</b>\nکاربر: {escape(row['name'][:80])} · <code>{row['user_id']}</code>\n"
          f"مبلغ: <b>{row['amount']:,} تومان</b>\nرسید: <code>{receipt_id}</code>\n")
    if plan:
        text+=(f"سفارش: <code>{row['order_id']}</code>\nپلن: {escape(plan['name'][:80])}\n"
               f"{plan['volume_gb']:g} گیگ · {plan['days']:g} روز\n")
    text+=f"وضعیت: { {'pending':'منتظر بررسی','approved':'تأیید شد','rejected':'رد شد'}[row['status']]}\n"
    text+='پس از تطبیق با گردش بانکی، تأیید کن؛ اشتراک مستقیم برای مشتری ارسال می‌شود.'
    buttons=[]
    if row['status']=='pending':
        if row['order_id']:
            buttons.append(InlineKeyboardButton(text='✅ تأیید و تحویل اشتراک',callback_data='adm:receiptapprove:'+receipt_id,style='success'))
        buttons.append(InlineKeyboardButton(text='❌ رد رسید',callback_data='adm:receiptreject:'+receipt_id,style='danger'))
    markup=InlineKeyboardMarkup(inline_keyboard=[buttons] if buttons else [])
    return row,text,markup
