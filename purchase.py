"""Checkout for one Target 30th Celebration Elite Trainer Box.

Uses a visible Chrome window and the saved login in the Playwright profile.
Does not solve CAPTCHAs, skip queues, or refresh past a block. A real order
is submitted only when AUTO_PURCHASE=true and PURCHASE_DRY_RUN=false.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from retailers import product_url

log = logging.getLogger("restock.purchase")

ROOT = Path(__file__).resolve().parent
TARGET_ETB_TCIN = "1010892076"
PRODUCT_URL = product_url("target", TARGET_ETB_TCIN)
CART_URL = "https://www.target.com/cart"

AUTOMATIC_BLOCK = frozenset(
    {"succeeded", "uncertain", "submitting", "in_progress", "needs_attention"}
)
RETRY_BLOCK = frozenset({"succeeded", "uncertain", "submitting", "in_progress"})

ORDER_RE = re.compile(r"order\s*(?:number|#)\s*[:#]?\s*([0-9]{8,})", re.I)
TOTAL_RE = re.compile(
    r"(?:estimated\s+total|order\s+total|(?<![A-Za-z])total)\s*\$\s*([0-9,]+\.\d{2})",
    re.I,
)
QTY_RE = re.compile(r"(?:qty|quantity)\s*:?\s*(\d+)\b", re.I)
MULTI_ITEM_RE = re.compile(r"\b(?:[2-9]|[1-9]\d+)\s+items?\b", re.I)
HUMAN_TEXT = (
    "please verify you are a human",
    "verify you are human",
    "pardon our interruption",
    "are you a robot",
    "just a moment",
    "verify your identity",
    "enter the code we sent",
    "enter your verification code",
    "access denied",
)


class PurchaseAbort(Exception):
    def __init__(self, message: str, *, attention: bool = False):
        super().__init__(message)
        self.attention = attention


@dataclass
class PurchaseResult:
    status: str
    detail: str
    order_number: str | None = None
    total: str | None = None


@dataclass(frozen=True)
class PurchaseConfig:
    auto_purchase: bool
    dry_run: bool
    max_total: Decimal | None
    zip_code: str
    profile_dir: Path
    browser_channel: str | None

    @classmethod
    def from_env(cls) -> "PurchaseConfig":
        raw_max = os.getenv("MAX_ORDER_TOTAL", "").strip()
        max_total = None
        if raw_max:
            try:
                max_total = Decimal(raw_max.lstrip("$")).quantize(Decimal("0.01"))
            except InvalidOperation as exc:
                raise ValueError("MAX_ORDER_TOTAL must be a dollar amount like 70.00.") from exc
            if max_total <= 0:
                raise ValueError("MAX_ORDER_TOTAL must be greater than zero.")
        channel = os.getenv("BROWSER_CHANNEL", "chrome").strip()
        profile = Path(os.getenv("PLAYWRIGHT_PROFILE_DIR", "data/target-profile"))
        if not profile.is_absolute():
            profile = ROOT / profile
        return cls(
            auto_purchase=_flag("AUTO_PURCHASE", default=False),
            dry_run=_dry_run(),
            max_total=max_total,
            zip_code=os.getenv("TARGET_ZIP", "53202").strip() or "53202",
            profile_dir=profile,
            browser_channel=channel or None,
        )


def _flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true"}


def _dry_run() -> bool:
    raw = os.getenv("PURCHASE_DRY_RUN", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def submit_allowed(auto_purchase: bool, dry_run: bool) -> bool:
    return auto_purchase and not dry_run


def recover_status(status: str | None) -> tuple[str, str] | None:
    if status == "submitting":
        return (
            "uncertain",
            "The bot stopped while placing the order. Check Target order history before trying again.",
        )
    if status == "in_progress":
        return ("failed", "The bot stopped before placing the order.")
    return None


def is_target_etb(title: str) -> bool:
    text = title.lower().replace("é", "e")
    if re.search(r"\b(case|display)\b", text):
        return False
    return all(part in text for part in ("30th", "celebration", "elite trainer"))


def choose_fulfillment(shipping: bool, pickup: bool) -> str:
    if shipping:
        return "shipping"
    if pickup:
        return "pickup"
    raise ValueError("The product is not available for shipping or pickup.")


def assert_single_etb(text: str, tcin: str) -> None:
    lowered = text.lower().replace("é", "e")
    if tcin not in text:
        raise ValueError("The page does not show the Elite Trainer Box product id.")
    if not all(part in lowered for part in ("30th", "celebration", "elite trainer")):
        raise ValueError("The page does not show the 30th Celebration Elite Trainer Box.")
    quantities = QTY_RE.findall(text)
    if not quantities or any(qty != "1" for qty in quantities):
        raise ValueError("Could not confirm the quantity is exactly 1.")
    if MULTI_ITEM_RE.search(text):
        raise ValueError("The cart has more than one item.")
    if not re.search(r"\b1\s+item\b", lowered):
        raise ValueError("Could not confirm the cart contains exactly one item.")


def assert_checkout_safe(text: str, tcin: str, max_total: Decimal | None, zip_code: str) -> Decimal:
    assert_single_etb(text, tcin)
    lowered = text.lower()
    if not zip_code or not re.search(rf"\b{re.escape(zip_code)}\b", text):
        raise ValueError(f"Saved address or pickup store does not show ZIP {zip_code}.")
    if re.search(r"add (?:a )?shipping address|enter (?:your )?address", lowered):
        raise ValueError("No saved shipping address is selected.")
    if not re.search(r"\b(shipping|delivery|pickup|pick up)\b", lowered):
        raise ValueError("Shipping details are not shown.")
    if re.search(r"add (?:a )?payment method|enter (?:your )?card|add a card", lowered):
        raise ValueError("No saved payment method is selected.")
    endings = set(re.findall(r"ending in\s*(\d{4})", lowered))
    if len(endings) > 1:
        raise ValueError("More than one saved card is shown.")
    if not endings:
        named = set(re.findall(r"target circle\s+card|redcard|paypal|gift\s*card", lowered))
        if len(named) != 1:
            raise ValueError("Could not confirm exactly one saved payment method.")
    if not re.search(r"\btax(?:es)?\b", lowered):
        raise ValueError("Tax is not shown, so the full total is unknown.")
    matches = TOTAL_RE.findall(text)
    if not matches:
        raise ValueError("Could not read the order total.")
    total = Decimal(matches[-1].replace(",", "")).quantize(Decimal("0.01"))
    if total <= 0:
        raise ValueError("The order total is not a real price.")
    if max_total is None:
        raise ValueError(f"Set MAX_ORDER_TOTAL in .env. Checkout shows ${total}.")
    if total > max_total:
        raise ValueError(f"Order total ${total} is above MAX_ORDER_TOTAL ${max_total}.")
    return total


def extract_order_number(text: str) -> str | None:
    if not re.search(r"thanks|order (?:has been )?placed|order confirmed|your order", text, re.I):
        return None
    match = ORDER_RE.search(text)
    return None if match is None else match.group(1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def launch_persistent(playwright, config: PurchaseConfig):
    kwargs = {
        "user_data_dir": str(config.profile_dir),
        "headless": False,
        "viewport": {"width": 1366, "height": 900},
        "locale": "en-US",
        "timezone_id": "America/Chicago",
    }
    if config.browser_channel:
        kwargs["channel"] = config.browser_channel
    try:
        return await playwright.chromium.launch_persistent_context(**kwargs)
    except Exception as exc:
        raise PurchaseAbort(
            "Could not open Chrome. Install Google Chrome, close any window using this "
            "profile, or set BROWSER_CHANNEL= after installing Playwright Chromium."
        ) from exc


class Purchaser:
    def __init__(self, config: PurchaseConfig, database, notify):
        self.config = config
        self.db = database
        self._notify = notify
        self._submitted = False
        self._person_notified = False
        self._step = "start"
        self._shipping = False
        self._pickup = False
        self._playwright = None
        self._context = None
        self._page = None

    async def run(self, *, shipping: bool, pickup: bool) -> PurchaseResult:
        self._shipping = shipping
        self._pickup = pickup
        try:
            return await self._checkout()
        except PurchaseAbort as exc:
            await self._snap()
            if self._submitted:
                return PurchaseResult("uncertain", str(exc))
            if exc.attention:
                return PurchaseResult("needs_attention", str(exc))
            return PurchaseResult("failed", str(exc))
        except Exception:
            log.exception("Checkout error during %s", self._step)
            await self._snap()
            if self._submitted:
                return PurchaseResult(
                    "uncertain",
                    "Checkout failed after Place order was clicked. Check Target order history.",
                )
            return PurchaseResult(
                "failed",
                f"Checkout stopped during {self._step} before the order was placed.",
            )
        finally:
            await self._close()

    async def _checkout(self) -> PurchaseResult:
        if os.getenv("PURCHASE_QUANTITY", "1").strip() not in {"", "1"}:
            raise PurchaseAbort("Quantity must stay 1. No order was placed.")
        if not self.config.zip_code:
            raise PurchaseAbort("Set TARGET_ZIP before checkout.")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PurchaseAbort(
                "Playwright is not installed. Run the setup steps, then try again."
            ) from exc

        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        self._step = "open browser"
        self._playwright = await async_playwright().start()
        self._context = await self._launch()
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page.set_default_timeout(20_000)

        try:
            mode = choose_fulfillment(self._shipping, self._pickup)
        except ValueError as exc:
            raise PurchaseAbort(str(exc)) from exc
        await self._open(PRODUCT_URL)
        await self._require_title()
        await self._select_fulfillment(mode)
        await self._ensure_qty_one()
        await self._click_add()
        await self._open(CART_URL)
        await self._require_cart()
        await self._click_checkout()
        total = await self._require_checkout()
        if not submit_allowed(self.config.auto_purchase, self.config.dry_run):
            shown = f"${total}"
            return PurchaseResult(
                "dry_run",
                f"Dry run stopped before Place order. Total {shown}. No order was submitted.",
                total=shown,
            )
        total = await self._require_checkout()
        await self._click_place(total)
        order_number = await self._wait_for_order_number()
        shown = f"${total}"
        if not order_number:
            return PurchaseResult(
                "uncertain",
                "Place order was clicked, but no order number appeared. Check Target order history.",
                total=shown,
            )
        return PurchaseResult(
            "succeeded",
            f"Order {order_number} placed. Total {shown}.",
            order_number=order_number,
            total=shown,
        )

    async def _launch(self):
        return await launch_persistent(self._playwright, self.config)

    async def _open(self, url: str) -> None:
        self._step = f"open {url}"
        await self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await self._settle()
        if url.rstrip("/") not in self._page.url:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await self._settle()

    async def _settle(self) -> None:
        await self._page.wait_for_timeout(800)
        if not await self._needs_person():
            return
        if not self._person_notified:
            self._person_notified = True
            await self._notify(
                "Sign-in needed",
                "Target is asking you to sign in or finish a human check in the Chrome window. "
                "The bot will not solve that check or refresh past a queue.",
            )
        deadline = asyncio.get_running_loop().time() + 600
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(5)
            if not await self._needs_person():
                return
        raise PurchaseAbort(
            "Sign-in or the human check was not finished. No further checkout steps were taken.",
            attention=True,
        )

    async def _needs_person(self) -> bool:
        url = self._page.url.lower()
        if re.search(r"/(login|challenge)(\?|$)|account/signin", url):
            return True
        password = self._page.locator("input[type='password']")
        if await password.count() and await password.first.is_visible():
            return True
        challenge = self._page.locator(
            "#px-captcha, iframe[title*='px-captcha' i], iframe[title*='human' i]"
        )
        for index in range(await challenge.count()):
            if await challenge.nth(index).is_visible():
                return True
        text = (await self._page.locator("body").inner_text()).lower()
        return any(phrase in text for phrase in HUMAN_TEXT)

    async def _visible(self) -> str:
        return await self._page.locator("body").inner_text()

    async def _require_title(self) -> None:
        self._step = "check product title"
        heading = self._page.get_by_role("heading", level=1)
        if await heading.count() == 0:
            raise PurchaseAbort("The product page has no title. No order was placed.")
        title = await heading.first.inner_text()
        if not is_target_etb(title):
            raise PurchaseAbort(f"Product title was {title!r}. No order was placed.")

    async def _select_fulfillment(self, mode: str) -> None:
        self._step = f"choose {mode}"
        pattern = (
            re.compile(r"shipping|ship to|ship it", re.I)
            if mode == "shipping"
            else re.compile(r"pick\s?up|pick it up", re.I)
        )
        for role in ("radio", "button", "tab"):
            loc = self._page.get_by_role(role, name=pattern)
            for index in range(await loc.count()):
                choice = loc.nth(index)
                if not await choice.is_visible():
                    continue
                label = (await choice.inner_text()).lower()
                if any(word in label for word in ("return", "policy", "help", "question")):
                    continue
                if any(word in label for word in ("unavailable", "not available", "out of stock")):
                    raise PurchaseAbort(f"{mode.capitalize()} is shown as unavailable.")
                await choice.click()
                return
        raise PurchaseAbort(f"Could not find the {mode} option. No order was placed.")

    async def _ensure_qty_one(self) -> None:
        self._step = "set quantity"
        for role in ("combobox", "spinbutton"):
            loc = self._page.get_by_role(role, name=re.compile(r"^(qty|quantity)$", re.I))
            if await loc.count() == 0:
                continue
            value = (await loc.first.input_value()).strip()
            if value in {"", "1"}:
                return
            if role == "combobox":
                await loc.first.select_option("1")
            else:
                await loc.first.fill("1")
            value = (await loc.first.input_value()).strip()
            if value not in {"", "1"}:
                raise PurchaseAbort("Could not set the quantity to 1.")
            return

    async def _click_add(self) -> None:
        self._step = "add to cart"
        await self._click_named(
            re.compile(r"^(add to cart|pre-?order)( now)?$", re.I),
            allow_duplicate=True,
        )

    async def _require_cart(self) -> None:
        self._step = "check cart"
        try:
            assert_single_etb(await self._visible(), TARGET_ETB_TCIN)
        except ValueError as exc:
            raise PurchaseAbort(str(exc)) from exc

    async def _click_checkout(self) -> None:
        self._step = "open checkout"
        await self._click_named(re.compile(r"check out", re.I), allow_duplicate=False)
        try:
            await self._page.wait_for_url(re.compile(r"target\.com/checkout"), timeout=30_000)
        except Exception as exc:
            raise PurchaseAbort("Checkout did not open. No order was placed.") from exc
        await self._settle()

    async def _require_checkout(self) -> Decimal:
        self._step = "review order"
        try:
            return assert_checkout_safe(
                await self._visible(),
                TARGET_ETB_TCIN,
                self.config.max_total,
                self.config.zip_code,
            )
        except ValueError as exc:
            raise PurchaseAbort(str(exc)) from exc

    async def _click_place(self, total: Decimal) -> None:
        self._step = "place order"
        button = await self._only_button(re.compile(r"place (your )?order", re.I))
        self.db.record_purchase(
            sku=TARGET_ETB_TCIN,
            status="submitting",
            detail=f"Place order is about to be clicked. Total ${total}.",
            order_number=None,
            total=f"${total}",
            created_at=_now(),
        )
        self._submitted = True
        await button.click()

    async def _wait_for_order_number(self) -> str | None:
        self._step = "read confirmation"
        deadline = asyncio.get_running_loop().time() + 45
        while asyncio.get_running_loop().time() < deadline:
            if await self._needs_person():
                raise PurchaseAbort(
                    "A human check appeared after Place order. Check Target order history.",
                    attention=True,
                )
            number = extract_order_number(await self._visible())
            if number:
                return number
            await asyncio.sleep(1)
        return None

    async def _click_named(self, pattern: re.Pattern[str], *, allow_duplicate: bool) -> None:
        button = await self._buttons(pattern, allow_duplicate=allow_duplicate)
        await button.click()

    async def _only_button(self, pattern: re.Pattern[str]):
        return await self._buttons(pattern, allow_duplicate=False)

    async def _buttons(self, pattern: re.Pattern[str], *, allow_duplicate: bool):
        loc = self._page.get_by_role("button", name=pattern)
        enabled = []
        for index in range(await loc.count()):
            item = loc.nth(index)
            if not (await item.is_visible() and await item.is_enabled()):
                continue
            text = (await item.inner_text()).lower()
            if any(word in text for word in ("unavailable", "sold out", "out of stock")):
                continue
            enabled.append(item)
        if len(enabled) == 1 or (allow_duplicate and len(enabled) > 1):
            return enabled[0]
        raise PurchaseAbort(f"Could not find one enabled {pattern.pattern} button. No order was placed.")

    async def _snap(self) -> None:
        if self._page is None:
            return
        try:
            path = ROOT / "logs"
            path.mkdir(parents=True, exist_ok=True)
            await self._page.screenshot(path=str(path / "purchase-last.png"), full_page=True)
        except Exception:
            log.warning("Could not save a checkout screenshot")

    async def _close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:
            log.warning("Could not close the browser")
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            log.warning("Could not stop Playwright")
        self._context = None
        self._page = None
        self._playwright = None


def stock_flags(detail: str) -> tuple[bool, bool]:
    lowered = detail.lower()
    shipping = "shipping" in lowered or "preorder" in lowered
    pickup = "pickup" in lowered
    return shipping, pickup
