"""Provider boundary for future panel adapters. Only tested Sanaei API is enabled."""
from .panel import Panel
from .store import DomainError

PROTOCOLS = {'vless', 'vmess', 'trojan', 'shadowsocks', 'hysteria', 'tuic'}


def panel_factory(server, credentials):
    if server.get('panel_type', 'sanaei') != 'sanaei':
        raise DomainError('این نوع پنل هنوز در این نسخه پشتیبانی نمی‌شود.')
    return Panel(server, credentials)


def inbound_targets(server, rows):
    selected = set(server.get('protocols') or PROTOCOLS)
    nodes = set(server.get('node_ids') or [])
    return sorted({row['id'] for row in rows if row.get('enable')
                   and row.get('protocol') in PROTOCOLS & selected
                   and (not nodes or (row.get('nodeId') or row.get('node_id') or 0) in nodes)})
