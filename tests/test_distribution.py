import base64
import io
import json
import zipfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from PIL import Image

from farstar import licensing
from farstar.api import create_app
from farstar.branding import branding
from farstar.panels import inbound_targets
from farstar.services import Provisioner
from farstar.store import DomainError, now


@pytest.fixture
def signer(monkeypatch):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(licensing, 'PUBLIC_KEY', key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex())
    def sign(payload):
        return {'payload': payload, 'signature': base64.b64encode(key.sign(licensing.canonical(payload))).decode()}
    return sign


def payload(store, **values):
    return dict(license_id='test', installation_id='a' * 32, bot_id=123,
                machine_hash=licensing.machine_fingerprint(),
                status='active', issued_at=now(), expires_at=now()+30*86400,
                lease_until=now()+86400, grace_until=now()+3*86400, **values)


def enable(store):
    store.settings.license_required = True
    store.settings.bot_token = '123:test'
    store.settings.license_installation_id = 'a' * 32
    store.settings.license_installation_token = 'token' * 10


def save_lease(store, envelope):
    with store.db.transaction() as con:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('license:lease',?)", (json.dumps(envelope),))


def test_sales_stop_but_paid_delivery_and_existing_identity_survive(store, panel):
    order = store.create_order(1, 1)
    store.pay_wallet(order['id'], 1)
    enable(store)
    with pytest.raises(DomainError, match='لایسنس'):
        store.create_order(1, 1)
    assert store.order(order['id'])['status'] == 'paid'


async def test_paid_order_completes_with_missing_license(store, panel):
    order = store.create_order(1, 1)
    store.pay_wallet(order['id'], 1)
    enable(store)
    assert await Provisioner(store, panel).process_one()
    assert store.order(order['id'])['status'] == 'completed'


def test_signed_lease_cannot_move_or_change_expiry(store, signer):
    enable(store)
    lease = signer(payload(store))
    save_lease(store, lease)
    assert licensing.client(store).status()['can_sell']
    lease['payload']['expires_at'] += 999999
    save_lease(store, lease)
    assert not licensing.client(store).status()['can_sell']
    lease = signer({**payload(store), 'installation_id': 'b'*32})
    save_lease(store, lease)
    assert not licensing.client(store).status()['can_sell']
    lease = signer({**payload(store), 'bot_id': 999})
    save_lease(store, lease)
    assert not licensing.client(store).status()['can_sell']
    save_lease(store, signer({**payload(store), 'machine_hash': '0'*64}))
    assert not licensing.client(store).status()['can_sell']


async def test_outage_grace_is_bounded_and_revoke_is_signed(store, signer):
    enable(store)
    save_lease(store, signer({**payload(store), 'issued_at': now()-2*86400, 'lease_until': now()-3600,
                             'grace_until': now()+3600}))
    assert licensing.client(store).status()['offline_grace']
    def down(request):
        raise httpx.ConnectError('offline')
    await licensing.client(store).refresh(httpx.MockTransport(down))
    assert licensing.client(store).status()['can_sell']
    revoked = signer({**payload(store), 'status': 'revoked'})
    await licensing.client(store).refresh(httpx.MockTransport(lambda r: httpx.Response(200, json=revoked)))
    assert not licensing.client(store).status()['can_sell']
    expired = signer({**payload(store), 'issued_at': now()-4*86400, 'lease_until': now()-3*86400,
                      'grace_until': now()-86400})
    save_lease(store, expired)
    assert not licensing.client(store).status()['can_sell']


async def test_all_protocols_and_node_targets_use_shared_client(store, panel):
    panel.inbound_rows = [dict(id=i+1, enable=True, protocol=p, nodeId=4 if i % 2 else None)
                          for i,p in enumerate(['vless','vmess','trojan','shadowsocks','hysteria','tuic'])]
    store.save_server({'all_inbounds': True, 'protocols': [], 'node_ids': []}, 1)
    order = store.create_order(1, 1)
    store.pay_wallet(order['id'], 1)
    assert await Provisioner(store, panel).process_one()
    record = next(iter(panel.clients.values()))
    assert record['inboundIds'] == [1,2,3,4,5,6]
    assert panel.adds == 1
    assert inbound_targets({'protocols':['vmess','tuic'], 'node_ids':[4]}, panel.inbound_rows) == [2,6]
    with pytest.raises(DomainError):
        store.save_server({'node_ids':[9]}, 1)


