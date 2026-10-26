"""A branded subscription card. Branding stays outside the QR quiet zone."""
import io
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import qrcode
from PIL import Image, ImageDraw, ImageFont

FONT = Path(__file__).parent / 'static/Vazirmatn-Regular.ttf'


def subscription_card(link, service):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,border=4,box_size=1)
    qr.add_data(link)
    qr.make(fit=True)
    qr.box_size = max(1,640 // len(qr.get_matrix()))
    code = qr.make_image(fill_color='#10241c',back_color='white').convert('RGB')
    width,height=800,1040
    card=Image.new('RGB',(width,height),'white')
    draw=ImageDraw.Draw(card)
    draw.rectangle((0,0,width,174),fill='#10241c')
    draw.rounded_rectangle((46,38,114,106),radius=20,fill='#d6ff8f')
    draw.text((80,71),'F',font=ImageFont.truetype(str(FONT),40),fill='#10241c',anchor='mm')
    draw.text((140,66),'FARSTAR',font=ImageFont.truetype(str(FONT),44),fill='white',anchor='lm')
    draw.text((140,119),'YOUR PERSONAL CONNECTION',font=ImageFont.truetype(str(FONT),21),fill='#d6ff8f',anchor='lm')
    draw.rectangle((0,174,width,180),fill='#d6ff8f')
    card.paste(code,((width-code.width)//2,200+(640-code.height)//2))
    email=service['email']
    size=36
    while size>16 and draw.textlength(email,font=ImageFont.truetype(str(FONT),size))>700:
        size-=2
    draw.text((400,865),email,font=ImageFont.truetype(str(FONT),size),fill='#10241c',anchor='mt')
    volume=service['total_bytes']/1024**3
    expiry=datetime.fromtimestamp(service['expires_at'],ZoneInfo('Asia/Tehran')).strftime('%Y-%m-%d')
    draw.text((400,930),f'{volume:g} GB  |  VALID UNTIL {expiry}',font=ImageFont.truetype(str(FONT),23),fill='#44594e',anchor='mt')
    draw.text((400,990),'SCAN IN HAPP  /  KEEP YOUR LINK PRIVATE',font=ImageFont.truetype(str(FONT),19),fill='#44594e',anchor='mt')
    output=io.BytesIO()
    card.save(output,'PNG',optimize=True)
    return output.getvalue()
