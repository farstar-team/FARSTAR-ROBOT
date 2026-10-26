"""Build schematic Persian Happ tutorials. Optional authoring deps: arabic-reshaper, python-bidi."""
import math
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

from farstar.happ import DEVICES

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / 'farstar/static/Vazirmatn-Regular.ttf'
OUT = ROOT / 'farstar/static'
W, H = 720, 960
INK, MUTED, LIME, BG = '#edf6f3', '#8bada4', '#b8f876', '#0b1716'


def rtl(text):
    return get_display(arabic_reshaper.reshape(text))


def text(draw, value, y, size=24, color=INK, center=True):
    value = rtl(value)
    font = ImageFont.truetype(str(FONT), size)
    width = draw.textlength(value, font=font)
    x = (W - width) / 2 if center else W - width - 55
    draw.text((x, y), value, font=font, fill=color)


def wrapped(draw, value, y, size=25, width=600, color=INK):
    line = ''
    for word in value.split():
        trial = (line + ' ' + word).strip()
        if draw.textlength(rtl(trial), font=ImageFont.truetype(str(FONT), size)) > width and line:
            text(draw, line, y, size, color)
            y += 42
            line = word
        else:
            line = trial
    if line:
        text(draw, line, y, size, color)


def scene(device, step, pulse):
    image = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(image)
    for radius in range(360, 0, -12):
        tint = int(20 * (1 - radius / 360))
        d.ellipse((380-radius, -100-radius, 380+radius, -100+radius), fill=(11, 23+tint, 22+tint//2))
    d.rounded_rectangle((35, 30, 685, 120), radius=24, fill='#112522', outline='#254239', width=2)
    text(d, 'FARSTAR  ×  HAPP', 43, 27, LIME)
    text(d, 'آموزش اتصال · ' + DEVICES[device]['name'], 85, 18, MUTED)
    tv = device in {'androidtv', 'appletv'}
    desktop = device in {'windows', 'macos', 'linux'}
    box = (80, 235, 640, 620) if desktop or tv else (180, 195, 540, 680)
    x1, y1, x2, y2 = box
    d.rounded_rectangle(box, radius=28, fill='#122b27', outline='#426258', width=3)
    d.rounded_rectangle((x1+12, y1+12, x2-12, y2-12), radius=21, fill='#f1f6f3')
    d.text((x1+33, y1+28), 'Happ', font=ImageFont.truetype(str(FONT), 25), fill='#18332b')
    if tv:
        d.rounded_rectangle((x1+34, y1+85, x2-34, y2-42), radius=17, fill='#e3ede8')
        d.text((x1+75, y1+107), 'Web Import', font=ImageFont.truetype(str(FONT), 28), fill='#244b3b')
        d.text((x1+114, y1+168), 'A1B2C3', font=ImageFont.truetype(str(FONT), 42), fill='#63833b')
        if step >= 2:
            d.rounded_rectangle((x1+50, y1+238, x2-50, y2-43), radius=11, fill='#bded8f')
            d.text((x1+75, y1+250), 'tv.happ.su', font=ImageFont.truetype(str(FONT), 23), fill='#22442e')
    else:
        cx, cy = (x1+x2)//2, y1+138
        d.ellipse((cx-46,cy-46,cx+46,cy+46),fill='#b9ee88' if step==3 else '#e1e9e3')
        d.arc((cx-20,cy-20,cx+20,cy+20),-55,235,fill='#456f3b',width=4)
        d.line((cx,cy-27,cx,cy-2),fill='#456f3b',width=4)
        px, py = x2-45, y1+42
        d.text((px-9,py-18), '+',font=ImageFont.truetype(str(FONT),33),fill='#315443')
        d.rounded_rectangle((x1+30,y1+225,x2-30,y1+295),radius=13,fill='#e3ede6')
        d.text((x1+47,y1+239),'Farstar',font=ImageFont.truetype(str(FONT),20),fill='#325442')
        d.rounded_rectangle((x1+30,y1+315,x2-30,y1+373),radius=13,fill='#bfe994' if step>=2 else '#e3ede6')
        d.text((x1+47,y1+328),'Server 01',font=ImageFont.truetype(str(FONT),18),fill='#325442')
        if step in {1,2}:
            d.rounded_rectangle((x1+27,y1+68,x2-27,y1+115),radius=9,fill='#24493c')
            d.text((x1+45,y1+77),'Paste / Clipboard',font=ImageFont.truetype(str(FONT),16),fill='white')
        points=[(cx,y1+260),(px,py),(cx,y1+342),(cx,cy)]
        tx,ty=points[step]
        radius=int(20+8*pulse)
        d.ellipse((tx-radius,ty-radius,tx+radius,ty+radius),outline='#74bd3e',width=4)
    for i in range(4):
        x=260+i*65
        d.rounded_rectangle((x,715,x+43,722),radius=3,fill=LIME if i==step else '#2c4a40')
    text(d, f'گام {step+1} از ۴', 745, 23, LIME)
    wrapped(d, DEVICES[device]['steps'][step], 795, 25)
    text(d, 'نمای شماتیک · ظاهر نسخه‌های برنامه ممکن است متفاوت باشد', 919, 16, MUTED)
    return image


def main():
    OUT.mkdir(exist_ok=True)
    for device in DEVICES:
        frames=[]
        for step in range(4):
            for tick in range(8):
                frame=scene(device,step,(math.sin(tick/8*math.pi*2)+1)/2)
                frames.append(frame)
        palette=frames[0].quantize(colors=96)
        frames=[frame.quantize(palette=palette,dither=Image.Dither.NONE) for frame in frames]
        target=OUT/f'happ-{device}.gif'
        frames[0].save(target,save_all=True,append_images=frames[1:],duration=280,loop=0,optimize=True,disposal=1)
        print(device,target.stat().st_size)


if __name__=='__main__':
    main()