def test_branding_is_plain_text_persistent_and_one_bounded_logo(store):
    b = branding(store)
    b.save('فروشگاه تست', {'welcome':'<script>bad()</script>', 'buy_button':'خرید از من'}, actor=999)
    assert b.text('buy_button') == 'خرید از من'
    assert b.data()['app_name'] == 'فروشگاه تست'
    with pytest.raises(DomainError):
        b.save(texts={'unknown':'text'})
    with pytest.raises(DomainError):
        b.save(texts={'buy_button':'x\ny'})
    for color in ['red','green']:
        out=io.BytesIO()
        Image.new('RGB',(800,800),color).save(out,'PNG')
        b.save_logo(out.getvalue(), 999)
    assert len(b.logo()) < 260000
    assert store.db.one("SELECT COUNT(*) n FROM meta WHERE key='brand:logo'")['n'] == 1
    with pytest.raises(DomainError):
        b.save_logo(b'<svg onload="alert(1)"></svg>')


def test_public_install_has_no_issuer_routes_or_buttons(settings):
    app = create_app(settings, run_background=False)
    with TestClient(app) as http:
        assert http.post('/license-api/check',json={'license_key':'bad'}).status_code == 404
        assert http.get('/api/owner/licenses').status_code == 404
        assert not http.get('/mini-api/context').json()['license_issuer']
        assert http.get('/owner-assets/licenses.js').status_code == 404
        assert http.put('/api/branding',json={'app_name':'changed'}).status_code == 401


async def test_native_admin_server_wizard_and_live_brand_text(store, panel):
    from aiogram.types import Update
    from aiogram.methods import SendMessage
    from test_inline_admin import setup, click
    from test_bot import message
    session, bot, dp = setup(store, panel)
    await click(dp, bot, 'adm:newserver', 999)
    replies=['Second panel','Europe','https://other.example/private','new-api-token',
             'https://sub.example/sub','5','vmess,trojan,shadowsocks','0,4','200']
    for index,text in enumerate(replies,2):
        await dp.feed_update(bot,Update(update_id=index,message=message(text,user_id=999)))
    server=store.server(2)
    assert server['node_ids']==[0,4] and server['protocols']==['shadowsocks','trojan','vmess']
    assert store.vault.open(server['credentials'])['token']=='new-api-token'
    assert store.user(999)['state']==''
    branding(store).save('Custom name',{'welcome':'hello <user>','buy_button':'خرید دلخواه'})
    await dp.feed_update(bot,Update(update_id=40,message=message('/start')))
    sent=[x for x in session.calls if isinstance(x,SendMessage)]
    assert any('hello &lt;user&gt;' in x.text for x in sent)
    assert any(b.text=='خرید دلخواه' for x in sent if x.reply_markup and hasattr(x.reply_markup,'inline_keyboard')
               for row in x.reply_markup.inline_keyboard for b in row)
    assert not any(b.callback_data=='license:home' for x in sent if x.reply_markup and hasattr(x.reply_markup,'inline_keyboard')
                   for row in x.reply_markup.inline_keyboard for b in row)


def installer():
    spec=spec_from_file_location('farstar_installer',Path(__file__).parents[1]/'scripts/install.py')
    module=module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_rejects_traversal_and_parses_unicode_env(tmp_path):
    script=installer()
    raw=io.BytesIO()
    with zipfile.ZipFile(raw,'w') as z:
        z.writestr('repo/safe.txt','safe')
        z.writestr('repo/../../outside.txt','bad')
    with pytest.raises(RuntimeError):
        script.archive_members(raw.getvalue(),tmp_path/'release')
    assert not (tmp_path/'outside.txt').exists()
    from dotenv import dotenv_values
    parsed=dotenv_values(stream=io.StringIO(script.env_text({'APP_NAME':'فروشگاه تست','ADMIN_IDS':[123],
                  'SECRET_KEY':'x"$`\\z','COOKIE_SECURE':True})))
    assert parsed['APP_NAME']=='فروشگاه تست'
    assert parsed['SECRET_KEY']=='x"$`\\z'
    assert json.loads(parsed['ADMIN_IDS']) == [123]


