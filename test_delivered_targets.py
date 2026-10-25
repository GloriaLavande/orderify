import unittest

import main as backend


CONFIG = {
    "market": "FR",
    "currency": "EUR",
    "quantity": 1,
    "allow_price_decreases": False,
    "delivered_targets_eur": {
        "No strap": 20.18,
        "Clear Back No Strap": 26.00,
        "Built-in Hand Strap": 28.30,
        "ClearBack Hand Strap": 28.60,
        "Rotate&Detach sans sangle - standard": 30.30,
        "Rotate&Detach sans sangle - bas": 27.35,
    },
    "no_strap_discounted_item_cap_eur": 14.33,
    "no_strap_maximum_discounted_increase_from_saved_baseline_eur": 1.00,
    "rotate_detach_low_model_patterns": ["LOWMODEL"],
    "review_model_keywords": ["Scribe", "Oasis", "Kobo", "Fire"],
}


def inventory(style, cents=2500, model="SA568B"):
    return {"products": [{
        "sku": "CASE-1",
        "property_values": [
            {"property_id": 1, "property_name": "Device", "values": [model]},
            {"property_id": 2, "property_name": "Style", "values": [style]},
        ],
        "offerings": [{
            "quantity": 3,
            "is_enabled": True,
            "readiness_state_id": 7,
            "price": {"amount": cents, "divisor": 100, "currency_code": "EUR"},
        }],
    }], "price_on_property": [1, 2], "quantity_on_property": [], "sku_on_property": [1, 2]}


def context(style="No strap", shipping="6.00", baseline="13.33", error=None):
    product = inventory(style)["products"][0]
    key = backend._stable_baseline_key(product, 0)
    return {
        "config": backend._parse_delivered_targets(CONFIG),
        "shipping_cost": None if shipping is None else backend.Decimal(shipping),
        "shipping_source": "profil Etsy FR primary_cost" if shipping is not None else None,
        "shipping_error": error,
        "baselines": {key: backend.Decimal(baseline)},
        "promotion_source": "saisie manuelle vérifiée",
    }


