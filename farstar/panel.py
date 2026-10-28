"""3x-ui v3.11 contract: global /clients endpoints, never mutate inbound settings."""
from urllib.parse import quote, urlencode
import re
import os
import ssl
from functools import lru_cache
from pathlib import Path

import httpx
import certifi


@lru_cache(maxsize=1)
def _shared_tls(cafile, capath, ca_revision):
    # One CA store per process; a changed CA file creates a fresh verified context.
    return ssl.create_default_context(cafile=cafile,capath=capath)


def panel_tls():
    cafile=os.environ.get('SSL_CERT_FILE')
    capath=None
    if not cafile:
        capath=os.environ.get('SSL_CERT_DIR')
        if not capath:
            cafile=certifi.where()
    stat=Path(cafile or capath).stat()
    return _shared_tls(cafile,capath,(stat.st_mtime_ns,stat.st_size))


class PanelError(Exception):
    pass


class Panel:
    def __init__(self, server, credentials, transport=None):
        self.server = server
        self.credentials = credentials
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if credentials.get("token"):
            headers["Authorization"] = "Bearer " + credentials["token"]
        self.http = httpx.AsyncClient(
            base_url=server["url"].rstrip("/") + "/", headers=headers,
            timeout=httpx.Timeout(25, connect=10), verify=panel_tls(), follow_redirects=False,
            transport=transport,
        )
        self.logged_in = bool(credentials.get("token"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def request(self, method, path, payload=None, missing_ok=False):
        try:
            if not self.logged_in:
                login = await self.http.post("login", json={
                    "username": self.credentials.get("username", ""),
                    "password": self.credentials.get("password", ""),
                })
                if login.status_code != 200 or not login.json().get("success"):
                    raise PanelError("ورود به پنل ناموفق بود؛ توکن API یا نام کاربری را بررسی کنید.")
                self.logged_in = True
            response = await self.http.request(method, path.lstrip("/"), json=payload)
            if response.status_code in {401, 403}:
                raise PanelError("توکن پنل معتبر نیست یا دسترسی admin ندارد.")
            if response.status_code == 404:
                raise PanelError("مسیر API پیدا نشد؛ نسخه 3.11 و webBasePath پنل را بررسی کنید.")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or "success" not in data:
                raise PanelError("پاسخ پنل معتبر نیست؛ آدرس و مسیر پنل را بررسی کنید.")
            if not data.get("success"):
                # Only the documented not-found response means absence. A generic API failure
                # must never be interpreted as permission to create another client.
                message = str(data.get("msg", ""))
                if missing_ok and data.get("obj") is None and "not found" in message.lower():
                    return None
                raise PanelError("پنل درخواست را نپذیرفت؛ جزئیات را در پنل ثنایی بررسی کنید.")
            return data.get("obj")
        except (httpx.HTTPError, ValueError) as exc:
            # Never log HTTP URLs: a panel base path can itself be a secret.
            raise PanelError("ارتباط با پنل قطع شد یا پاسخ نامعتبر بود؛ عملیات دوباره بررسی می‌شود.") from exc

    async def inbound(self, inbound_id=None):
        return await self.request("GET", f"panel/api/inbounds/get/{inbound_id or self.server['inbound_id']}")

    async def inbounds(self):
        return await self.request('GET', 'panel/api/inbounds/list')

    async def highest_client_number(self, prefix):
        pattern = re.compile(re.escape(prefix) + r'(\d+)', re.I)
        highest = 0
        for page in range(1,1001):
            query = urlencode({'page':page,'pageSize':200,'search':prefix,'sort':'email','order':'asc'})
            result = await self.request('GET', 'panel/api/clients/list/paged?' + query)
            if not isinstance(result,dict) or not isinstance(result.get('items'),list) or type(result.get('total')) is not int:
                raise PanelError('فهرست کاربران پنل برای شماره‌گذاری معتبر نیست.')
            highest = max(highest, max((int(match[1]) for row in result['items']
                          if (match := pattern.fullmatch(row.get('email','')))), default=0))
            if page * 200 >= result['total']:
                return highest
            if not result['items']:
                raise PanelError('صفحه‌بندی کاربران پنل کامل نشد؛ شماره‌گذاری متوقف شد.')
        raise PanelError('تعداد کاربران برای شماره‌گذاری از حد بررسی بیشتر است.')

    async def get_client(self, email):
        return await self.request("GET", "panel/api/clients/get/" + quote(email, safe=""), missing_ok=True)

    async def add(self, client, inbound_ids=None):
        return await self.request("POST", "panel/api/clients/add", {
            "client": client, "inboundIds": inbound_ids if inbound_ids is not None else [self.server["inbound_id"]],
        })

    async def attach(self, email, inbound_ids):
        if inbound_ids:
            return await self.request('POST', 'panel/api/clients/' + quote(email, safe='') + '/attach',
                                      {'inboundIds': sorted(set(inbound_ids))})

    async def update(self, email, client):
        return await self.request("POST", "panel/api/clients/update/" + quote(email, safe=""), client)

    async def links(self, email):
        return await self.request("GET", "panel/api/clients/links/" + quote(email, safe=""))

    async def traffic(self, email):
        return await self.request("GET", "panel/api/clients/traffic/" + quote(email, safe=""))
