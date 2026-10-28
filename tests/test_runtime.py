import asyncio
import contextlib
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramForbiddenError
from aiogram.methods import GetUpdates, SendMessage

from farstar.api import create_app
from farstar.panel import panel_tls, _shared_tls
from farstar.runtime import TelegramTelemetry, runtime_watchdog


async def test_polling_health_reports_actual_requests_and_delivery_without_message_contents():
    state={'telegram':'connecting','telegram_error':None}
    monitor=TelegramTelemetry(state)
    await monitor(AsyncMock(return_value=[]),None,GetUpdates())
    assert state['telegram']=='connected' and state['telegram_last_poll']>0
    await monitor(AsyncMock(return_value=[object(),object()]),None,GetUpdates())
    assert state['telegram_updates_received']==2
    await monitor(AsyncMock(return_value=True),None,SendMessage(chat_id=123,text='private-message'))
    assert state['telegram_messages_sent']==1 and state['telegram_last_send']>0
    assert 'private-message' not in str(state) and '123' not in str(state)


async def test_poll_network_failure_is_visible_safe_and_recovers(caplog):
    state={}
    monitor=TelegramTelemetry(state)
    method=GetUpdates()
    fail=AsyncMock(side_effect=TelegramNetworkError(method=method,message='https://api.telegram.org/private-token'))
    with pytest.raises(TelegramNetworkError):
        await monitor(fail,None,method)
    assert state['telegram']=='reconnecting' and state['telegram_error']=='TelegramNetworkError'
    assert 'private-token' not in caplog.text
    await monitor(AsyncMock(return_value=[]),None,method)
    assert state['telegram']=='connected' and state['telegram_error'] is None


async def test_user_blocking_bot_does_not_report_global_poll_failure():
    state={'telegram':'connected'}
    method=SendMessage(chat_id=1,text='test')
    with pytest.raises(TelegramForbiddenError):
        await TelegramTelemetry(state)(AsyncMock(side_effect=TelegramForbiddenError(method=method,message='blocked')),None,method)
    assert state['telegram']=='connected'


async def test_watchdog_stops_healthy_signals_when_critical_task_stops(monkeypatch):
    calls=[]
    beat=asyncio.Event()
    def notify(msg):
        calls.append(msg)
        if msg=='WATCHDOG=1':
            beat.set()
    monkeypatch.setattr('farstar.runtime.notify_systemd',notify)
    event=asyncio.Event()
    critical=asyncio.create_task(event.wait(),name='critical-test')
    state={}
    watchdog=asyncio.create_task(runtime_watchdog([critical],state,interval=0.002))
    await asyncio.wait_for(beat.wait(),1)
    assert state['background_tasks_ok'] and 'WATCHDOG=1' in calls
    event.set()
    await asyncio.wait_for(watchdog,1)
    assert state['background_tasks_ok'] is False
    assert calls[-1].startswith('STATUS=Critical')
    assert 'STOPPING=1' not in calls
    assert any('READY=1' in s for s in calls)


async def test_watchdog_cancellation_does_not_survive_shutdown(monkeypatch):
    calls=[]
    monkeypatch.setattr('farstar.runtime.notify_systemd',lambda msg:calls.append(msg))
    task=asyncio.create_task(runtime_watchdog([],{},interval=0.002))
    await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    size=len(calls)
    await asyncio.sleep(0.01)
    assert len(calls)==size


def test_tls_store_is_reused_without_disabling_certificate_verification(monkeypatch):
    monkeypatch.delenv('SSL_CERT_FILE',raising=False)
    monkeypatch.delenv('SSL_CERT_DIR',raising=False)
    _shared_tls.cache_clear()
    first=panel_tls()
    assert all(panel_tls() is first for _ in range(100))
    assert first.verify_mode==ssl.CERT_REQUIRED and first.check_hostname
    assert first.cert_store_stats()['x509_ca']>0


def test_health_rejects_stopped_critical_task(settings):
    from fastapi.testclient import TestClient
    app=create_app(settings,run_background=False)
    client=TestClient(app)
    assert client.get('/health').status_code==200
    app.state.runtime['background_tasks_ok']=False
    assert client.get('/health').status_code==503


