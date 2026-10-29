"""Fresh-install wizard for Debian 12 / Ubuntu 24.04. Does not edit the VPN panel."""
import getpass
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ISSUER = 'https://sub.mystics.top/license-api'
ROOT = Path('/opt/farstar-robot')
STATE = Path('/var/lib/farstar-robot')
CONFIG = Path('/etc/farstar-robot')
UNIT = Path('/etc/systemd/system/farstar-robot.service')


def ask(prompt, default='', secret=False, validate=None):
    while True:
        value = (getpass.getpass if secret else input)(prompt + (f' [{default}]' if default else '') + ': ').strip() or default
        if validate is None or validate(value):
            return value
        print('مقدار معتبر نیست؛ دوباره وارد کن.')


def request(url, data=None):
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json', 'User-Agent': 'FARSTAR-Installer/1.4'})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(8192)).get('detail', 'درخواست رد شد.')
            if not isinstance(detail, str):
                detail = 'ساختار اطلاعات معتبر نیست.'
        except (ValueError, AttributeError):
            detail = 'درخواست رد شد.'
        raise RuntimeError(detail) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError('ارتباط امن با مرجع برقرار نشد. بعداً دوباره تلاش کن.') from None


def port_available(port, bind='127.0.0.1'):
    with socket.socket() as probe:
        try:
            probe.bind((bind, port))
            return True
        except OSError:
            return False


def run(command, **kwargs):
    # Never put tokens, passwords, license keys or API URLs in process arguments.
    return subprocess.run(command, check=True, **kwargs)


def env_text(values):
    # JSON double-quoted values are compatible with python-dotenv and systemd EnvironmentFile.
    return '\n'.join(key + '=' + (json.dumps(value, ensure_ascii=False) if isinstance(value, str)
                                 else 'true' if value is True else 'false' if value is False
                                 else json.dumps(value, separators=(',', ':'))) for key, value in values.items()) + '\n'


