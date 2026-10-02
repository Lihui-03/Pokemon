import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from db import Database
from purchase import (
    AUTOMATIC_BLOCK,
    RETRY_BLOCK,
    PurchaseConfig,
    assert_checkout_safe,
    assert_single_etb,
    choose_fulfillment,
    extract_order_number,
    is_target_etb,
    recover_status,
    submit_allowed,
)

TCIN = "1010892076"
GOOD = """
Pokémon TCG: 30th Celebration Elite Trainer Box
A-1010892076
Qty 1
1 item
Shipping
123 Main Street
Milwaukee WI 53202
Visa ending in 4242
Subtotal $49.99
Delivery $0.00
Estimated taxes $4.40
Total $54.39
"""


class GuardTests(unittest.TestCase):
    def test_submit_requires_opt_in_and_live_mode(self):
        self.assertFalse(submit_allowed(False, True))
        self.assertFalse(submit_allowed(True, True))
        self.assertFalse(submit_allowed(False, False))
        self.assertTrue(submit_allowed(True, False))

    def test_yes_does_not_enable_auto_purchase(self):
        old = os.environ.get("AUTO_PURCHASE")
        os.environ["AUTO_PURCHASE"] = "yes"
        try:
            self.assertFalse(PurchaseConfig.from_env().auto_purchase)
        finally:
            _restore("AUTO_PURCHASE", old)

    def test_dry_run_defaults_on(self):
        old = os.environ.get("PURCHASE_DRY_RUN")
        os.environ.pop("PURCHASE_DRY_RUN", None)
        try:
            self.assertTrue(PurchaseConfig.from_env().dry_run)
        finally:
            _restore("PURCHASE_DRY_RUN", old)

    def test_title_must_be_the_single_box(self):
        self.assertTrue(is_target_etb("Pokémon TCG: 30th Celebration Elite Trainer Box"))
        self.assertFalse(is_target_etb("Pokémon TCG: 30th Celebration Poster Collection"))
        self.assertFalse(is_target_etb("30th Celebration Elite Trainer Box Case"))

    def test_shipping_is_preferred(self):
        self.assertEqual(choose_fulfillment(True, True), "shipping")
        self.assertEqual(choose_fulfillment(False, True), "pickup")
        with self.assertRaises(ValueError):
            choose_fulfillment(False, False)

    def test_checkout_accepts_a_single_saved_order(self):
        total = assert_checkout_safe(GOOD, TCIN, Decimal("70.00"), "53202")
        self.assertEqual(total, Decimal("54.39"))

    def test_subtotal_is_not_the_order_total(self):
        text = GOOD.replace("Total $54.39", "Amount due later")
        with self.assertRaisesRegex(ValueError, "order total"):
            assert_checkout_safe(text, TCIN, Decimal("70.00"), "53202")

    def test_missing_tax_stops(self):
        text = GOOD.replace("Estimated taxes $4.40", "Fees $4.40")
        with self.assertRaisesRegex(ValueError, "Tax"):
            assert_checkout_safe(text, TCIN, Decimal("70.00"), "53202")

    def test_price_cap_stops_before_an_order(self):
        with self.assertRaisesRegex(ValueError, "MAX_ORDER_TOTAL"):
            assert_checkout_safe(GOOD, TCIN, Decimal("50.00"), "53202")

    def test_missing_cap_reports_the_total(self):
        with self.assertRaisesRegex(ValueError, r"\$54\.39"):
            assert_checkout_safe(GOOD, TCIN, None, "53202")

    def test_wrong_quantity_stops(self):
        text = GOOD.replace("Qty 1", "Qty 2")
        with self.assertRaisesRegex(ValueError, "quantity"):
            assert_single_etb(text, TCIN)

    def test_extra_item_stops(self):
        text = GOOD.replace("1 item", "2 items")
        with self.assertRaisesRegex(ValueError, "more than one"):
            assert_single_etb(text, TCIN)

    def test_wrong_product_stops(self):
        text = GOOD.replace(TCIN, "1010892067").replace("Elite Trainer Box", "Poster Collection")
        with self.assertRaises(ValueError):
            assert_single_etb(text, TCIN)

    def test_wrong_zip_stops(self):
        with self.assertRaisesRegex(ValueError, "53202"):
            assert_checkout_safe(GOOD.replace("53202", "10001"), TCIN, Decimal("70.00"), "53202")

    def test_one_card_can_share_the_page_with_another_option(self):
        text = GOOD.replace("Visa ending in 4242", "Visa ending in 4242\nPayPal")
        total = assert_checkout_safe(text, TCIN, Decimal("70.00"), "53202")
        self.assertEqual(total, Decimal("54.39"))

    def test_two_cards_stop(self):
        text = GOOD.replace("Visa ending in 4242", "Visa ending in 4242\nMastercard ending in 1111")
        with self.assertRaisesRegex(ValueError, "More than one saved card"):
            assert_checkout_safe(text, TCIN, Decimal("70.00"), "53202")

    def test_order_number_requires_confirmation_language(self):
        self.assertIsNone(extract_order_number("Order number: 1020024412345678"))
        self.assertEqual(
            extract_order_number("Thanks for your order. Order number: 1020024412345678"),
            "1020024412345678",
        )

    def test_locks(self):
        self.assertIn("succeeded", AUTOMATIC_BLOCK)
        self.assertIn("uncertain", AUTOMATIC_BLOCK)
        self.assertIn("needs_attention", AUTOMATIC_BLOCK)
        self.assertNotIn("failed", AUTOMATIC_BLOCK)
        self.assertNotIn("dry_run", RETRY_BLOCK)
        self.assertIn("uncertain", RETRY_BLOCK)
        self.assertEqual(recover_status("submitting")[0], "uncertain")
        self.assertEqual(recover_status("in_progress")[0], "failed")
        self.assertIsNone(recover_status("idle"))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "watches.db")

    def tearDown(self):
        self.db._conn.close()
        self.tmp.cleanup()

    def test_later_update_keeps_the_order_number(self):
        self.db.record_purchase(
            sku=TCIN,
            status="succeeded",
            detail="placed",
            order_number="1020024412345678",
            total="$54.39",
            created_at="2026-10-01T00:00:00+00:00",
        )
        self.db.record_purchase(
            sku=TCIN,
            status="idle",
            detail="cleared",
            order_number=None,
            total=None,
            created_at="2026-10-01T00:01:00+00:00",
        )
        self.assertEqual(self.db.get_setting("purchase_order_number"), "1020024412345678")
        self.assertEqual(self.db.latest_purchase()["status"], "idle")


def _restore(name: str, old: str | None) -> None:
    if old is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = old


if __name__ == "__main__":
    unittest.main()
