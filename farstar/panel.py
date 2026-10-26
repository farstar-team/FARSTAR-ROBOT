"""3x-ui v3.11 contract: global /clients endpoints, never mutate inbound settings."""
from urllib.parse import quote

import httpx


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
            timeout=httpx.Timeout(25, connect=10), verify=True, follow_redirects=False,
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

    async def inbound(self):
        return await self.request("GET", f"panel/api/inbounds/get/{self.server['inbound_id']}")

    async def get_client(self, email):
        return await self.request("GET", "panel/api/clients/get/" + quote(email, safe=""), missing_ok=True)

    async def add(self, client):
        return await self.request("POST", "panel/api/clients/add", {
            "client": client, "inboundIds": [self.server["inbound_id"]],
        })

    async def update(self, email, client):
        return await self.request("POST", "panel/api/clients/update/" + quote(email, safe=""), client)

    async def links(self, email):
        return await self.request("GET", "panel/api/clients/links/" + quote(email, safe=""))

    async def traffic(self, email):
        return await self.request("GET", "panel/api/clients/traffic/" + quote(email, safe=""))