def archive_members(raw, release):
    """Validate every member before extracting anything; require one GitHub archive root."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    roots = {p.filename.split('/')[0] for p in z.infolist() if p.filename}
    if len(roots) != 1 or sum(p.file_size for p in z.infolist()) > 30 * 1024 * 1024:
        raise RuntimeError('ساختار بسته نصب معتبر نیست.')
    members = []
    for member in z.infolist():
        parts = Path(member.filename).parts[1:]
        if not parts:
            continue
        path = release.joinpath(*parts)
        if not path.resolve().is_relative_to(release.resolve()) or '\\' in member.filename or member.external_attr >> 16 & 0o170000 == 0o120000:
            raise RuntimeError('مسیر غیرمجاز در بسته نصب.')
        members.append((member, path))
    for member, path in members:
        if member.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o755)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(z.read(member))
            path.chmod(0o644)
    for directory in release.rglob('*'):
        if directory.is_dir():
            directory.chmod(0o755)


def main():
    if os.geteuid() != 0:
        raise RuntimeError('با root اجرا کن.')
    print('FARSTAR · نصب ربات فروش VPN\nمرجع دریافت و تمدید لایسنس: @FARSTAR_VPN_BOT')
    # This is deliberately the first question, before installing packages or changing the host.
    license_key = ask('لایسنس شخصی', secret=True, validate=lambda v: bool(re.fullmatch(r'FS-[A-Za-z0-9_-]{30,60}', v)))
    verified = request(ISSUER + '/check', {'license_key': license_key})
    print('✓ لایسنس معتبر است.')
    if ROOT.exists() or STATE.exists() or CONFIG.exists() or UNIT.exists():
        raise RuntimeError('نصب موجود پیدا شد؛ برای حفظ اطلاعات بازنویسی نشد. از راهنمای به‌روزرسانی استفاده کن.')
    if sys.version_info < (3, 11):
        raise RuntimeError('Python 3.11+ لازم است. Ubuntu 24.04 یا Debian 12 پیشنهاد می‌شود؛ پنل فعلی را ارتقا نمی‌دهیم.')
    if not shutil.which('systemctl') or not shutil.which('apt-get'):
        raise RuntimeError('این نصاب برای Debian/Ubuntu با systemd آماده شده است.')
    if not Path('/etc/machine-id').is_file():
        raise RuntimeError('شناسه پایدار سرور /etc/machine-id موجود نیست.')
    bot_token = ask('توکن ربات از BotFather', secret=True, validate=lambda v: bool(re.fullmatch(r'\d+:[A-Za-z0-9_-]{25,100}', v)))
    identity = request('https://api.telegram.org/bot' + bot_token + '/getMe')
    if not identity.get('ok'):
        raise RuntimeError('توکن تلگرام تأیید نشد.')
    webhook = request('https://api.telegram.org/bot' + bot_token + '/getWebhookInfo')
    if webhook.get('result', {}).get('url'):
        raise RuntimeError('این توکن وبهوک فعال دارد؛ برای حفظ ربات قبلی آن را تغییر ندادیم. ابتدا از سرویس قبلی جدا کن.')
    print('✓ ربات: @' + identity['result'].get('username', ''))
    admin_id = int(ask('آیدی عددی ادمین اصلی (همان حساب دریافت‌کننده لایسنس)', str(verified['owner_id']), validate=lambda v: v.isdecimal() and int(v) == verified['owner_id']))
    name = ask('نام نمایشی فروشگاه', 'فروشگاه من', validate=lambda v: 2 <= len(v) <= 64 and '\n' not in v)
    card = ask('شماره کارت پرداخت (۱۶ رقم؛ خالی = بعداً در مدیریت)', validate=lambda v: not v or bool(re.fullmatch(r'\d{16}', v)))
    holder = ask('نام صاحب کارت', validate=lambda v: not card or 3 <= len(v) <= 80)
    support = ask('نام کاربری پشتیبانی بدون @ (اختیاری)', validate=lambda v: not v or bool(re.fullmatch(r'[A-Za-z0-9_]{5,32}', v)))
    domain = ask('دامنه مینی‌اپ بدون https (خالی = فعلاً فقط ربات تلگرام)', validate=lambda v: not v or bool(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251})\.[A-Za-z]{2,63}', v)))
    cert = key = ''
    host = '0.0.0.0' if domain else '127.0.0.1'
    default_port = '18443' if domain else '18080'
    port = int(ask('پورت مستقل و آزاد برای ربات', default_port, validate=lambda v: v.isdecimal() and 1024 <= int(v) <= 65535 and port_available(int(v), host)))
    if domain:
        print('برای HTTPS از گواهی موجود همین دامنه استفاده می‌شود؛ پورت‌های پنل و وب‌سرور تغییر نمی‌کند.')
        cert = ask('مسیر مطلق fullchain.pem گواهی دامنه', validate=lambda v: v.startswith('/') and Path(v).is_file() and not any(c.isspace() for c in v))
        key = ask('مسیر مطلق private key گواهی دامنه', validate=lambda v: v.startswith('/') and Path(v).is_file() and not any(c.isspace() for c in v))
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            ssl.match_hostname(ssl._ssl._test_decode_cert(cert), domain)
        except (ssl.SSLError, OSError, ValueError):
            raise RuntimeError('گواهی، کلید و دامنه با هم تطابق ندارند.') from None
    installation_id = secrets.token_hex(16)
    print('بررسی نهایی لایسنس و آماده‌سازی نصب مستقل…')
    # Check first, fetch the checksummed release, then activate as late as possible.
    release_info = verified['release']
    commit, checksum = release_info['commit'], release_info['sha256']
    if not re.fullmatch(r'[a-f0-9]{40}', commit) or not re.fullmatch(r'[a-f0-9]{64}', checksum):
        raise RuntimeError('شناسه نسخه معتبر نیست.')
    archive_url = f'https://codeload.github.com/farstar-team/FARSTAR-ROBOT/zip/{commit}'
    with urllib.request.urlopen(archive_url, timeout=60) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != checksum:
        raise RuntimeError('اثر انگشت بسته نصب با مرجع یکسان نیست.')
    run(['apt-get', 'update', '-qq'])
    run(['apt-get', 'install', '-y', '--no-install-recommends', 'python3-venv', 'ca-certificates'])
    if not port_available(port, host):
        raise RuntimeError('پورت انتخابی دیگر آزاد نیست؛ دوباره اجرا کن.')
    ROOT.mkdir(mode=0o755)
    ROOT.chmod(0o755)
    release = ROOT / 'releases' / commit[:12]
    release.mkdir(parents=True, mode=0o755)
    release.parent.chmod(0o755)
    release.chmod(0o755)
    archive_members(raw, release)
    # Runtime files must be readable by the unprivileged service user.
    previous_umask = os.umask(0o022)
    try:
        run([sys.executable, '-m', 'venv', str(ROOT / 'venv')])
        python = ROOT / 'venv/bin/python'
        run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(release / 'requirements.lock')])
        run([str(python), '-m', 'pip', 'install', '--no-deps', str(release)])
    finally:
        os.umask(previous_umask)
    # Remove license-install privilege from the public build: it contains no issuer module or key.
    activated = request(ISSUER + '/activate', {'license_key': license_key, 'installation_id': installation_id,
                                            'bot_token': bot_token, 'admin_id': admin_id,
                                            'machine_hash': hashlib.sha256(b'farstar-install-v1:' + Path('/etc/machine-id').read_bytes().strip()).hexdigest()})
    if subprocess.run(['id', 'farstarbot'], capture_output=True).returncode != 0:
        run(['useradd', '--system', '--home', str(STATE), '--shell', '/usr/sbin/nologin', 'farstarbot'])
    STATE.mkdir(mode=0o700)
    CONFIG.mkdir(mode=0o700)
    shutil.chown(STATE, user='farstarbot', group='farstarbot')
    (ROOT / 'current').symlink_to(release, target_is_directory=True)
    (release / 'RELEASE').write_text(commit + '\n')
    url = f'https://{domain}:{port}' if domain else f'http://127.0.0.1:{port}'
    password = secrets.token_urlsafe(24)
    config = {'APP_NAME': name, 'BOT_TOKEN': bot_token, 'ADMIN_IDS': [admin_id],
              'ADMIN_USERNAME': 'admin', 'ADMIN_PASSWORD': password, 'SECRET_KEY': secrets.token_urlsafe(48),
              'DATABASE_PATH': str(STATE / 'farstar.db'), 'HOST': host, 'PORT': port,
              'PUBLIC_URL': url, 'MINI_APP_URL': url + '/app' if domain else '',
              'ADMIN_MINI_APP_URL': url + '/admin-app' if domain else '', 'COOKIE_SECURE': bool(domain),
              'CURRENCY': 'IRT', 'MANUAL_PAYMENTS': True, 'CARD_NUMBER': card, 'CARD_HOLDER': holder,
              'TRIAL_ENABLED': False, 'SUPPORT_USERNAME': support, 'LICENSE_REQUIRED': True,
              'LICENSE_INSTALLATION_ID': installation_id, 'LICENSE_INSTALLATION_TOKEN': activated['installation_token'],
              'TLS_CREDENTIALS': bool(domain)}
    env = CONFIG / 'bot.env'
    env.write_text(env_text(config), encoding='utf-8')
    env.chmod(0o600)
    # Insert only the signed lease into an otherwise empty business database.
    code = """import json,sys
