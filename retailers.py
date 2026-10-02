"""Public stock checks for 30th Celebration listings.

Target uses the same product API as target.com. Best Buy uses Best Buy's
official developer API when BESTBUY_API_KEY is set. Other retailers are read
with one normal page request. If a site refuses, the result says so. This
module does not buy anything and does not try to get around blocks.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import httpx

from catalog import is_30th_celebration

TARGET_KEY = os.getenv(
    "TARGET_API_KEY",
    "9f36aeafbe60771e321a7cc95a78140772ab3e96",
)
IN_STOCK = {"IN_STOCK", "AVAILABLE", "LIMITED_STOCK", "PRE_ORDER", "PREORDER"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class RetailerError(Exception):
    def __init__(self, message: str, *, cooldown: bool = False):
        super().__init__(message)
        self.cooldown = cooldown


@dataclass
class Stock:
    status: str
    detail: str
    name: str | None = None
    price: str | None = None
    url: str | None = None
    shipping: bool = False
    pickup: bool = False


@dataclass
class FoundProduct:
    retailer: str
    sku: str
    name: str
    url: str
    price: str | None = None


def parse_watch(text: str) -> tuple[str, str]:
    raw = text.strip()
    if raw.lower().startswith("http"):
        return _parse_url(raw)
    parts = raw.split()
    aliases = {
        "target": "target",
        "bestbuy": "bestbuy",
        "best": "bestbuy",
        "walmart": "walmart",
        "gamestop": "gamestop",
        "amazon": "amazon",
        "pokemoncenter": "pokemoncenter",
        "pokemon": "pokemoncenter",
        "pc": "pokemoncenter",
    }
    if len(parts) == 2 and parts[0].lower() in aliases and re.fullmatch(r"[A-Za-z0-9-]+", parts[1]):
        return aliases[parts[0].lower()], parts[1]
    raise ValueError(
        "Paste a product URL, or type a retailer and id like `target 1010892076` "
        "or `bestbuy 6685559`."
    )


def product_url(retailer: str, sku: str) -> str:
    if retailer == "target":
        return f"https://www.target.com/p/-/A-{sku}"
    if retailer == "bestbuy":
        return f"https://www.bestbuy.com/site/searchpage.jsp?id=pcat17071&st=sku%3A{sku}"
    if retailer == "walmart":
        return f"https://www.walmart.com/ip/{sku}"
    if retailer == "gamestop":
        return f"https://www.gamestop.com/search/?q={sku}&lang=default"
    if retailer == "amazon":
        return f"https://www.amazon.com/dp/{sku}"
    if retailer == "pokemoncenter":
        return f"https://www.pokemoncenter.com/product/{sku}"
    raise ValueError(f"Unknown retailer {retailer}")


def _parse_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path
    if "target.com" in host:
        match = re.search(r"/A-(\d+)", path, re.I)
        if not match:
            raise ValueError("That Target link has no product id (A-########).")
        return "target", match.group(1)
    if "bestbuy.com" in host:
        match = re.search(r"(?:sku[iI]d=|/|sku%3A)(\d{6,8})", url)
        if not match:
            raise ValueError(
                "That Best Buy link has no numeric SKU. Add it as `bestbuy 6685559`."
            )
        return "bestbuy", match.group(1)
    if "walmart.com" in host:
        match = re.search(r"/ip/(?:[^/]+/)?(\d+)", path)
        if not match:
            raise ValueError("That Walmart link has no item id.")
        return "walmart", match.group(1)
    if "gamestop.com" in host:
        match = re.search(r"/(\d+)\.html", path)
        if not match:
            raise ValueError("That GameStop link has no product id.")
        return "gamestop", match.group(1)
    if "amazon.com" in host:
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", path, re.I)
        if not match:
            raise ValueError("That Amazon link has no ASIN.")
        return "amazon", match.group(1).upper()
    if "pokemoncenter.com" in host:
        match = re.search(r"/product/([^/?#]+)", path)
        if not match:
            raise ValueError("That Pokémon Center link has no product id.")
        return "pokemoncenter", match.group(1)
    raise ValueError(
        "Supported links: Target, Best Buy, Walmart, GameStop, Amazon, and Pokémon Center."
    )


def _money(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value if value.startswith("$") else f"${value}"
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return None


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    return html.unescape(text).strip()


def _raise_for_status(response: httpx.Response, retailer: str) -> None:
    if response.status_code in {403, 429, 435}:
        raise RetailerError(
            f"{retailer} refused the stock check ({response.status_code}). "
            "The bot will pause that store and will not try to get around the block.",
            cooldown=True,
        )
    if response.status_code >= 400:
        raise RetailerError(f"{retailer} returned HTTP {response.status_code}.")


class Checker:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        # 223 is Target Milwaukee Chase. Shipping stock is online; this id is for pickup.
        self.store_id = os.getenv("TARGET_STORE_ID", "223").strip() or "223"
        self.zip_code = os.getenv("TARGET_ZIP", "53202").strip() or "53202"
        self.detail_blocked = False

    async def ensure_store(self) -> str:
        return self.store_id

    async def check(self, retailer: str, sku: str, *, with_details: bool = False) -> Stock:
        if retailer == "target":
            return await self.check_target(sku, with_details=with_details)
        if retailer == "bestbuy":
            return await self.check_bestbuy(sku)
        if retailer == "walmart":
            return await self._page_status(
                "Walmart",
                product_url(retailer, sku),
                in_stock_markers=('"availabilityStatus":"IN_STOCK"',),
                out_markers=('"availabilityStatus":"OUT_OF_STOCK"',),
            )
        if retailer == "gamestop":
            return await self._page_status(
                "GameStop",
                f"https://www.gamestop.com/on/demandware.store/Sites-gamestop-us-Site/default/Product-Variation?pid={sku}",
                in_stock_markers=('"availability":"IN_STOCK"',),
                out_markers=('"availability":"NOT_AVAILABLE"', '"availability":"OUT_OF_STOCK"'),
            )
        if retailer == "amazon":
            return await self._page_status(
                "Amazon",
                product_url(retailer, sku),
                in_stock_markers=('"availabilityStatus":"IN_STOCK"',),
                out_markers=('"availabilityStatus":"OUT_OF_STOCK"',),
            )
        if retailer == "pokemoncenter":
            return await self._page_status(
                "Pokémon Center",
                product_url(retailer, sku),
                in_stock_markers=('"availability":"IN_STOCK"', '"stockLevel":"IN_STOCK"'),
                out_markers=('"availability":"OUT_OF_STOCK"', '"stockLevel":"OUT_OF_STOCK"'),
            )
        raise RetailerError(f"Unknown retailer {retailer}.")

    async def check_target(self, tcin: str, *, with_details: bool = False) -> Stock:
        store_id = await self.ensure_store()
        stock = await self._target_fulfillment(tcin, store_id)
        stock.url = product_url("target", tcin)
        if with_details:
            try:
                await asyncio.sleep(1.5)
                info = await self._target_pdp(tcin, store_id)
            except RetailerError as exc:
                self.detail_blocked = exc.cooldown
                return stock
            stock.name = info.name or stock.name
            stock.price = info.price or stock.price
        return stock

    async def _target_pdp(self, tcin: str, store_id: str) -> Stock:
        response = await self.client.get(
            "https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1",
            params={
                "key": TARGET_KEY,
                "tcin": tcin,
                "store_id": store_id,
                "pricing_store_id": store_id,
                "has_pricing_store_id": "true",
                "channel": "WEB",
                "page": f"/p/A-{tcin}",
            },
        )
        _raise_for_status(response, "Target")
        product = response.json().get("data", {}).get("product") or {}
        item = product.get("item") or {}
        description = item.get("product_description") or {}
        price = product.get("price") or {}
        return Stock(
            status="unknown",
            detail="",
            name=_clean(description.get("title")),
            price=_money(price.get("formatted_current_price") or price.get("current_retail")),
        )

    async def _target_fulfillment(self, tcin: str, store_id: str) -> Stock:
        response = await self.client.get(
            "https://redsky.target.com/redsky_aggregations/v1/web/product_fulfillment_v1",
            params={
                "key": TARGET_KEY,
                "tcin": tcin,
                "store_id": store_id,
                "zip": self.zip_code,
                "pricing_store_id": store_id,
                "has_pricing_store_id": "true",
                "scheduled_delivery_store_id": store_id,
                "channel": "WEB",
                "page": f"/p/A-{tcin}",
            },
        )
        _raise_for_status(response, "Target")
        fulfillment = (
            response.json().get("data", {}).get("product", {}).get("fulfillment") or {}
        )
        if not fulfillment:
            raise RetailerError("Target did not return fulfillment for that product.")
        shipping = fulfillment.get("shipping_options") or {}
        ship_state = str(shipping.get("availability_status") or "")
        quantity = shipping.get("available_to_promise_quantity") or 0
        store = (fulfillment.get("store_options") or [{}])[0]
        pickup = str((store.get("order_pickup") or {}).get("availability_status") or "")
        in_store = str((store.get("in_store_only") or {}).get("availability_status") or "")
        online = ship_state in IN_STOCK or (isinstance(quantity, (int, float)) and quantity > 0)
        local = pickup in IN_STOCK or in_store in IN_STOCK
        bits = [f"shipping {ship_state or 'unknown'}"]
        if isinstance(quantity, (int, float)) and quantity > 0:
            bits.append(f"available to promise {int(quantity)}")
        if pickup:
            bits.append(f"pickup {pickup}")
        if online or local:
            how = []
            if online:
                how.append("shipping" if ship_state != "PRE_ORDER" else "preorder")
            if local:
                how.append("pickup")
            return Stock(
                "in_stock",
                "In stock for " + " and ".join(how) + " (" + "; ".join(bits) + ")",
                shipping=online,
                pickup=local,
            )
        if ship_state in {"OUT_OF_STOCK", "UNAVAILABLE", "SOLD_OUT"} or fulfillment.get("sold_out"):
            return Stock("out_of_stock", "Out of stock (" + "; ".join(bits) + ")")
        return Stock("unknown", "Target status unclear (" + "; ".join(bits) + ")")

    async def search_target(self, query: str) -> list[FoundProduct]:
        store_id = await self.ensure_store()
        response = await self.client.get(
            "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2",
            params={
                "key": TARGET_KEY,
                "channel": "WEB",
                "count": "24",
                "offset": "0",
                "platform": "desktop",
                "pricing_store_id": store_id,
                "default_purchasability_filter": "false",
                "keyword": query,
                "page": f"/s/{query}",
            },
        )
        _raise_for_status(response, "Target")
        products = response.json().get("data", {}).get("search", {}).get("products") or []
        found = []
        for product in products:
            item = product.get("item") or {}
            title = _clean((item.get("product_description") or {}).get("title")) or ""
            if not is_30th_celebration(title):
                continue
            tcin = str(product.get("tcin") or "")
            if not tcin:
                continue
            price = _money((product.get("price") or {}).get("formatted_current_price"))
            found.append(
                FoundProduct(
                    "target",
                    tcin,
                    title,
                    product_url("target", tcin),
                    price,
                )
            )
        return found

    async def check_bestbuy(self, sku: str) -> Stock:
        api_key = os.getenv("BESTBUY_API_KEY", "").strip()
        if not api_key:
            raise RetailerError(
                "Best Buy needs a free developer API key. Add BESTBUY_API_KEY to .env."
            )
        response = await self.client.get(
            f"https://api.bestbuy.com/v1/products(sku={sku})",
            params={
                "apiKey": api_key,
                "format": "json",
                "show": "sku,name,salePrice,onlineAvailability,orderable,url,inStoreAvailability",
            },
        )
        _raise_for_status(response, "Best Buy")
        products = response.json().get("products") or []
        if not products:
            raise RetailerError(f"Best Buy has no product for SKU {sku}.")
        item = products[0]
        online = bool(item.get("onlineAvailability") or item.get("orderable"))
        name = _clean(item.get("name"))
        price = _money(item.get("salePrice"))
        url = item.get("url") or product_url("bestbuy", sku)
        if online:
            where = "online"
            if item.get("inStoreAvailability"):
                where += " and in store"
            return Stock("in_stock", f"In stock {where}", name, price, url)
        return Stock("out_of_stock", "Out of stock online", name, price, url)

    async def search_bestbuy(self, query: str) -> list[FoundProduct]:
        api_key = os.getenv("BESTBUY_API_KEY", "").strip()
        if not api_key:
            return []
        words = [part for part in re.split(r"\s+", query) if part][:4]
        search = "&".join(f"search={quote(word)}" for word in words)
        response = await self.client.get(
            f"https://api.bestbuy.com/v1/products({search})",
            params={
                "apiKey": api_key,
                "format": "json",
                "show": "sku,name,salePrice,url",
                "pageSize": "20",
            },
        )
        _raise_for_status(response, "Best Buy")
        found = []
        for item in response.json().get("products") or []:
            name = _clean(item.get("name")) or ""
            if not is_30th_celebration(name):
                continue
            sku = str(item.get("sku") or "")
            if not sku:
                continue
            found.append(
                FoundProduct(
                    "bestbuy",
                    sku,
                    name,
                    item.get("url") or product_url("bestbuy", sku),
                    _money(item.get("salePrice")),
                )
            )
        return found

    async def _page_status(
        self,
        retailer: str,
        url: str,
        *,
        in_stock_markers: tuple[str, ...],
        out_markers: tuple[str, ...],
    ) -> Stock:
        response = await self.client.get(url, follow_redirects=True)
        _raise_for_status(response, retailer)
        body = response.text
        lowered = body.lower()
        blocked = (
            "pardon our interruption",
            "access denied",
            "please verify you are a human",
            "px-captcha",
            "just a moment",
        )
        if any(sign in lowered for sign in blocked):
            raise RetailerError(
                f"{retailer} blocked the page request. The bot will not try to get around that.",
                cooldown=True,
            )
        in_stock = any(marker.lower() in lowered for marker in in_stock_markers)
        out = any(marker.lower() in lowered for marker in out_markers)
        if in_stock and not out:
            return Stock("in_stock", "Page shows the item as available", url=url)
        if out and not in_stock:
            return Stock("out_of_stock", "Page shows the item as unavailable", url=url)
        return Stock(
            "unknown",
            f"{retailer} did not show a clear stock status on the public page.",
            url=url,
        )