class DeliveredTargetTests(unittest.TestCase):
    def calculate(self, style, cents=2500, shipping="6.00", baseline="13.33", model="SA568B"):
        return backend._clean_inventory_payload(
            inventory(style, cents, model), backend.Decimal("0"), "full",
            "delivered_targets", 40, context(style, shipping, baseline),
        )[1][0]

    def test_shipping_is_subtracted_from_delivered_target(self):
        change = self.calculate("Clear Back No Strap")
        self.assertEqual(change["discounted_new_price"], 20.0)
        self.assertEqual(change["new_price"], 33.33)
        self.assertEqual(change["proposed_total"], 26.0)

    def test_no_strap_three_caps(self):
        for shipping, item_price, total in (
            ("5.85", 14.33, 20.18), ("6.00", 14.18, 20.18), ("3.00", 14.33, 17.33),
        ):
            change = self.calculate("No strap", 2221, shipping, "13.33")
            self.assertEqual(change["discounted_new_price"], item_price)
            self.assertEqual(change["proposed_total"], total)

    def test_increase_only_and_idempotent(self):
        first = self.calculate("Clear Back No Strap")
        second = self.calculate("Clear Back No Strap", int(first["new_price"] * 100))
        self.assertEqual(second["old_price"], second["new_price"])
        above = self.calculate("Clear Back No Strap", 4000)
        self.assertEqual(above["status"], "déjà au-dessus de la cible")
        self.assertEqual(above["new_price"], 40.0)

    def test_unknown_shipping_is_excluded(self):
        ctx = context("Clear Back No Strap", None, error="destination France absente")
        change = backend._clean_inventory_payload(
            inventory("Clear Back No Strap"), backend.Decimal("0"), "full",
            "delivered_targets", 40, ctx,
        )[1][0]
        self.assertEqual(change["status"], "à vérifier")
        self.assertEqual(change["new_price"], 25.0)

    def test_detachable_with_strap_is_temporary_pricing_alias(self):
        with_strap = self.calculate("Rotate&Detach +Strap")
        self.assertTrue(with_strap["matched"])
        self.assertEqual(with_strap["pricing_rule"], "Rotate&Detach sans sangle - standard")
        plain = self.calculate("Rotate&Detach")
        self.assertEqual(plain["pricing_rule"], "Rotate&Detach sans sangle - standard")

    def test_normalizer_renames_and_prices_from_real_shipping(self):
        for shipping in ("0", "5.85", "6.25"):
            payload, _changes, counts = backend._normalize_rotate_inventory(
                inventory("Rotate&Detach +Strap"), backend.Decimal(shipping), 40, 29.99
            )
            product = payload["products"][0]
            self.assertEqual(product["property_values"][1]["values"], ["Rotate&Detach"])
            price = backend.Decimal(str(product["offerings"][0]["price"]))
            total = (price * backend.Decimal("0.60")).quantize(
                backend.Decimal("0.01"), rounding=backend.ROUND_HALF_UP
            ) + backend.Decimal(shipping)
            self.assertEqual(total, backend.Decimal("29.99"))
            self.assertEqual(counts["count_renamed"], 1)

    def test_normalizer_merges_only_lossless_duplicate(self):
        plus = inventory("Rotate&Detach +Strap")["products"][0]
        plain = inventory("Rotate&Detach")["products"][0]
        plain["offerings"][0]["is_enabled"] = False
        payload, _changes, counts = backend._normalize_rotate_inventory(
            {"products": [plus, plain]}, backend.Decimal("5.85"), 40, 29.99
        )
        self.assertEqual(len(payload["products"]), 1)
        self.assertTrue(payload["products"][0]["offerings"][0]["is_enabled"])
        self.assertEqual(counts["count_merged"], 1)

        plain = inventory("Rotate&Detach")["products"][0]
        plain["sku"] = "DIFFERENT"
        with self.assertRaises(backend.HTTPException):
            backend._normalize_rotate_inventory(
                {"products": [plus, plain]}, backend.Decimal("5.85"), 40, 29.99
            )

    def test_special_models_require_review(self):
        change = self.calculate("Clear Back No Strap", model="Kindle Scribe")
        self.assertFalse(change["matched"])
        self.assertEqual(change["status"], "à vérifier")

    def test_attraction_price_protects_only_exact_model_and_style(self):
        protected_config = dict(CONFIG)
        protected_config["attraction_price_overrides"] = [{
            "model": "SY69JL",
            "style": "No strap",
            "discounted_price_eur": 13.33,
        }]
        protected_context = context("No strap", "5.85")
        protected_context["config"] = backend._parse_delivered_targets(protected_config)
        change = backend._clean_inventory_payload(
            inventory("No strap", 2500, "SY69JL"), backend.Decimal("0"), "full",
            "delivered_targets", 40, protected_context,
        )[1][0]
        self.assertEqual(change["pricing_rule"], "Prix d’attraction")
        self.assertEqual(change["new_price"], 22.21)
        self.assertEqual(change["discounted_new_price"], 13.33)
        self.assertEqual(change["proposed_total"], 19.18)

        other_style = backend._clean_inventory_payload(
            inventory("Build-in Hand Strap", 2500, "SY69JL"),
            backend.Decimal("0"), "full", "delivered_targets", 40,
            protected_context,
        )[1][0]
        self.assertEqual(other_style["pricing_rule"], "Built-in Hand Strap")
        self.assertTrue(other_style["matched"])

    def test_inventory_fields_are_preserved(self):
        payload, _changes = backend._clean_inventory_payload(
            inventory("Clear Back No Strap"), backend.Decimal("0"), "full",
            "delivered_targets", 40, context("Clear Back No Strap"),
        )
        offering = payload["products"][0]["offerings"][0]
        self.assertEqual(offering["quantity"], 3)
        self.assertTrue(offering["is_enabled"])
        self.assertEqual(offering["readiness_state_id"], 7)
        self.assertEqual(payload["price_on_property"], [1, 2])


if __name__ == "__main__":
    unittest.main()