def test_installer_invalid_license_precedes_all_mutations(tmp_path, monkeypatch):
    script=installer()
    monkeypatch.setattr(script.os, 'geteuid', lambda:0, raising=False)
    monkeypatch.setattr(script, 'ask', lambda *a,**kw:'FS-'+'a'*40)
    def reject(*a, **kw):
        raise RuntimeError('invalid license')
    monkeypatch.setattr(script, 'request', reject)
    monkeypatch.setattr(script, 'ROOT', tmp_path/'must-not-exist')
    with pytest.raises(RuntimeError, match='invalid license'):
        script.main()
    assert not script.ROOT.exists()


def test_installer_full_wizard_with_isolated_host_boundaries(tmp_path, monkeypatch):
    script=installer()
    for name, path in [('ROOT','opt'),('STATE','state'),('CONFIG','etc'),('UNIT','unit.service')]:
        monkeypatch.setattr(script, name, tmp_path/path)
    monkeypatch.setattr(script.os, 'geteuid', lambda:0, raising=False)
    answers=iter(['FS-'+'a'*40,'123:'+'b'*35,'1','My Shop','','','','','18080'])
    prompts=[]
    def answer(prompt,*a,**kw):
        prompts.append(prompt)
        value=next(answers)
        assert not kw.get('validate') or kw['validate'](value)
        return value
    monkeypatch.setattr(script, 'ask', answer)
    monkeypatch.setattr(script.shutil, 'which',lambda _: '/usr/bin/fake')
    monkeypatch.setattr(script.shutil, 'chown',lambda *a,**kw:None)
    monkeypatch.setattr(script, 'port_available',lambda *a:True)
    calls=[]
    def run(command, **kwargs):
        calls.append(command)
    monkeypatch.setattr(script, 'run',run)
    monkeypatch.setattr(script.subprocess, 'run',lambda *a,**kw:type('Result',(),{'returncode':0})())
    monkeypatch.setattr(Path, 'symlink_to',lambda self,target,**kw:self.write_text(str(target)))
    original_read=Path.read_bytes
    original_is_file=Path.is_file
    monkeypatch.setattr(Path, 'is_file',lambda p:True if str(p).replace('\\','/')=='/etc/machine-id' else original_is_file(p))
    monkeypatch.setattr(Path, 'read_bytes',lambda p: b'machine-test' if str(p).replace('\\','/')=='/etc/machine-id' else original_read(p))
    raw=io.BytesIO()
    unit=(Path(__file__).parents[1]/'docs/farstar-robot.service').read_text()
    with zipfile.ZipFile(raw,'w') as z:
        z.writestr('repo/docs/farstar-robot.service',unit)
        z.writestr('repo/requirements.lock','')
    import hashlib
    release={'commit':'a'*40,'sha256':hashlib.sha256(raw.getvalue()).hexdigest()}
    def request(url, data=None):
        if url.endswith('/check'):
            assert not script.ROOT.exists()
            return {'valid':True,'owner_id':1,'release':release}
        if url.endswith('/getMe'):
            return {'ok':True,'result':{'id':123,'username':'test_bot'}}
        if url.endswith('/getWebhookInfo'):
            return {'ok':True,'result':{'url':''}}
        assert url.endswith('/activate')
        assert data['admin_id']==1 and len(data['installation_id'])==32
        return {'installation_token':'install-token','lease':{'signed':'lease'},'release':release}
    monkeypatch.setattr(script, 'request', request)
    monkeypatch.setattr(script.urllib.request, 'urlopen',lambda *a,**kw:io.BytesIO(raw.getvalue()))
    script.main()
    assert prompts[0]=='لایسنس شخصی'
    assert (script.CONFIG/'bot.env').exists()
    assert script.UNIT.read_text()==unit
    assert all('x-ui' not in ' '.join(map(str,c)) and 'xray' not in ' '.join(map(str,c)) for c in calls)
    assert all('FS-' not in ' '.join(map(str,c)) and '123:' not in ' '.join(map(str,c)) for c in calls)
