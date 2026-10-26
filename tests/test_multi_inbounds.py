import concurrent.futures
import json
import sqlite3

import httpx
import pytest

from farstar.db import Database, SCHEMA
from farstar.panel import Panel, PanelError
from farstar.services import Provisioner
from farstar.store import DomainError


def all_inbounds(store,panel):
    store.save_server({'all_inbounds':True},1)
    panel.inbound_rows=[{'id':n,'enable':True,'protocol':'vless'} for n in [7,8,9,10]]


async def purchase(store,panel):
    order=store.create_order(1,1)
    store.pay_wallet(order['id'],1)
    worker=Provisioner(store,panel)
    await worker.process_one()
    return worker,store.order(order['id'])


async def test_one_client_shared_across_all_inbounds_and_number_continues(store,panel):
    all_inbounds(store,panel)
    panel.clients['FARSTAR_405']={'client':{'email':'FARSTAR_405','subId':'manual'},'inboundIds':[7]}
    _,order=await purchase(store,panel)
    assert order['status']=='completed'
    service=store.db.one('SELECT * FROM services')
    assert service['email']=='FARSTAR_406'
    assert panel.clients[service['email']]['inboundIds']==[7,8,9,10]
    assert panel.adds==1 and len(panel.clients)==2
    _,order2=await purchase(store,panel)
    assert json.loads(order2['snapshot'])['email']=='FARSTAR_407'


async def test_partial_create_retries_only_missing_memberships(store,panel):
    all_inbounds(store,panel)
    real_add=panel.add
    async def partial(client,inbound_ids):
        await real_add(client,[7,8])
        raise PanelError('ambiguous partial create')
    panel.add=partial
    worker,order=await purchase(store,panel)
    assert order['status']=='paid'
    email=json.loads(order['snapshot'])['email']
    with store.db.transaction() as con:
        con.execute('UPDATE orders SET next_attempt=0')
    panel.add=real_add
    panel.fail_after_attach=True
    await worker.process_one()
    assert store.order(order['id'])['status']=='paid'
    with store.db.transaction() as con:
        con.execute('UPDATE orders SET next_attempt=0')
    await worker.process_one()
    assert store.order(order['id'])['status']=='completed'
    assert panel.clients[email]['inboundIds']==[7,8,9,10]
    assert panel.adds==1 and panel.attachments==[(email,[9,10])]
    assert json.loads(store.order(order['id'])['snapshot'])['email']==email


async def test_new_inbound_sync_preserves_identity_quota_and_existing_attachments(store,panel):
    all_inbounds(store,panel)
    worker,_=await purchase(store,panel)
    service=store.db.one('SELECT * FROM services')
    before=panel.clients[service['email']]['client'].copy()
    panel.inbound_rows.extend([{'id':11,'enable':True,'protocol':'vless'},
                              {'id':12,'enable':False,'protocol':'vless'},
                              {'id':13,'enable':True,'protocol':'socks'}])
    panel.clients[service['email']]['inboundIds'].append(99)
    await worker.sync_service(service)
    await worker.sync_service(service)
    assert panel.clients[service['email']]['client']==before
    assert panel.clients[service['email']]['inboundIds']==[7,8,9,10,11,99]
    assert panel.attachments==[(service['email'],[11])]
    assert panel.updates==0


async def test_foreign_identity_is_never_attached(store,panel):
    all_inbounds(store,panel)
    worker,_=await purchase(store,panel)
    service=store.db.one('SELECT * FROM services')
    panel.clients[service['email']]['client']['subId']='foreign-owner'
    panel.inbound_rows.append({'id':11,'enable':True,'protocol':'vless'})
    with pytest.raises(DomainError,match='شناسه'):
        await worker.sync_service(service)
    assert not panel.attachments


