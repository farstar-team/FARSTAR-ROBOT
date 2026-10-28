"""Safe liveness signals; no chat contents, tokens or panel URLs enter diagnostics."""
import asyncio
import logging
import os
import socket
import time
import gc
import sys
import ctypes
from pathlib import Path

from aiogram.methods import GetUpdates, SendMessage, SendPhoto, SendAnimation, SendDocument

log=logging.getLogger(__name__)


class TelegramTelemetry:
    def __init__(self,state):
        self.state=state
        self.last_logged=('',0.0)

    async def __call__(self,make_request,bot,method):
        polling=isinstance(method,GetUpdates)
        try:
            result=await make_request(bot,method)
        except Exception as exc:
            if polling:
                kind=type(exc).__name__
                self.state['telegram']='polling_conflict' if kind=='TelegramConflictError' else 'reconnecting'
                self.state['telegram_error']=kind
                now=time.monotonic()
                if kind!=self.last_logged[0] or now-self.last_logged[1]>=60:
                    log.warning('Telegram polling request failed type=%s',kind)
                    self.last_logged=(kind,now)
            raise
        if polling:
            self.state.update(telegram='connected',telegram_error=None,telegram_last_poll=int(time.time()))
            if result:
                self.state['telegram_last_update']=int(time.time())
                self.state['telegram_updates_received']=self.state.get('telegram_updates_received',0)+len(result)
        elif isinstance(method,(SendMessage,SendPhoto,SendAnimation,SendDocument)):
            self.state['telegram_last_send']=int(time.time())
            self.state['telegram_messages_sent']=self.state.get('telegram_messages_sent',0)+1
        return result


def notify_systemd(message):
    address=os.environ.get('NOTIFY_SOCKET')
    if not address:
        return False
    if address.startswith('@'):
        address='\0'+address[1:]
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as channel:
            channel.setblocking(False)
            channel.sendto(message.encode(),address)
        return True
    except (OSError,AttributeError):
        return False


def rss_bytes():
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1])*1024
    except (OSError,ValueError):
        pass
    return None


def collect_unused_memory():
    freed=gc.collect(2)
    # Pillow/OpenSSL allocate outside Python's heap. Return unused glibc arenas too.
    if sys.platform=='linux':
        try:
            trim=ctypes.CDLL(None).malloc_trim
            trim.argtypes=[ctypes.c_size_t]
            trim.restype=ctypes.c_int
            trim(0)
        except (AttributeError,OSError):
            pass
    return freed


async def memory_maintenance(state,interval=60,threshold=256*1024**2):
    while True:
        await asyncio.sleep(interval)
        before=rss_bytes()
        if before is not None and before<threshold:
            state['high_memory_samples']=0
        if before is not None and before>=threshold:
            state['memory_objects_collected']=await asyncio.to_thread(collect_unused_memory)
            after=rss_bytes()
            state['memory_released_bytes']=max(0,before-(after or before))
            state['memory_maintenance_at']=int(time.time())
            state['high_memory_samples']=state.get('high_memory_samples',0)+1 if after and after>=384*1024**2 else 0
            if state['high_memory_samples']>=2:
                state['memory_recovery_required']=True
                return


async def runtime_watchdog(tasks,state,interval=10,clock=time.monotonic):
    """Runs on the app loop: a frozen loop cannot falsely report itself healthy."""
    notify_systemd('READY=1\nSTATUS=Farstar HTTP and background tasks started')
    expected=clock()+interval
    while True:
        await asyncio.sleep(interval)
        state['event_loop_lag_seconds']=round(max(0,clock()-expected),3)
        state['runtime_heartbeat']=int(time.time())
        state['rss_bytes']=rss_bytes()
        expected=clock()+interval
        if state.get('memory_recovery_required') or state['event_loop_lag_seconds']>30:
            state['background_tasks_ok']=False
            reason='memory_pressure' if state.get('memory_recovery_required') else 'event_loop_stall'
            log.error('Controlled recovery requested reason=%s',reason)
            notify_systemd('WATCHDOG=trigger\nSTATUS=Controlled recovery: '+reason)
            return
        stopped=[task.get_name() for task in tasks if task.done()]
        state['background_tasks_ok']=not stopped
        if stopped:
            log.error('Critical background task stopped names=%s',','.join(stopped))
            notify_systemd('STATUS=Critical background task stopped; watchdog recovery required')
            return
        notify_systemd('WATCHDOG=1')