from farstar.db import Database
from farstar.licensing import verify_envelope
envelope=json.load(sys.stdin)
verify_envelope(envelope,sys.argv[2],int(sys.argv[3]))
d=Database(sys.argv[1]);d.initialize()
with d.transaction() as c:c.execute(\"INSERT OR REPLACE INTO meta VALUES ('license:lease',?)\",(json.dumps(envelope),))
"""
    run([str(python), '-c', code, str(STATE / 'farstar.db'), installation_id, str(identity['result']['id'])], input=json.dumps(activated['lease']), text=True)
    for file in STATE.iterdir():
        shutil.chown(file, user='farstarbot', group='farstarbot')
    unit = (release / 'docs/farstar-robot.service').read_text()
    if domain:
        unit = unit.replace('[Service]', '[Service]\nLoadCredential=cert.pem:' + cert + '\nLoadCredential=key.pem:' + key)
    UNIT.write_text(unit)
    UNIT.chmod(0o644)
    # Preserve a private, resumable install record; no panel credentials were requested.
    record = CONFIG / 'INSTALLATION.json'
    record.write_text(json.dumps({'commit': commit, 'installation_id': installation_id, 'license_bot': '@FARSTAR_VPN_BOT', 'port': port}))
    record.chmod(0o600)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'farstar-robot.service'])
    run(['systemctl', 'is-active', '--quiet', 'farstar-robot.service'])
    print('\n✓ نصب کامل شد. در ربات خودت /start و بعد /admin را بزن.')
    print('از مدیریت ← سرورها، پنل ثنایی و نودها را اضافه و اتصال را بررسی کن؛ سپس پلن بساز.')
    print('رمز داشبورد فقط داخل /etc/farstar-robot/bot.env نگه داشته شد.')
    print('رابط محلی/مینی‌اپ:', url)
    if not domain:
        print(f'تونل SSH برای داشبورد: ssh -L {port}:127.0.0.1:{port} root@YOUR_SERVER_IP')
        print('فعال‌کردن مینی‌اپ بعداً: docs/INSTALL.md؛ دامنه HTTPS خودت لازم است.')
    else:
        print('در BotFather دامنه را ثبت کن؛ پورت انتخابی را در فایروال خودت باز کن. نصاب فایروال را تغییر نداده است.')
        print('پس از تمدید گواهی دامنه، فقط farstar-robot.service را restart کن تا گواهی جدید بارگذاری شود.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, OSError, KeyError, ValueError) as exc:
        # urllib/SSL exceptions may contain private request URLs: show no traceback or tokens.
        print('\nنصب تکمیل نشد: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__), file=sys.stderr)
        print('اطلاعات پنل و سرویس‌های VPN تغییر نکرد. نصب نیمه‌تمام را بازنویسی نمی‌کنیم؛ راهنما: @FARSTAR_VPN_BOT', file=sys.stderr)
        sys.exit(1)