async def test_optional_menu_failure_does_not_prevent_bounded_polling(settings,monkeypatch):
    settings.bot_token='123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'
    session=SimpleNamespace(middleware=lambda m:None,close=AsyncMock())
    fake=SimpleNamespace(session=session,get_me=AsyncMock(return_value=SimpleNamespace(username='test_bot')),
        get_webhook_info=AsyncMock(return_value=SimpleNamespace(url='')),
        set_my_commands=AsyncMock(side_effect=TelegramBadRequest(method=GetUpdates(),message='menu rejected')))
    monkeypatch.setattr('aiogram.Bot',lambda *a,**kw:fake)
    calls=[]
    async def polling(bot,**kwargs):
        calls.append(kwargs)
        await asyncio.Event().wait()
    monkeypatch.setattr('farstar.bot.build_dispatcher',lambda *a:SimpleNamespace(start_polling=polling))
    app=create_app(settings)
    async with app.router.lifespan_context(app):
        for _ in range(30):
            if calls:
                break
            await asyncio.sleep(0.005)
        assert calls and calls[0]['tasks_concurrency_limit']==8
        assert calls[0]['polling_timeout']==20


async def test_monitor_fetches_inbounds_once_per_server_and_preserves_each_identity(store,panel):
    from farstar.services import Provisioner,Worker
    store.save_server({'all_inbounds':True},1)
    p=Provisioner(store,panel)
    for _ in range(3):
        order=store.create_order(1,1)
        store.pay_wallet(order['id'],1)
        await p.process_one()
    panel.inbound_rows.append({'id':8,'protocol':'vless','enable':True})
    panel.inbounds=AsyncMock(wraps=panel.inbounds)
    worker=Worker(store)
    worker.provisioner=p
    await worker.monitor()
    assert panel.inbounds.call_count==1
    assert all(set(r['inboundIds'])=={7,8} for r in panel.clients.values())
    assert panel.updates==0


async def test_unused_native_heap_is_reclaimed_off_event_loop_when_above_threshold(monkeypatch):
    from farstar.runtime import memory_maintenance
    state={}
    values=iter([400*1024**2,280*1024**2])
    monkeypatch.setattr('farstar.runtime.rss_bytes',lambda:next(values,200*1024**2))
    monkeypatch.setattr('farstar.runtime.collect_unused_memory',lambda:123)
    task=asyncio.create_task(memory_maintenance(state,interval=0.002))
    for _ in range(100):
        if state.get('memory_maintenance_at'):
            break
        await asyncio.sleep(0.005)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert state['memory_objects_collected']==123
    assert state['memory_released_bytes']==120*1024**2


async def test_partial_loop_stall_requests_recovery_even_if_loop_resumes(monkeypatch):
    calls=[]
    monkeypatch.setattr('farstar.runtime.notify_systemd',lambda msg:calls.append(msg))
    values=iter([0,40,40])
    state={}
    await asyncio.wait_for(runtime_watchdog([],state,interval=0.002,clock=lambda:next(values)),1)
    assert state['background_tasks_ok'] is False
    assert calls[-1].startswith('WATCHDOG=trigger')


async def test_sustained_high_memory_after_reclamation_requests_recovery(monkeypatch):
    from farstar.runtime import memory_maintenance
    monkeypatch.setattr('farstar.runtime.rss_bytes',lambda:400*1024**2)
    monkeypatch.setattr('farstar.runtime.collect_unused_memory',lambda:0)
    state={}
    await asyncio.wait_for(memory_maintenance(state,interval=0.002),1)
    assert state['high_memory_samples']==2 and state['memory_recovery_required'] is True


async def test_recovered_memory_resets_pressure_streak(monkeypatch):
    from farstar.runtime import memory_maintenance
    monkeypatch.setattr('farstar.runtime.rss_bytes',lambda:128*1024**2)
    state={'high_memory_samples':1}
    task=asyncio.create_task(memory_maintenance(state,interval=0.002))
    for _ in range(30):
        if state['high_memory_samples']==0:
            break
        await asyncio.sleep(0.005)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert state['high_memory_samples']==0 and not state.get('memory_recovery_required')
