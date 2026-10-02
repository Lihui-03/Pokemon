"""Open the saved Chrome profile so you can sign in to Target yourself.

The bot reuses this profile later. It does not read or store your password.
Close the bot before running this, because Chrome locks the profile.
"""

from __future__ import annotations

import asyncio
import sys

from purchase import PurchaseAbort, PurchaseConfig, launch_persistent


async def main() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Install dependencies first:")
        print(r"  .venv\Scripts\python.exe -m pip install -r requirements.txt")
        print(r"  .venv\Scripts\python.exe -m playwright install chromium")
        raise SystemExit(1)

    config = PurchaseConfig.from_env()
    config.profile_dir.mkdir(parents=True, exist_ok=True)
    print(f"Profile: {config.profile_dir}")
    print("Sign in to the Target account that has your saved address and payment method.")
    print("Finish any human check yourself. This window will not solve it.")
    print("When your account page is open, come back here and press Enter.")

    try:
        async with async_playwright() as playwright:
            context = await launch_persistent(playwright, config)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.target.com/account", wait_until="domcontentloaded")
            await asyncio.get_running_loop().run_in_executor(None, input, "")
            await context.close()
    except PurchaseAbort as exc:
        print(exc)
        raise SystemExit(1)
    print("Saved the Target session in the profile directory.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