def test_concurrent_reservations_and_restart_do_not_reuse_numbers(store):
    orders=[store.create_order(1,1) for _ in range(3)]
    for order in orders:
        store.pay_wallet(order['id'],1)
    def reserve(order):return store.reserve_client_name(order['id'],405)['email']
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        names=list(pool.map(reserve,orders))
    assert set(names)=={'FARSTAR_406','FARSTAR_407','FARSTAR_408'}
    assert reserve(orders[0])==names[0]
    from farstar.store import Store
    fresh=Store(store.db,store.settings)
    assert fresh.reserve_client_name(orders[0]['id'],900)['email']==names[0]


async def test_external_number_race_is_skipped_without_touching_foreign_client(store,panel):
    real_get=panel.get_client
    async def raced(email):
        if email=='FARSTAR_405' and email not in panel.clients:
            panel.clients[email]={'client':{'email':email,'subId':'someone-else'},'inboundIds':[7]}
        return await real_get(email)
    panel.get_client=raced
    _,order=await purchase(store,panel)
    assert order['status']=='completed'
    assert json.loads(order['snapshot'])['email']=='FARSTAR_406'
    assert panel.clients['FARSTAR_405']['client']['subId']=='someone-else'


async def test_paged_number_scan_and_multi_attach_use_official_api(store):
    calls=[]
    def handle(request):
        calls.append(request)
        if request.url.path.endswith('/list/paged'):
            page=int(request.url.params['page'])
            obj={'items':[{'email':'FARSTAR_405' if page==1 else 'farstar_1002'},
                          {'email':'FARSTAR_8000-invalid'}], 'total':201}
        else:
            obj=None
        return httpx.Response(200,json={'success':True,'obj':obj})
    async with Panel(store.server(1),{'token':'private-token'},transport=httpx.MockTransport(handle)) as p:
        assert await p.highest_client_number('FARSTAR_')==1002
        await p.add({'email':'FARSTAR_1003'},[7,8,9,10])
        await p.attach('FARSTAR_1003',[10,8,8])
    assert [r.url.params['page'] for r in calls[:2]]==['1','2']
    assert json.loads(calls[2].content)['inboundIds']==[7,8,9,10]
    assert calls[3].url.path.endswith('/clients/FARSTAR_1003/attach')
    assert json.loads(calls[3].content)=={'inboundIds':[8,10]}


def test_upgrade_old_database_keeps_single_inbound_default(tmp_path):
    path=tmp_path/'legacy.db'
    with sqlite3.connect(path) as con:
        con.executescript(SCHEMA.replace(' all_inbounds INTEGER NOT NULL DEFAULT 0,\n',''))
        con.execute("INSERT INTO meta VALUES ('owner-value','preserved')")
    db=Database(path)
    db.initialize()
    db.initialize()
    assert 'all_inbounds' in {r['name'] for r in db.all('PRAGMA table_info(servers)')}
    assert db.one("SELECT value FROM meta WHERE key='owner-value'")['value']=='preserved'


async def test_renewal_adds_new_inbound_without_new_name_or_double_quota(store,panel):
    all_inbounds(store,panel)
    worker,original=await purchase(store,panel)
    service=store.db.one('SELECT * FROM services')
    before=panel.clients[service['email']]['client'].copy()
    panel.inbound_rows.append({'id':11,'enable':True,'protocol':'vless'})
    order=store.create_order(1,1,service_id=original['service_id'])
    store.pay_wallet(order['id'],1)
    panel.fail_after_update=True
    await worker.process_one()
    with store.db.transaction() as con:
        con.execute('UPDATE orders SET next_attempt=0')
    await worker.process_one()
    after=panel.clients[service['email']]['client']
    assert store.order(order['id'])['status']=='completed'
    assert json.loads(store.order(order['id'])['snapshot'])['email']==service['email']
    assert after['uuid']==before['uuid'] and after['subId']==before['subId']
    assert after['totalGB']==2*before['totalGB']
    assert after['expiryTime']==before['expiryTime']+30*86400000
    assert panel.adds==1 and panel.clients[service['email']]['inboundIds']==[7,8,9,10,11]
