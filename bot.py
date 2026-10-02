"""Discord alerts and optional Target checkout for one 30th Celebration box.

Setup is in .env.example. Stock checks stay on a timer. Checkout uses a visible
Chrome window and the saved login in data/target-profile. It does not solve
CAPTCHAs or skip queues. A real order is placed only when AUTO_PURCHASE=true
and PURCHASE_DRY_RUN=false.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
import httpx
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from catalog import DISCOVERY_QUERIES
from db import DATA, Database
from purchase import (
    AUTOMATIC_BLOCK,
    RETRY_BLOCK,
    TARGET_ETB_TCIN,
    PurchaseConfig,
    PurchaseResult,
    Purchaser,
    recover_status,
    stock_flags,
)
from retailers import USER_AGENT, Checker, RetailerError, Stock, parse_watch, product_url

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("restock")

POLL_SECONDS = max(60, int(os.getenv("POLL_SECONDS", "120")))
DISCOVERY_SECONDS = int(os.getenv("DISCOVERY_SECONDS", "900"))
GAP_SECONDS = float(os.getenv("REQUEST_GAP_SECONDS", "2"))
COOLDOWN_SECONDS = int(os.getenv("BLOCK_COOLDOWN_SECONDS", "900"))

db = Database()


def _acquire_lock() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    lock_path = DATA / "bot.lock"
    if lock_path.exists():
        raw = lock_path.read_text(encoding="utf-8").strip()
        if raw.isdigit():
            try:
                os.kill(int(raw), 0)
            except OSError:
                pass
            else:
                log.error("Bot already running with pid %s", raw)
                raise SystemExit(1)
    lock_path.write_text(str(os.getpid()), encoding="utf-8")


def _release_lock() -> None:
    lock_path = DATA / "bot.lock"
    try:
        if lock_path.exists() and lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock_path.unlink()
    except OSError:
        pass


def _owner_id() -> str | None:
    return db.get_setting("owner_id")


async def _require_owner(interaction: discord.Interaction) -> bool:
    owner = _owner_id()
    if owner is None:
        await interaction.response.send_message(
            "Run `/alerts` in the channel that should receive restock posts first.",
            ephemeral=True,
        )
        return False
    if str(interaction.user.id) != owner:
        await interaction.response.send_message(
            "Only the person who set up alerts can change the watch list.",
            ephemeral=True,
        )
        return False
    return True


class Watch(app_commands.Group):
    def __init__(self):
        super().__init__(name="watch", description="Manage 30th Celebration restock watches")

    @app_commands.command(name="add", description="Watch a product URL or retailer id")
    @app_commands.describe(item="Product URL, or 'target 1010892076' / 'bestbuy 6685559'")
    async def add(self, interaction: discord.Interaction, item: str):
        if not await _require_owner(interaction):
            return
        try:
            retailer, sku = parse_watch(item)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        added = db.add_product(retailer, sku, f"{retailer} {sku}", product_url(retailer, sku))
        if not added:
            await interaction.response.send_message(
                f"Already watching {retailer} `{sku}`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Watching {retailer} `{sku}`. The next scan will fill in the name and stock.",
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="Stop watching a product by its id")
    async def remove(self, interaction: discord.Interaction, product_id: int):
        if not await _require_owner(interaction):
            return
        if db.delete_product(product_id):
            await interaction.response.send_message(f"Removed #{product_id}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"No watch #{product_id}.", ephemeral=True)

    @app_commands.command(name="pause", description="Pause alerts for one product")
    async def pause(self, interaction: discord.Interaction, product_id: int):
        if not await _require_owner(interaction):
            return
        if db.set_enabled(product_id, False):
            await interaction.response.send_message(f"Paused #{product_id}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"No watch #{product_id}.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume alerts for one product")
    async def resume(self, interaction: discord.Interaction, product_id: int):
        if not await _require_owner(interaction):
            return
        if db.set_enabled(product_id, True):
            await interaction.response.send_message(f"Resumed #{product_id}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"No watch #{product_id}.", ephemeral=True)

    @app_commands.command(name="list", description="Show every watched product")
    async def list_watches(self, interaction: discord.Interaction):
        if not await _require_owner(interaction):
            return
        rows = db.products()
        if not rows:
            await interaction.response.send_message("Nothing is being watched yet.", ephemeral=True)
            return
        lines = [_format_row(row) for row in rows]
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n…"
        await interaction.response.send_message(text, ephemeral=True)


def _format_row(row) -> str:
    state = row["last_status"] or "not checked"
    flag = "" if row["enabled"] else " (paused)"
    price = f" {row['last_price']}" if row["last_price"] else ""
    return f"#{row['id']} {row['retailer']}{flag} — {state}{price} — {row['name']}"


class Purchase(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="purchase",
            description="Checkout for the Target 30th Celebration Elite Trainer Box",
        )

    @app_commands.command(name="status", description="Show checkout settings and the last result")
    async def status(self, interaction: discord.Interaction):
        if not await _require_owner(interaction):
            return
        await interaction.response.send_message(_purchase_status_text(), ephemeral=True)

    @app_commands.command(name="retry", description="Try checkout again if the last check was in stock")
    async def retry(self, interaction: discord.Interaction):
        if not await _require_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = _etb_row()
        if row is None or row["last_status"] != "in_stock":
            await interaction.followup.send(
                "The last check did not show the Elite Trainer Box in stock.",
                ephemeral=True,
            )
            return
        shipping, pickup = stock_flags(row["last_detail"] or "")
        stock = Stock(
            status="in_stock",
            detail=row["last_detail"] or "",
            name=row["name"],
            price=row["last_price"],
            url=row["url"],
            shipping=shipping,
            pickup=pickup,
        )
        message = await _maybe_purchase(row, stock, automatic=False)
        await interaction.followup.send(message, ephemeral=True)

    @app_commands.command(
        name="clear-uncertain",
        description="Allow another attempt after you confirm Target has no order",
    )
    async def clear_uncertain(self, interaction: discord.Interaction):
        if not await _require_owner(interaction):
            return
        status = db.get_setting("purchase_status") or "idle"
        if status != "uncertain":
            await interaction.response.send_message(
                f"Purchase status is {status}. Nothing was cleared.",
                ephemeral=True,
            )
            return
        db.record_purchase(
            sku=TARGET_ETB_TCIN,
            status="idle",
            detail="Cleared after the owner checked Target order history.",
            order_number=None,
            total=None,
            created_at=_now(),
        )
        await interaction.response.send_message(
            "Purchase status is idle. Checkout can run on the next restock, or use /purchase retry.",
            ephemeral=True,
        )


class RestockBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.cooldown_until: dict[str, float] = {}
        self.checkout_lock = asyncio.Lock()
        self._purchase_recovered = False

    async def setup_hook(self) -> None:
        self.tree.add_command(Watch())
        self.tree.add_command(Purchase())
        guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
        if guild_raw:
            guild = discord.Object(id=int(guild_raw))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %s commands to guild %s", len(synced), guild_raw)
        else:
            synced = await self.tree.sync()
            log.info("Synced %s global commands. Guild sync is faster; set DISCORD_GUILD_ID.", len(synced))
        self.loop.create_task(self._monitor())

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)
        await _recover_purchase()
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="30th Celebration restocks",
            )
        )


bot = RestockBot()


@bot.tree.command(name="alerts", description="Post restock alerts in this channel")
async def alerts(interaction: discord.Interaction):
    owner = _owner_id()
    if owner and str(interaction.user.id) != owner:
        await interaction.response.send_message(
            "Only the person who set up alerts can move the channel.",
            ephemeral=True,
        )
        return
    if interaction.channel_id is None:
        await interaction.response.send_message("Use this inside a server channel.", ephemeral=True)
        return
    db.set_setting("alert_channel_id", str(interaction.channel_id))
    db.set_setting("owner_id", str(interaction.user.id))
    await interaction.response.send_message(
        "Restock alerts will be posted in this channel. "
        "Target is checked on a timer. Best Buy checks start after you add BESTBUY_API_KEY. "
        "Walmart, GameStop, Amazon, and Pokémon Center are checked with a normal page request "
        "and often refuse scripts; this bot will not try to get around that. "
        "Target checkout stays off unless AUTO_PURCHASE=true. "
        "A real order also needs PURCHASE_DRY_RUN=false.",
        ephemeral=True,
    )


@bot.tree.command(name="scan", description="Check every watched product now")
async def scan(interaction: discord.Interaction):
    if not await _require_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    checked, alerts_sent = await _cycle(discover=False)
    await interaction.followup.send(
        f"Checked {checked} products. Sent {alerts_sent} restock alert(s).",
        ephemeral=True,
    )


@bot.tree.command(name="status", description="Show the last check for each product")
async def status(interaction: discord.Interaction):
    if not await _require_owner(interaction):
        return
    rows = db.products(enabled_only=True)
    if not rows:
        await interaction.response.send_message("No active watches.", ephemeral=True)
        return
    in_stock = sum(1 for row in rows if row["last_status"] == "in_stock")
    lines = [f"Watching {len(rows)}. In stock: {in_stock}.", ""]
    lines.extend(_format_row(row) for row in rows[:30])
    lines.append(_purchase_status_text().split("\n", 1)[0])
    paused = [row for row in bot.cooldown_until.items() if row[1] > time.monotonic()]
    if paused:
        names = ", ".join(name for name, _until in paused)
        lines.append("")
        lines.append(f"Paused after a block: {names}")
    text = "\n".join(lines)
    await interaction.response.send_message(text[:1900], ephemeral=True)


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Command failed", exc_info=error)
    message = "That command failed. The details are in logs/bot.log."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html"},
        follow_redirects=True,
    )


def _etb_row():
    for row in db.products():
        if row["retailer"] == "target" and row["sku"] == TARGET_ETB_TCIN:
            return row
    return None


def _purchase_transition(row, stock, previous: str | None) -> bool:
    return (
        row["retailer"] == "target"
        and row["sku"] == TARGET_ETB_TCIN
        and stock.status == "in_stock"
        and previous != "in_stock"
    )


def _purchase_status_text() -> str:
    try:
        config = PurchaseConfig.from_env()
        auto = "on" if config.auto_purchase else "off"
        dry = "on" if config.dry_run else "off"
        maximum = "not set" if config.max_total is None else f"${config.max_total}"
    except ValueError as exc:
        auto, dry, maximum = "invalid", "invalid", str(exc)
    status = db.get_setting("purchase_status") or "idle"
    detail = db.get_setting("purchase_detail") or "No checkout attempt yet."
    order_number = db.get_setting("purchase_order_number")
    line = f"Purchase: {status}. Auto: {auto}. Dry run: {dry}. Max total: {maximum}."
    if order_number:
        line += f" Order {order_number}."
    return f"{line}\n{detail}"


async def _notify_purchase(title: str, description: str, *, color: int) -> None:
    log.info("%s — %s", title, description)
    channel_id = db.get_setting("alert_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except discord.HTTPException:
            log.exception("Could not fetch alert channel %s", channel_id)
            return
    embed = discord.Embed(
        title=title,
        description=description[:4000],
        url=product_url("target", TARGET_ETB_TCIN),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    try:
        await channel.send(content="30th Celebration Elite Trainer Box", embed=embed)
    except discord.HTTPException:
        log.exception("Failed to send purchase update")


async def _purchase_notify(title: str, description: str) -> None:
    color = 0xE67E22 if title == "Sign-in needed" else 0x3498DB
    await _notify_purchase(title, description, color=color)


async def _recover_purchase() -> None:
    if bot._purchase_recovered:
        return
    bot._purchase_recovered = True
    found = recover_status(db.get_setting("purchase_status"))
    if found is None:
        return
    status, detail = found
    db.record_purchase(
        sku=TARGET_ETB_TCIN,
        status=status,
        detail=detail,
        order_number=None,
        total=None,
        created_at=_now(),
    )
    title = "Order status uncertain" if status == "uncertain" else "Purchase stopped"
    await _notify_purchase(title, detail, color=0xE67E22)


async def _maybe_purchase(row, stock, *, automatic: bool) -> str:
    try:
        config = PurchaseConfig.from_env()
    except ValueError as exc:
        await _notify_purchase("Purchase stopped", str(exc), color=0xE74C3C)
        return str(exc)
    if not config.auto_purchase:
        message = "AUTO_PURCHASE is not true, so checkout did not start."
        if automatic:
            log.info("Elite Trainer Box is in stock. %s", message)
        else:
            await _notify_purchase("Purchase not started", message, color=0xE67E22)
        return message
    async with bot.checkout_lock:
        status = db.get_setting("purchase_status") or "idle"
        blocked = status in (AUTOMATIC_BLOCK if automatic else RETRY_BLOCK)
        if blocked:
            if status == "succeeded":
                number = db.get_setting("purchase_order_number") or "saved"
                message = f"Checkout did not start because order {number} already succeeded."
            else:
                message = f"Checkout did not start because the purchase status is {status}."
            await _notify_purchase("Purchase not started", message, color=0xE67E22)
            return message
        db.record_purchase(
            sku=TARGET_ETB_TCIN,
            status="in_progress",
            detail="Checkout started for one Elite Trainer Box.",
            order_number=None,
            total=None,
            created_at=_now(),
        )
        await _notify_purchase(
            "Checkout started",
            "Opening the Target product page for one Elite Trainer Box.",
            color=0x3498DB,
        )
        result = await Purchaser(config, db, _purchase_notify).run(
            shipping=stock.shipping,
            pickup=stock.pickup,
        )
        if db.get_setting("purchase_status") == "submitting" and result.status != "succeeded":
            result = PurchaseResult(
                "uncertain",
                result.detail,
                order_number=result.order_number,
                total=result.total,
            )
        db.record_purchase(
            sku=TARGET_ETB_TCIN,
            status=result.status,
            detail=result.detail,
            order_number=result.order_number,
            total=result.total,
            created_at=_now(),
        )
        titles = {
            "succeeded": "Order placed",
            "dry_run": "Dry run finished",
            "failed": "Purchase stopped",
            "needs_attention": "Sign-in needed",
            "uncertain": "Order status uncertain",
        }
        colors = {
            "succeeded": 0x2ECC71,
            "dry_run": 0xF1C40F,
            "failed": 0xE74C3C,
            "needs_attention": 0xE67E22,
            "uncertain": 0xE67E22,
        }
        await _notify_purchase(
            titles.get(result.status, "Purchase update"),
            result.detail,
            color=colors.get(result.status, 0x3498DB),
        )
        return result.detail


async def _monitor() -> None:
    await bot.wait_until_ready()
    next_discovery = 0.0
    while not bot.is_closed():
        started = time.monotonic()
        try:
            discover = started >= next_discovery
            await _cycle(discover=discover)
            if discover:
                next_discovery = time.monotonic() + DISCOVERY_SECONDS
        except Exception:
            log.exception("Monitor cycle failed")
        wait = max(5.0, POLL_SECONDS - (time.monotonic() - started))
        await asyncio.sleep(wait)


async def _cycle(discover: bool) -> tuple[int, int]:
    checked = 0
    sent = 0
    async with _client() as client:
        checker = Checker(client)
        for row in db.products(enabled_only=True):
            if _cooling(row["retailer"]):
                continue
            if row["retailer"] == "bestbuy" and not os.getenv("BESTBUY_API_KEY", "").strip():
                if "BESTBUY_API_KEY" not in (row["last_detail"] or ""):
                    db.save_check(
                        row["id"],
                        status=None,
                        detail="Best Buy needs a free developer API key. Add BESTBUY_API_KEY to .env.",
                        price=None,
                        name=None,
                        checked_at=_now(),
                    )
                continue
            needs_details = not row["last_price"] or row["name"] == f"{row['retailer']} {row['sku']}"
            try:
                stock = await checker.check(
                    row["retailer"], row["sku"], with_details=needs_details
                )
            except RetailerError as exc:
                log.warning("%s %s: %s", row["retailer"], row["sku"], exc)
                if exc.cooldown:
                    bot.cooldown_until[row["retailer"]] = time.monotonic() + COOLDOWN_SECONDS
                db.save_check(
                    row["id"],
                    status=None,
                    detail=str(exc),
                    price=None,
                    name=None,
                    checked_at=_now(),
                )
            else:
                checked += 1
                if checker.detail_blocked:
                    bot.cooldown_until[row["retailer"]] = time.monotonic() + COOLDOWN_SECONDS
                    checker.detail_blocked = False
                previous = row["last_status"]
                if await _record(row, stock):
                    sent += 1
                if _purchase_transition(row, stock, previous):
                    await _maybe_purchase(row, stock, automatic=True)
            await asyncio.sleep(GAP_SECONDS)
        if discover:
            await _discover(checker)
    log.info("Cycle checked %s products, sent %s alerts", checked, sent)
    return checked, sent


def _cooling(retailer: str) -> bool:
    return bot.cooldown_until.get(retailer, 0) > time.monotonic()


async def _record(row, stock) -> bool:
    previous = row["last_status"]
    should_alert = stock.status == "in_stock" and previous != "in_stock"
    detail = stock.detail
    store_status = stock.status if stock.status in {"in_stock", "out_of_stock"} else None
    sent = False
    if should_alert:
        sent = await _alert(row, stock)
        if not sent:
            store_status = None
            detail = stock.detail + " Alert was not posted. Run /alerts in a channel, then it will retry."
    db.save_check(
        row["id"],
        status=store_status,
        detail=detail,
        price=stock.price,
        name=stock.name,
        checked_at=_now(),
    )
    return sent


async def _alert(row, stock) -> bool:
    channel_id = db.get_setting("alert_channel_id")
    if not channel_id:
        log.warning("In stock, but no alert channel is set. Run /alerts.")
        return False
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except discord.HTTPException:
            log.exception("Could not fetch alert channel %s", channel_id)
            return False
    name = stock.name or row["name"]
    url = stock.url or row["url"]
    price = stock.price or row["last_price"] or "unknown"
    embed = discord.Embed(
        title=name,
        url=url,
        description=stock.detail,
        color=0x2ECC71,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Store", value=row["retailer"], inline=True)
    embed.add_field(name="Price", value=str(price), inline=True)
    embed.add_field(name="Id", value=str(row["sku"]), inline=True)
    role_id = os.getenv("ALERT_ROLE_ID", "").strip()
    content = f"<@&{role_id}> 30th Celebration restock" if role_id else "30th Celebration restock"
    try:
        await channel.send(content=content, embed=embed)
    except discord.HTTPException:
        log.exception("Failed to send alert for %s", row["sku"])
        return False
    return True


async def _discover(checker: Checker) -> None:
    raw_index = db.get_setting("discovery_index") or "0"
    index = int(raw_index) % len(DISCOVERY_QUERIES)
    query = DISCOVERY_QUERIES[index]
    db.set_setting("discovery_index", str((index + 1) % len(DISCOVERY_QUERIES)))
    found = []
    try:
        if not _cooling("target"):
            found.extend(await checker.search_target(query))
    except RetailerError as exc:
        log.warning("Target discovery: %s", exc)
        if exc.cooldown:
            bot.cooldown_until["target"] = time.monotonic() + COOLDOWN_SECONDS
    await asyncio.sleep(GAP_SECONDS)
    try:
        if not _cooling("bestbuy"):
            found.extend(await checker.search_bestbuy(query))
    except RetailerError as exc:
        log.warning("Best Buy discovery: %s", exc)
        if exc.cooldown:
            bot.cooldown_until["bestbuy"] = time.monotonic() + COOLDOWN_SECONDS
    added = 0
    for item in found:
        if db.add_product(item.retailer, item.sku, item.name, item.url):
            added += 1
            log.info("Discovered %s %s %s", item.retailer, item.sku, item.name)
    if added:
        log.info("Discovery query %r added %s products", query, added)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        log.error("Set DISCORD_TOKEN in .env. See .env.example.")
        raise SystemExit(1)
    _acquire_lock()
    added = db.seed()
    log.info("Seeded %s new products", added)
    try:
        bot.run(token, log_handler=None)
    finally:
        _release_lock()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _release_lock()
        sys.exit(0)
