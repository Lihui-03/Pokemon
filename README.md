# Pokémon 30th Celebration Restock Bot

Discord restock alerts for Pokémon TCG: 30th Celebration products, with an optional Target checkout for one Elite Trainer Box.

Product checked for auto-buy:

- **Name:** Pokémon TCG: 30th Celebration Elite Trainer Box
- **Target ID:** `1010892076`
- **Quantity:** 1

By default the bot only watches stock and posts Discord alerts. It does not place an order until you turn that on.

## What it does

1. Checks Target (and other watched stores) on a timer.
2. Posts a Discord alert when a watched product goes from out of stock to in stock.
3. For the Target Elite Trainer Box only, if checkout is enabled:
   - opens the product page in Chrome
   - adds 1 box to the cart
   - goes to checkout with your saved Target account, address, and payment method
   - in dry run, stops before Place order
   - in live mode, clicks Place order and saves the order number

Shipping is used when shipping is available. Pickup is used only when shipping is not. The saved address or pickup store must show your `TARGET_ZIP`.

## Requirements

- Windows
- Python 3.11+ (`py -3` on PATH)
- Google Chrome
- A Discord bot token
- A Target account with a saved shipping address and saved payment method

## First-time setup

### 1. Open the project folder in PowerShell

```powershell
cd "C:\Users\meil\OneDrive - Milwaukee School of Engineering\Desktop\Pokemon"
```

### 2. Create the virtual environment and install packages

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

### 3. Create your `.env` file

```powershell
Copy-Item .env.example .env
```

Edit `.env` and fill in at least:

| Setting | What to put |
| --- | --- |
| `DISCORD_TOKEN` | Bot token from [Discord Developer Portal](https://discord.com/developers/applications) |
| `DISCORD_GUILD_ID` | Your Discord server ID (Developer Mode → right-click server → Copy Server ID) |
| `TARGET_ZIP` | ZIP on your saved Target address / pickup area (default `53202`) |
| `TARGET_STORE_ID` | Target store for pickup checks (default `223` Milwaukee Chase) |
| `AUTO_PURCHASE` | Leave `false` until you are ready to test checkout |
| `PURCHASE_DRY_RUN` | Leave `true` until a dry run succeeds |
| `MAX_ORDER_TOTAL` | Highest total you will accept, including tax and shipping (example `70.00`) |

Optional:

| Setting | What it does |
| --- | --- |
| `ALERT_ROLE_ID` | Discord role mentioned on restock alerts |
| `BESTBUY_API_KEY` | Free key from [Best Buy developer site](https://developer.bestbuy.com) so Best Buy is checked |
| `POLL_SECONDS` | Seconds between stock checks (default `120`; values under 60 are raised to 60) |
| `PLAYWRIGHT_PROFILE_DIR` | Chrome profile folder (default `data/target-profile`) |
| `BROWSER_CHANNEL` | Browser channel for Playwright (default `chrome`) |

Never put your Target password or card number in `.env` or in any project file. Login happens in Chrome.

### 4. Invite the Discord bot

In the Discord Developer Portal, create a bot application, copy the token into `.env`, and invite the bot to your server with permission to send messages and embeds in the alert channel.

### 5. Sign in to Target once

Stop any running copy of the bot first. Chrome can only use the profile from one process.

```powershell
.\.venv\Scripts\python.exe login.py
```

1. Sign in to Target in the Chrome window that opens.
2. Finish any human check yourself. The bot will not solve it.
3. Confirm you are on your account page.
4. Press Enter in PowerShell.

That saves the session under `data\target-profile`.

### 6. Start the bot

```powershell
.\start-bot.ps1
```

In Discord, run:

```text
/alerts
```

in the channel that should receive restock and purchase messages.

Useful commands after that:

| Command | Purpose |
| --- | --- |
| `/status` | Last stock check for each watched product |
| `/scan` | Check every watched product now |
| `/watch list` | Show watched products |
| `/watch add` | Add a product URL or `target 1010892076` |
| `/watch remove` | Stop watching by watch id |
| `/watch pause` / `/watch resume` | Pause or resume one product |
| `/purchase status` | Checkout mode and last purchase result |
| `/purchase retry` | Start checkout if the last check showed the ETB in stock |
| `/purchase clear-uncertain` | Allow another attempt only after you confirm Target has no order |

Restart the bot after any `.env` change.

## Safe test before buying

Keep money mode off until this dry run works.

1. In `.env` set:

```env
AUTO_PURCHASE=true
PURCHASE_DRY_RUN=true
MAX_ORDER_TOTAL=70.00
```

Use a real max total you are willing to pay. Adjust the number.

2. Restart:

```powershell
.\start-bot.ps1
```

3. Start checkout:

- Wait for a restock transition (out of stock → in stock), or
- If the ETB is already in stock, run `/purchase retry`

4. Watch Chrome and Discord.

Expected dry-run result:

- Discord says **Checkout started**
- Chrome opens the product page, adds 1 box, reaches checkout
- Discord says **Dry run finished** and shows the total
- Place order is **not** clicked

If Discord says **Purchase stopped** or **Sign-in needed**, fix that first. Common reasons:

- not signed in / human check waiting in Chrome
- wrong product title
- quantity is not 1
- cart has more than one item
- ZIP on the page does not match `TARGET_ZIP`
- no saved address or payment method selected
- tax or full order total missing
- total above `MAX_ORDER_TOTAL`

A screenshot of the last failed page is saved at `logs\purchase-last.png`. That file stays on your PC and is not posted to Discord.

5. Confirm `/purchase status` shows the dry-run result and the total looked right.

## Turn on a real purchase

Only after a dry run reaches the order-review page with an acceptable total.

1. In `.env` set:

```env
AUTO_PURCHASE=true
PURCHASE_DRY_RUN=false
MAX_ORDER_TOTAL=70.00
```

2. Restart with a visible window:

```powershell
.\start-bot.ps1
```

Do not use the hidden scheduled-task installer while auto-purchase is on. Chrome must be able to open on screen.

3. Run `/purchase retry` if the box is already in stock, or wait for the next restock alert.

4. Discord should report **Order placed** and include the order number. The number is also stored in `data\watches.db`.

## Purchase safeguards

- Buys at most **one** Elite Trainer Box.
- Real checkout requires both `AUTO_PURCHASE=true` and `PURCHASE_DRY_RUN=false`.
- Stops if the product title, quantity, ZIP, shipping details, payment method, tax, or total look wrong.
- After a successful order, it will not buy again.
- If Place order may already have been clicked and confirmation is unclear, status becomes `uncertain` and it will not buy again.
- `/purchase clear-uncertain` is only for that uncertain case, and only after you check Target order history yourself.
- The bot does not solve CAPTCHAs, skip queues, or refresh past a block. It pauses and notifies Discord instead.
- Passwords and card numbers are not stored in source code. The Chrome profile holds your Target login session.

## Background alerts only

If checkout is off (`AUTO_PURCHASE=false`), you can register a logon task for Discord alerts:

```powershell
.\install-background.ps1
```

That script refuses to register while `AUTO_PURCHASE=true`. For checkout, use `.\start-bot.ps1` in a normal PowerShell window.

## Logs and data

| Path | Contents |
| --- | --- |
| `logs\bot.log` | Bot and checkout log |
| `logs\purchase-last.png` | Screenshot from the last failed or interrupted checkout |
| `data\watches.db` | Watched products, settings, purchase attempts |
| `data\target-profile\` | Chrome profile with your Target login |

Keep `.env` and `data\` private. Do not commit them.

## Quick checklist

1. Install packages and Playwright Chromium.
2. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
3. Run `login.py` and sign in to Target.
4. Start with `.\start-bot.ps1` and run `/alerts`.
5. Test with `AUTO_PURCHASE=true` and `PURCHASE_DRY_RUN=true`.
6. Only then set `PURCHASE_DRY_RUN=false` for one real purchase.
