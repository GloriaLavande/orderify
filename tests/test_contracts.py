import json
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("ETSY_API_KEY", "etsy-client")
os.environ.setdefault("ETSY_SECRET", "etsy-secret")
os.environ.setdefault("ETSY_REFRESH_TOKEN", "refresh-token")
os.environ.setdefault("REDIRECT_URI", "https://example.test/callback")
os.environ.setdefault("ORDERIFY_API_KEY", "orderify-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import main as backend


API_HEADERS = {"X-Orderify-Key": "orderify-test-key"}

DELIVERED_CONFIG = {
    "market": "FR",
    "currency": "EUR",
    "quantity": 1,
    "shipping_service": "standard",
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


def delivered_inventory(style, cents=2500, model="SA568B"):
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


def delivered_context(shipping="6.00", baseline="13.33", shipping_error=None):
    baseline_key = backend._stable_baseline_key(
        delivered_inventory("No strap")["products"][0], 0
    )
    return {
        "config": backend._parse_delivered_targets(DELIVERED_CONFIG),
        "shipping_cost": None if shipping is None else backend.Decimal(shipping),
        "shipping_source": "profil Etsy FR primary_cost" if shipping is not None else None,
        "shipping_error": shipping_error,
        "baselines": {baseline_key: backend.Decimal(baseline)},
        "promotion_source": "saisie manuelle vérifiée",
    }


class BackendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(backend.app)

    def setUp(self):
        backend._INVENTORY_CACHE.clear()
        backend.STATE.update({
            "access_token": "123.token",
            "refresh_token": "refresh-token",
            "expires_at": 9_999_999_999,
            "shop_id": None,
            "code_verifier": None,
            "oauth_state": None,
            "oauth_started_at": None,
        })

    def test_public_health_and_home_do_not_call_etsy(self):
        with patch.object(backend.ETSY_SESSION, "request") as request:
            health = self.client.get("/health")
            home = self.client.get("/")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(home.status_code, 200)
        self.assertIn("Orderify", home.text)
        request.assert_not_called()

    def test_business_routes_require_api_key(self):
        self.assertEqual(self.client.get("/orders").status_code, 401)
        self.assertEqual(self.client.get("/to-ship").status_code, 401)
        self.assertEqual(self.client.get("/listings-stats").status_code, 401)
        self.assertEqual(
            self.client.post("/listing-prices/42/adjust?amount=1").status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/listing-variations/42/normalize-rotate-detach",
                params={"sale_discount_percent": 40, "promotion_source": "test"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/listing-variations/42/ensure-attraction-anchor",
                params={"sale_discount_percent": 40, "promotion_source": "test"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/listing-variations/42/add-device-option",
                params={"source_device": "OLD", "new_device": "NEW"},
            ).status_code,
            401,
        )
        self.assertEqual(self.client.post("/listing-publish/42/preview", json={}).status_code, 401)
        self.assertEqual(self.client.post("/listing-publish/42/apply").status_code, 401)

    def test_publish_preview_is_read_only_and_returns_fresh_fingerprint(self):
        backend.STATE["shop_id"] = 7
        response = Mock(status_code=200)
        listing = {
            "listing_id": 42, "shop_id": 7, "state": "draft",
            "title": "Name", "description": "Description", "featured_rank": 1,
            "images": [{"listing_image_id": 10, "rank": 1}],
        }
        inventory = delivered_inventory("No strap")
        payload = {
            "title": "Blue Library Kindle Case",
            "description": "Ready for Etsy",
            "quantity": 73,
            "sku_prefix": "BLU",
            "image_sha256s": ["a" * 64, "b" * 64],
        }
        with patch.object(backend, "etsy_request", side_effect=[
            (response, listing), (response, inventory),
            (response, {"personalization_questions": []}), (response, {"results": []}),
        ]) as request:
            result = self.client.post(
                "/listing-publish/42/preview", headers=API_HEADERS, json=payload
            )
        self.assertEqual(result.status_code, 200)
        body = result.json()
        self.assertFalse(body["applied"])
        self.assertEqual(body["existing_image_count"], 1)
        self.assertEqual(body["new_image_count"], 2)
        self.assertEqual(len(body["plan_sha256"]), 64)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET"] * 4)

    def test_publish_preview_does_not_reject_unreliable_draft_feature_rank(self):
        backend.STATE["shop_id"] = 7
        response = Mock(status_code=200)
        listing = {
            "listing_id": 42, "shop_id": 7, "state": "draft",
            "title": "Name", "description": "Description", "featured_rank": 0,
            "images": [{"listing_image_id": 10, "rank": 1}],
        }
        payload = {
            "title": "Blue Library Kindle Case", "description": "Ready for Etsy",
            "quantity": 73, "sku_prefix": "BLU", "image_sha256s": ["a" * 64],
        }
        with patch.object(backend, "etsy_request", side_effect=[
            (response, listing), (response, delivered_inventory("No strap")),
            (response, {"personalization_questions": []}), (response, {"results": []}),
        ]):
            result = self.client.post(
                "/listing-publish/42/preview", headers=API_HEADERS, json=payload
            )
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["featured"])

    def test_publish_apply_rejects_stale_preview_before_any_write(self):
        backend.STATE["shop_id"] = 7
        response = Mock(status_code=200)
        listing = {
            "listing_id": 42, "shop_id": 7, "state": "draft",
            "title": "Name", "description": "Description", "featured_rank": 1,
            "images": [{"listing_image_id": 10, "rank": 1}],
        }
        inventory = delivered_inventory("No strap")
        blob = b"final-png"
        digest = backend.hashlib.sha256(blob).hexdigest()
        data = {
            "title": "Blue Library Kindle Case",
            "description": "Ready for Etsy",
            "quantity": "73",
            "sku_prefix": "BLU",
            "image_sha256s": json.dumps([digest]),
            "expected_plan_sha256": "0" * 64,
            "confirmation": "PUBLISH",
        }
        with patch.object(backend, "etsy_request", side_effect=[
            (response, listing), (response, inventory),
            (response, {"personalization_questions": []}), (response, {"results": []}),
        ]) as request:
            result = self.client.post(
                "/listing-publish/42/apply", headers=API_HEADERS, data=data,
                files=[("images", ("mockup.png", blob, "image/png"))],
            )
        self.assertEqual(result.status_code, 409)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET"] * 4)

    def test_publish_inventory_changes_only_quantity_and_sku(self):
        inventory = delivered_inventory("No strap", cents=4023)
        payload = backend._publish_inventory_payload(inventory, 73, "BLU")
        product = payload["products"][0]
        self.assertRegex(product["sku"], r"^[A-Z]{5}$")
        self.assertEqual(product["property_values"], inventory["products"][0]["property_values"])
        self.assertEqual(product["offerings"][0]["quantity"], 73)
        self.assertEqual(product["offerings"][0]["price"], 40.23)
        self.assertEqual(payload["price_on_property"], inventory["price_on_property"])

    def test_publish_verification_accepts_safe_etsy_text_normalization(self):
        verified = {
            "state": "draft",
            "title": "Luna &amp; Books",
            "description": "First line  \r\nSecond line\r\n",
            "images": [
                {"listing_image_id": 101, "rank": 1},
                {"listing_image_id": 102, "rank": 2},
            ],
        }
        self.assertEqual(
            backend._publish_draft_mismatches(
                verified, "Luna & Books", "First line\nSecond line", [101, 102]
            ),
            [],
        )

    def test_publish_verification_keeps_image_order_strict(self):
        verified = {
            "state": "draft", "title": "Title", "description": "Description",
            "images": [
                {"listing_image_id": 102, "rank": 1},
                {"listing_image_id": 101, "rank": 2},
            ],
        }
        mismatches = backend._publish_draft_mismatches(
            verified, "Title", "Description", [101, 102]
        )
        self.assertTrue(any("ordre_images" in item for item in mismatches))

    def test_publish_builds_an_independent_draft_clone(self):
        listing = {
            "title": "Template", "description": "Template description",
            "quantity": 1, "taxonomy_id": 123, "who_made": "i_did",
            "when_made": "made_to_order", "is_supply": False,
            "is_customizable": True, "should_auto_renew": True,
            "is_taxable": True, "listing_type": "physical",
            "shipping_profile_id": 55, "return_policy_id": 66,
            "shop_section_id": 77, "tags": ["kindle case", "reader gift"],
            "materials": ["plastic"],
        }
        inventory = delivered_inventory("No strap", cents=4023)
        inventory["products"][0]["offerings"][0]["readiness_state_id"] = 9
        payload = backend._draft_clone_payload(
            listing, inventory, "New title", "New description"
        )
        self.assertEqual(payload["title"], "New title")
        self.assertEqual(payload["price"], 40.23)
        self.assertEqual(payload["taxonomy_id"], 123)
        self.assertEqual(payload["shipping_profile_id"], 55)
        self.assertEqual(payload["readiness_state_id"], 9)
        self.assertEqual(payload["tags"], "kindle case,reader gift")

    def test_publish_personalization_copy_drops_source_ids(self):
        payload = backend._personalization_copy_payload({
            "personalization_questions": [{
                "question_id": 99, "question_type": "dropdown",
                "question_text": "Choose a font", "required": False,
                "options": [{"option_id": 1, "label": "Font A"}],
            }]
        })
        question = payload["personalization_questions"][0]
        self.assertNotIn("question_id", question)
        self.assertEqual(question["options"], [{"label": "Font A"}])

    def test_publish_applies_changes_to_clone_and_preserves_template(self):
        inventory = delivered_inventory("No strap", cents=4023)
        inventory["products"][0]["offerings"][0]["readiness_state_id"] = 9
        title, description = "New title", "New description"
        blob = b"new-image"
        digest = backend.hashlib.sha256(blob).hexdigest()
        context = {
            "shop_id": 7,
            "listing": {
                "listing_id": 42, "taxonomy_id": 123, "who_made": "i_did",
                "when_made": "made_to_order", "listing_type": "physical",
            },
            "inventory": inventory,
            "desired_inventory": backend._publish_inventory_payload(inventory, 73, "BLU"),
            "existing_images": [], "properties": {"results": []},
            "personalization": {"personalization_questions": []},
            "plan_sha256": "a" * 64, "title": title, "description": description,
        }
        uploaded = Mock(filename="new.png", content_type="image/png", file=io.BytesIO(blob))
        ok = Mock(status_code=200)
        created = {"listing_id": 900}
        verified = {
            "state": "draft", "title": title, "description": description,
            "images": [{"listing_image_id": 201, "rank": 1}],
        }
        activated = {"listing_id": 900, "url": "https://www.etsy.com/listing/900/new"}
        with patch.object(backend, "_publish_context", return_value=context), \
             patch.object(backend, "_upload_listing_image", return_value=201), \
             patch.object(backend, "_force_listing_image_order", return_value=[201]), \
             patch.object(backend, "_copy_listing_properties"), \
             patch.object(backend, "etsy_request", side_effect=[
                 (ok, created), (ok, {}), (ok, verified), (ok, activated),
             ]) as request:
            result = backend.apply_listing_publish(
                42, title, description, 73, "BLU", json.dumps([digest]),
                "a" * 64, "PUBLISH", [uploaded],
            )
        self.assertEqual(result["listing_id"], 900)
        self.assertEqual(result["template_listing_id"], 42)
        self.assertTrue(result["template_preserved"])
        paths = [call.args[1] for call in request.call_args_list]
        self.assertEqual(paths[0], "/shops/7/listings")
        self.assertNotIn("/shops/7/listings/42", paths)

    def test_add_device_payload_clones_every_source_combination(self):
        first = delivered_inventory("No strap", model="OLD")
        second = delivered_inventory("Clear Back No Strap", model="OLD")["products"][0]
        unrelated = delivered_inventory("No strap", model="OTHER")["products"][0]
        inventory = {**first, "products": [first["products"][0], second, unrelated]}

        payload, changes, counts = backend._add_device_option_payload(
            inventory, "OLD", "NEW-CODE"
        )

        self.assertTrue(counts["affected"])
        self.assertEqual(counts["count_added"], 2)
        self.assertEqual(len(payload["products"]), 5)
        added = [
            product for product in payload["products"]
            if backend._normalized_variant_name(backend._device_value(product)) == "new code"
        ]
        self.assertEqual(len(added), 2)
        self.assertEqual(
            {backend._product_model_style(item["property_values"], item["sku"])[1] for item in added},
            {"No strap", "Clear Back No Strap"},
        )
        self.assertTrue(all(
            item["property_values"][0]["value_ids"] == [] for item in added
        ))
        self.assertEqual(len(changes), 2)

        idempotent, no_changes, repeated = backend._add_device_option_payload(
            payload, "OLD", "NEW-CODE"
        )
        self.assertTrue(repeated["already_exists"])
        self.assertFalse(repeated["affected"])
        self.assertEqual(no_changes, [])
        self.assertEqual(len(idempotent["products"]), 5)

    def test_add_device_payload_rejects_missing_source_and_partial_target(self):
        inventory = delivered_inventory("No strap", model="OLD")
        with self.assertRaises(backend.HTTPException):
            backend._add_device_option_payload(inventory, "MISSING", "NEW")

        second = delivered_inventory("Clear Back No Strap", model="OLD")["products"][0]
        partial = delivered_inventory("No strap", model="NEW")["products"][0]
        inventory["products"].extend([second, partial])
        with self.assertRaises(backend.HTTPException) as raised:
            backend._add_device_option_payload(inventory, "OLD", "NEW")
        self.assertEqual(raised.exception.status_code, 422)

    def test_add_device_payload_supports_exact_custom_style_matrix(self):
        styles = [
            "Rotate&Detach",
            "Built-in Hand Strap",
            "No strap",
            "ClearBack Hand Strap",
            "Clear Back No Strap",
        ]
        inventory = delivered_inventory(styles[0], model="SOURCE")
        inventory["products"] = [
            delivered_inventory(style, model="SOURCE")["products"][0]
            for style in styles
        ]

        basic_payload, _changes, basic_counts = backend._add_device_option_payload(
            inventory,
            "SOURCE",
            "Z8PC57",
            ["Rotate&Detach", "No strap", "ClearBack Hand Strap", "Clear Back No Strap"],
        )
        basic_styles = {
            backend._product_model_style(product["property_values"], product["sku"])[1]
            for product in basic_payload["products"]
            if backend._device_value(product) == "Z8PC57"
        }
        self.assertEqual(basic_counts["count_added"], 5)
        self.assertEqual(basic_counts["count_enabled_added"], 4)
        self.assertEqual(basic_counts["count_disabled_placeholders"], 1)
        self.assertEqual(basic_styles, set(styles))
        basic_changes = {change["style"]: change["is_enabled"] for change in _changes}
        self.assertFalse(basic_changes["Built-in Hand Strap"])
        self.assertTrue(all(
            basic_changes[style]
            for style in styles if style != "Built-in Hand Strap"
        ))

        paperwhite_payload, _changes, paperwhite_counts = backend._add_device_option_payload(
            inventory,
            "SOURCE",
            "KPW7/Z4P32D",
            ["Rotate&Detach", "No strap", "Clear Back No Strap"],
        )
        paperwhite_styles = {
            backend._product_model_style(product["property_values"], product["sku"])[1]
            for product in paperwhite_payload["products"]
            if backend._device_value(product) == "KPW7/Z4P32D"
        }
        self.assertEqual(paperwhite_counts["count_added"], 5)
        self.assertEqual(paperwhite_counts["count_enabled_added"], 3)
        self.assertEqual(paperwhite_counts["count_disabled_placeholders"], 2)
        self.assertEqual(paperwhite_styles, set(styles))
        paperwhite_changes = {change["style"]: change["is_enabled"] for change in _changes}
        self.assertFalse(paperwhite_changes["Built-in Hand Strap"])
        self.assertFalse(paperwhite_changes["ClearBack Hand Strap"])

        with self.assertRaises(backend.HTTPException) as raised:
            backend._add_device_option_payload(
                inventory, "SOURCE", "OTHER", ["Style inexistant"]
            )
        self.assertIn("Styles absents", raised.exception.detail)

    def test_add_device_detects_unnamed_etsy_device_property_by_exact_value(self):
        inventory = delivered_inventory("No strap", model="RS23CV/C2V2L3")
        inventory["products"][0]["property_values"][0].pop("property_name")
        payload, changes, counts = backend._add_device_option_payload(
            inventory, "RS23CV/C2V2L3", "Z8PC57", ["No strap"]
        )
        self.assertEqual(counts["count_added"], 1)
        self.assertEqual(changes[0]["new_device"], "Z8PC57")
        added = payload["products"][1]
        self.assertEqual(added["property_values"][0]["values"], ["Z8PC57"])
        repeated, no_changes, repeated_counts = backend._add_device_option_payload(
            payload, "RS23CV/C2V2L3", "Z8PC57", ["No strap"]
        )
        self.assertTrue(repeated_counts["already_exists"])
        self.assertEqual(no_changes, [])
        self.assertEqual(len(repeated["products"]), 2)

    def test_add_device_listing_requires_fresh_preview_and_verifies_full_inventory(self):
        response = type("Response", (), {"status_code": 200})()
        before = delivered_inventory("No strap", model="OLD")
        listing = {"listing_id": 42, "shop_id": 7, "title": "Case", "state": "active"}
        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[
                 (response, listing), (response, before),
             ]):
            preview = backend.add_device_option_listing(42, "OLD", "NEW")
        self.assertTrue(preview["affected"])
        self.assertFalse(preview["applied"])

        expected, _changes, _counts = backend._add_device_option_payload(before, "OLD", "NEW")
        verified = json.loads(json.dumps(expected))
        verified["products"][1]["property_values"][0]["value_ids"] = [987654]
        verified["products"][1]["offerings"][0]["price"] = {
            "amount": 2500, "divisor": 100, "currency_code": "EUR"
        }
        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[
                 (response, listing), (response, before), (response, {}), (response, verified),
             ]) as request:
            result = backend.add_device_option_listing(
                42,
                "OLD",
                "NEW",
                apply=True,
                expected_inventory_sha256=preview["inventory_sha256"],
                expected_operation_sha256=preview["operation_sha256"],
            )
        self.assertTrue(result["applied"])
        self.assertTrue(result["verified"])
        self.assertEqual(request.call_args_list[2].args[:2], ("PUT", "/listings/42/inventory"))

        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[
                 (response, listing), (response, before),
             ]) as request:
            with self.assertRaises(backend.HTTPException) as raised:
                backend.add_device_option_listing(
                    42, "OLD", "NEW", apply=True,
                    expected_inventory_sha256="stale",
                    expected_operation_sha256=preview["operation_sha256"],
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(request.call_count, 2)

    def test_unconfigured_api_key_keeps_personal_installation_compatible(self):
        with patch.object(backend, "API_KEY", None), \
             patch.object(backend, "get_receipts", return_value=(7, {"results": []})):
            response = self.client.get("/orders")
        self.assertEqual(response.status_code, 200)

    def test_orders_and_legacy_alias_share_the_same_contract(self):
        payload = {"results": [{"receipt_id": 42}]}
        with patch.object(backend, "get_receipts", return_value=(7, payload)):
            current = self.client.get("/orders", headers=API_HEADERS)
            legacy = self.client.get("/test-orders", headers=API_HEADERS)
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json(), legacy.json())
        self.assertEqual(current.json()["data"], payload)

    def test_legacy_routes_are_hidden_but_available(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        expected = {
            "/health", "/authorize", "/callback", "/orders", "/orders-full",
            "/to-ship", "/receipt/{receipt_id}", "/receipt-status/{receipt_id}",
            "/ship/{receipt_id}", "/carriers", "/listings-stats",
            "/status/config", "/admin/refresh-token",
        }
        self.assertTrue(expected.issubset(paths))
        self.assertNotIn("/test-orders", paths)
        self.assertNotIn("/debug-env", paths)
        self.assertNotIn("/refresh-token", paths)

    def test_receipt_rows_preserve_complete_data_contract(self):
        receipt = {
            "receipt_id": 4111730107,
            "name": "Buyer",
            "buyer_email": "buyer@example.test",
            "formatted_address": "Buyer\n1 Test Street",
            "first_line": "1 Test Street",
            "city": "Paris",
            "zip": "75001",
            "country_iso": "FR",
            "is_paid": True,
            "is_shipped": True,
            "status": "completed",
            "created_timestamp": 1_700_000_000,
            "grandtotal": {"amount": 2500, "divisor": 100},
            "total_shipping_cost": {"amount": 500, "divisor": 100},
            "transactions": [{
                "transaction_id": 99,
                "listing_id": 42,
                "title": "Case",
                "quantity": 2,
                "sku": "CASE-1",
                "personalization": "CF",
                "variations": [{"formatted_name": "Device", "formatted_value": "SA568B"}],
                "price": {"amount": 1000, "divisor": 100, "currency_code": "EUR"},
                "expected_ship_date": 1_700_100_000,
            }],
        }
        with patch.object(backend, "get_listing_thumbnail", return_value="https://image.test/x.png"):
            rows = backend.receipt_to_rows(receipt, now=1_700_050_000)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["receipt_id"], 4111730107)
        self.assertEqual(row["variations"], "Device: SA568B")
        self.assertEqual(row["personalization"], "CF")
        self.assertEqual(row["thumbnail_url"], "https://image.test/x.png")
        self.assertTrue(row["is_shipped"])
        for required in ("formatted_address", "quantity", "sku", "grandtotal", "expected_ship_date", "is_late"):
            self.assertIn(required, row)

    def test_multi_article_receipt_keeps_one_row_per_transaction(self):
        receipt = {
            "receipt_id": 10,
            "transactions": [
                {"transaction_id": 1, "listing_id": None, "quantity": 1},
                {"transaction_id": 2, "listing_id": None, "quantity": 3},
            ],
        }
        rows = backend.receipt_to_rows(receipt, include_thumbnails=False)
        self.assertEqual([row["transaction_id"] for row in rows], [1, 2])

    def test_missing_transaction_sku_falls_back_to_current_listing_inventory(self):
        receipt = {
            "receipt_id": 4196673810,
            "transactions": [{
                "transaction_id": 5243860899,
                "listing_id": 77,
                "sku": None,
                "variations": [{
                    "property_id": 1,
                    "value_id": 8,
                    "formatted_name": "Personalization",
                    "formatted_value": "8",
                }],
            }],
        }
        with patch.object(
            backend,
            "get_listing_inventory_sku",
            return_value="CSTR",
        ) as fallback:
            rows = backend.receipt_to_rows(receipt, include_thumbnails=False)
        self.assertEqual(rows[0]["sku"], "CSTR")
        self.assertEqual(rows[0]["sku_source"], "listing_inventory")
        fallback.assert_called_once_with(77, receipt["transactions"][0]["variations"])

    def test_inventory_sku_matches_the_ordered_variation_values(self):
        response = type("Response", (), {"status_code": 200})()
        inventory = {"products": [
            {
                "sku": "WRONG",
                "property_values": [{
                    "property_id": 1,
                    "property_name": "Personalization",
                    "value_ids": [7],
                    "values": ["7"],
                }],
            },
            {
                "sku": "CSTR",
                "property_values": [{
                    "property_id": 1,
                    "property_name": "Personalization",
                    "value_ids": [8],
                    "values": ["8"],
                }],
            },
        ]}
        variations = [{
            "property_id": 1,
            "value_id": 8,
            "formatted_name": "Personalization",
            "formatted_value": "8",
        }]
        with patch.object(backend, "etsy_request", return_value=(response, inventory)):
            self.assertEqual(backend.get_listing_inventory_sku(77, variations), "CSTR")

    def test_shipping_contract_and_payload_are_preserved(self):
        response = type("Response", (), {"status_code": 200})()
        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", return_value=(response, {"tracking": "ok"})) as request:
            result = backend.mark_as_shipped(42, "TRACK", "la-poste", "Merci")
        self.assertTrue(result["success"])
        request.assert_called_once_with(
            "POST",
            "/shops/7/receipts/42/tracking",
            data={"tracking_code": "TRACK", "carrier_name": "la-poste", "note_to_buyer": "Merci"},
        )

    def test_listing_stats_preserve_complete_export_fields(self):
        response = type("Response", (), {"status_code": 200})()
        listing_page = {"results": [{
            "listing_id": 9,
            "title": "Case",
            "description": "Description",
            "tags": ["kindle"],
            "materials": ["plastic"],
            "price": {"amount": 2000, "divisor": 100, "currency_code": "EUR"},
            "images": [{"url_fullxfull": "https://image.test/listing.png"}],
            "views": 20,
            "num_favorers": 3,
            "state": "active",
        }]}
        receipts_page = {"results": [{"transactions": [{
            "listing_id": 9,
            "quantity": 2,
            "price": {"amount": 2000, "divisor": 100},
        }]}]}
        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[(response, listing_page), (response, receipts_page)]):
            result = backend.listings_stats("30")
        row = result["rows"][0]
        self.assertEqual(row["sales_quantity"], 2)
        self.assertEqual(row["sales_revenue"], 40.0)
        for required in ("description", "tags", "materials", "views_lifetime", "thumbnail_url", "sales_orders_count"):
            self.assertIn(required, row)

    def test_price_preview_preserves_inventory_and_adds_fixed_amount(self):
        response = type("Response", (), {"status_code": 200})()
        inventory = {
            "products": [{
                "product_id": 99,
                "sku": "CASE-1",
                "property_values": [{
                    "property_id": 1,
                    "property_name": "Style",
                    "scale_id": None,
                    "value_ids": [10],
                    "values": ["Clear Back"],
                    "scale_name": "ignored",
                }],
                "offerings": [{
                    "offering_id": 88,
                    "quantity": 7,
                    "is_enabled": True,
                    "price": {"amount": 2320, "divisor": 100, "currency_code": "EUR"},
                    "readiness_state_id": 123,
                }],
            }],
            "price_on_property": [1],
            "quantity_on_property": [],
            "sku_on_property": [1],
        }
        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[
                 (response, {"listing_id": 42, "shop_id": 7, "title": "Case", "state": "active"}),
                 (response, inventory),
             ]):
            result = backend.listing_price_adjustment(42, "1.00", False)
        self.assertFalse(result["applied"])
        self.assertEqual(result["changes"][0]["old_price"], 23.2)
        self.assertEqual(result["changes"][0]["new_price"], 24.2)
        adjusted = result["backup_payload"]["products"][0]
        self.assertNotIn("product_id", adjusted)
        self.assertNotIn("offering_id", adjusted["offerings"][0])
        self.assertEqual(adjusted["offerings"][0]["price"], 23.2)
        self.assertEqual(adjusted["offerings"][0]["quantity"], 7)

    def test_no_strap_price_policy_can_exclude_or_halve_adjustment(self):
        inventory = {
            "products": [
                {
                    "sku": "NO-STRAP",
                    "property_values": [{"property_id": 1, "values": ["No strap"]}],
                    "offerings": [{
                        "quantity": 1,
                        "is_enabled": True,
                        "price": {"amount": 2221, "divisor": 100},
                    }],
                },
                {
                    "sku": "WITH-STRAP",
                    "property_values": [{"property_id": 1, "values": ["Rotate + strap"]}],
                    "offerings": [{
                        "quantity": 1,
                        "is_enabled": True,
                        "price": {"amount": 2821, "divisor": 100},
                    }],
                },
            ],
        }
        _, excluded = backend._clean_inventory_payload(
            inventory, backend.Decimal("1.00"), "exclude"
        )
        self.assertEqual(excluded[0]["new_price"], 22.21)
        self.assertEqual(excluded[0]["applied_adjustment"], 0.0)
        self.assertEqual(excluded[1]["new_price"], 29.21)

        _, halved = backend._clean_inventory_payload(
            inventory, backend.Decimal("1.00"), "half"
        )
        self.assertEqual(halved[0]["new_price"], 22.71)
        self.assertEqual(halved[0]["applied_adjustment"], 0.5)
        self.assertEqual(halved[1]["new_price"], 29.21)

    def test_kindle_sale_targets_match_requested_discounted_prices(self):
        variants = [
            ("No strap", 2221, 14.33, 23.88),
            ("Clear Back No Strap", 2655, 18.90, 31.50),
            ("Build-in Hand Strap", 3155, 21.90, 36.50),
            ("ClearBack Hand Strap", 2988, 21.60, 36.00),
            ("Rotate&Detach sans sangle", 3321, 23.90, 39.83),
            ("Rotate&Detach sans sangle", 2921, 21.70, 36.17),
        ]
        inventory = {"products": [
            {
                "sku": f"SKU-{index}",
                "property_values": [{"property_id": 1, "values": [name]}],
                "offerings": [{
                    "quantity": 1,
                    "is_enabled": True,
                    "price": {"amount": cents, "divisor": 100},
                }],
            }
            for index, (name, cents, _discounted, _base) in enumerate(variants)
        ]}
        _, changes = backend._clean_inventory_payload(
            inventory,
            backend.Decimal("0"),
            "full",
            "kindle_sale_targets",
            40,
        )
        self.assertEqual(
            [change["discounted_new_price"] for change in changes],
            [item[2] for item in variants],
        )
        self.assertEqual(
            [change["new_price"] for change in changes],
            [item[3] for item in variants],
        )
        self.assertTrue(all(change["matched"] for change in changes))

        reapplied_inventory = {"products": [
            {
                "sku": f"SKU-{index}",
                "property_values": [{"property_id": 1, "values": [name]}],
                "offerings": [{
                    "quantity": 1,
                    "is_enabled": True,
                    "price": {"amount": int(base * 100), "divisor": 100},
                }],
            }
            for index, (name, _cents, _discounted, base) in enumerate(variants)
        ]}
        _, reapplied_changes = backend._clean_inventory_payload(
            reapplied_inventory,
            backend.Decimal("0"),
            "full",
            "kindle_sale_targets",
            40,
        )
        self.assertTrue(all(
            change["old_price"] == change["new_price"]
            for change in reapplied_changes
        ))

    def test_kindle_sale_targets_leave_unknown_variant_unchanged_for_preview(self):
        inventory = {"products": [{
            "sku": "UNKNOWN",
            "property_values": [{"property_id": 1, "values": ["Mystery style"]}],
            "offerings": [{
                "quantity": 1,
                "is_enabled": True,
                "price": {"amount": 2000, "divisor": 100},
            }],
        }]}
        _, changes = backend._clean_inventory_payload(
            inventory, backend.Decimal("0"), "full", "kindle_sale_targets", 40
        )
        self.assertFalse(changes[0]["matched"])
        self.assertEqual(changes[0]["new_price"], 20.0)

    def test_delivered_target_subtracts_real_shipping_and_uses_verified_discount(self):
        inventory = delivered_inventory("Clear Back No Strap", cents=2500)
        payload, changes = backend._clean_inventory_payload(
            inventory, backend.Decimal("0"), "full", "delivered_targets", 40,
            delivered_context("6.00"),
        )
        change = changes[0]
        self.assertEqual(change["discounted_new_price"], 20.0)
        self.assertEqual(change["new_price"], 33.33)
        self.assertEqual(change["proposed_total"], 26.0)
        self.assertEqual(change["shipping_cost"], 6.0)
        self.assertEqual(change["promotion_source"], "saisie manuelle vérifiée")
        self.assertEqual(change["status"], "modification")
        self.assertEqual(payload["products"][0]["offerings"][0]["quantity"], 3)
        self.assertEqual(payload["products"][0]["offerings"][0]["readiness_state_id"], 7)

    def test_no_strap_uses_all_three_caps_from_saved_baseline(self):
        cases = (
            ("5.85", 14.33, 20.18),
            ("6.00", 14.18, 20.18),
            ("3.00", 14.33, 17.33),
        )
        for shipping, expected_item, expected_total in cases:
            with self.subTest(shipping=shipping):
                _, changes = backend._clean_inventory_payload(
                    delivered_inventory("No strap", cents=2221),
                    backend.Decimal("0"), "full", "delivered_targets", 40,
                    delivered_context(shipping, baseline="13.33"),
                )
                self.assertEqual(changes[0]["discounted_new_price"], expected_item)
                self.assertEqual(changes[0]["proposed_total"], expected_total)
                self.assertEqual(changes[0]["baseline_discounted_price"], 13.33)

    def test_attraction_price_protects_only_the_exact_model_and_style(self):
        config = dict(DELIVERED_CONFIG)
        config["attraction_price_overrides"] = [{
            "model": "SY69JL",
            "style": "No strap",
            "discounted_price_eur": 13.33,
        }]
        context = delivered_context("5.85")
        context["config"] = backend._parse_delivered_targets(config)

        _, anchor = backend._clean_inventory_payload(
            delivered_inventory("No strap", cents=2500, model="SY69JL"),
            backend.Decimal("0"), "full", "delivered_targets", 40, context,
        )
        self.assertEqual(anchor[0]["pricing_rule"], "Prix d’attraction")
        self.assertEqual(anchor[0]["discounted_new_price"], 13.33)
        self.assertEqual(anchor[0]["new_price"], 22.21)
        self.assertEqual(anchor[0]["proposed_total"], 19.18)
        self.assertEqual(anchor[0]["status_detail"], "prix d’attraction protégé")

        _, other_style = backend._clean_inventory_payload(
            delivered_inventory("Build-in Hand Strap", cents=2500, model="SY69JL"),
            backend.Decimal("0"), "full", "delivered_targets", 40, context,
        )
        self.assertEqual(other_style[0]["pricing_rule"], "Built-in Hand Strap")
        self.assertTrue(other_style[0]["matched"])

    def test_attraction_price_is_always_visible_and_dedicated_payload_changes_nothing_else(self):
        disabled = delivered_inventory("No strap", cents=3000, model="SY69JL")
        disabled["products"][0]["offerings"][0]["is_enabled"] = False
        unrelated = delivered_inventory("Build-in Hand Strap", cents=3550, model="SY69JL")["products"][0]
        inventory = {**disabled, "products": [disabled["products"][0], unrelated]}

        config = dict(DELIVERED_CONFIG)
        config["attraction_price_overrides"] = [{
            "model": "SY69JL", "style": "No strap", "discounted_price_eur": 13.33,
        }]
        context = delivered_context("5.85")
        context["config"] = backend._parse_delivered_targets(config)
        payload, changes = backend._clean_inventory_payload(
            inventory, backend.Decimal("0"), "full", "delivered_targets", 40, context
        )
        self.assertTrue(payload["products"][0]["offerings"][0]["is_enabled"])
        self.assertEqual(changes[0]["discounted_new_price"], 13.33)

        anchor_payload, anchor_changes, counts = backend._ensure_attraction_anchor_payload(
            inventory, 40
        )
        self.assertTrue(counts["anchor_found"])
        self.assertEqual(counts["count_reenabled"], 1)
        self.assertEqual(anchor_payload["products"][0]["offerings"][0]["price"], 22.21)
        self.assertTrue(anchor_payload["products"][0]["offerings"][0]["is_enabled"])
        self.assertEqual(anchor_payload["products"][1]["offerings"][0]["price"], 35.5)
        self.assertEqual(anchor_changes[0]["discounted_price"], 13.33)

    def test_delivered_mode_is_increase_only_and_idempotent(self):
        first_inventory = delivered_inventory("Clear Back No Strap", cents=2500)
        _, first = backend._clean_inventory_payload(
            first_inventory, backend.Decimal("0"), "full", "delivered_targets", 40,
            delivered_context("6.00"),
        )
        target_cents = int(round(first[0]["new_price"] * 100))
        _, second = backend._clean_inventory_payload(
            delivered_inventory("Clear Back No Strap", cents=target_cents),
            backend.Decimal("0"), "full", "delivered_targets", 40,
            delivered_context("6.00"),
        )
        self.assertEqual(second[0]["old_price"], second[0]["new_price"])
        self.assertEqual(second[0]["status"], "inchangé")

        _, above = backend._clean_inventory_payload(
            delivered_inventory("Clear Back No Strap", cents=4000),
            backend.Decimal("0"), "full", "delivered_targets", 40,
            delivered_context("6.00"),
        )
        self.assertEqual(above[0]["new_price"], 40.0)
        self.assertEqual(above[0]["status"], "déjà au-dessus de la cible")

    def test_delivered_mode_reports_unknown_shipping_and_sensitive_models(self):
        _, no_shipping = backend._clean_inventory_payload(
            delivered_inventory("Clear Back No Strap"),
            backend.Decimal("0"), "full", "delivered_targets", 40,
            delivered_context(None, shipping_error="destination France absente"),
        )
        self.assertEqual(no_shipping[0]["status"], "à vérifier")
        self.assertEqual(no_shipping[0]["new_price"], 25.0)

        _, special_model = backend._clean_inventory_payload(
            delivered_inventory("Clear Back No Strap", model="Kindle Scribe"),
            backend.Decimal("0"), "full", "delivered_targets", 40,
            delivered_context("6.00"),
        )
        self.assertFalse(special_model[0]["matched"])
        self.assertEqual(special_model[0]["status"], "à vérifier")

        _, detach_with_strap = backend._clean_inventory_payload(
            delivered_inventory("Rotate&Detach +Strap"),
            backend.Decimal("0"), "full", "delivered_targets", 40,
            delivered_context("6.00"),
        )
        self.assertTrue(detach_with_strap[0]["matched"])
        self.assertEqual(
            detach_with_strap[0]["pricing_rule"],
            "Rotate&Detach sans sangle - standard",
        )

        _, plain_detach = backend._clean_inventory_payload(
            delivered_inventory("Rotate&Detach"),
            backend.Decimal("0"), "full", "delivered_targets", 40,
            delivered_context("6.00"),
        )
        self.assertTrue(plain_detach[0]["matched"])
        self.assertEqual(
            plain_detach[0]["pricing_rule"],
            "Rotate&Detach sans sangle - standard",
        )

    def test_rotate_normalization_renames_legacy_style_and_targets_delivered_total(self):
        for shipping in ("0", "5.85", "6.25"):
            with self.subTest(shipping=shipping):
                inventory = delivered_inventory("Rotate&Detach +Strap", cents=3822)
                payload, changes, counts = backend._normalize_rotate_inventory(
                    inventory, backend.Decimal(shipping), 40, backend.Decimal("29.99")
                )
                product = payload["products"][0]
                self.assertEqual(product["property_values"][1]["values"], ["Rotate&Detach"])
                self.assertEqual(product["property_values"][1]["value_ids"], [])
                final_price = backend.Decimal(str(product["offerings"][0]["price"]))
                final_total = (final_price * backend.Decimal("0.60")).quantize(
                    backend.Decimal("0.01"), rounding=backend.ROUND_HALF_UP
                ) + backend.Decimal(shipping)
                self.assertEqual(final_total, backend.Decimal("29.99"))
                self.assertEqual(counts["count_renamed"], 1)
                self.assertEqual(counts["count_merged"], 0)
                self.assertEqual(changes[0]["delivered_total"], 29.99)
                audit = backend._audit_rotate_inventory(payload, backend.Decimal(shipping), 40, 29.99)
                self.assertEqual(audit["count_legacy_remaining"], 0)
                self.assertEqual(audit["count_duplicate_plain"], 0)
                self.assertEqual(audit["count_wrong_delivered_total"], 0)

    def test_rotate_normalization_merges_duplicates_and_combines_enabled_state(self):
        plus = delivered_inventory("Rotate&Detach +Strap", cents=3000)["products"][0]
        plain = delivered_inventory("Rotate&Detach", cents=3200)["products"][0]
        plus["offerings"][0]["is_enabled"] = True
        plain["offerings"][0]["is_enabled"] = False
        inventory = {
            "products": [plus, plain],
            "price_on_property": [1, 2],
            "quantity_on_property": [],
            "sku_on_property": [1, 2],
            "readiness_state_on_property": [2],
        }
        payload, _changes, counts = backend._normalize_rotate_inventory(
            inventory, backend.Decimal("5.85"), 40, backend.Decimal("29.99")
        )
        self.assertEqual(len(payload["products"]), 1)
        self.assertEqual(payload["products"][0]["property_values"][1]["values"], ["Rotate&Detach"])
        self.assertTrue(payload["products"][0]["offerings"][0]["is_enabled"])
        self.assertEqual(payload["products"][0]["sku"], "CASE-1")
        self.assertEqual(payload["products"][0]["offerings"][0]["quantity"], 3)
        self.assertEqual(payload["products"][0]["offerings"][0]["readiness_state_id"], 7)
        self.assertEqual(payload["readiness_state_on_property"], [2])
        self.assertEqual(counts["count_merged"], 1)
        self.assertEqual(counts["count_removed"], 1)
        self.assertEqual(counts["count_enabled_promoted"], 1)

    def test_rotate_normalization_blocks_lossy_merge_and_protects_attraction_price(self):
        plus = delivered_inventory("Rotate&Detach +Strap", cents=3000)["products"][0]
        plain = delivered_inventory("Rotate&Detach", cents=3200)["products"][0]
        plain["sku"] = "DIFFERENT"
        with self.assertRaises(backend.HTTPException) as raised:
            backend._normalize_rotate_inventory(
                {"products": [plus, plain]}, backend.Decimal("5.85"), 40, 29.99
            )
        self.assertEqual(raised.exception.status_code, 422)

        anchor = delivered_inventory("No strap", cents=2221, model="SY69JL")["products"][0]
        payload, _changes, _counts = backend._normalize_rotate_inventory(
            {"products": [delivered_inventory("Rotate&Detach +Strap")["products"][0], anchor]},
            backend.Decimal("5.85"), 40, 29.99,
        )
        anchor_after = next(product for product in payload["products"] if product["sku"] == "CASE-1" and any(
            item.get("values") == ["No strap"] for item in product["property_values"]
        ))
        self.assertEqual(anchor_after["offerings"][0]["price"], 22.21)

    def test_rotate_normalization_supports_three_variations(self):
        inventory = delivered_inventory("Rotate&Detach +Strap")
        inventory["products"][0]["property_values"].append({
            "property_id": 3, "property_name": "Finish", "values": ["Matte"]
        })
        inventory["price_on_property"] = [1, 2, 3]
        payload, _changes, counts = backend._normalize_rotate_inventory(
            inventory, backend.Decimal("5.85"), 40, 29.99
        )
        self.assertEqual(len(payload["products"][0]["property_values"]), 3)
        self.assertEqual(counts["count_renamed"], 1)

    def test_rotate_route_requires_fresh_fingerprints_and_verifies_full_inventory(self):
        response = type("Response", (), {"status_code": 200})()
        before = delivered_inventory("Rotate&Detach +Strap", cents=3000)
        shipping = {"profile_type": "manual", "shipping_profile_destinations": [{
            "destination_country_iso": "FR",
            "primary_cost": {"amount": 585, "divisor": 100, "currency_code": "EUR"},
        }]}
        listing = {"listing_id": 42, "shop_id": 7, "title": "Case", "state": "active"}
        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[
                 (response, listing),
                 (response, before),
                 (response, {"results": [{"listing_id": 42, "shipping_profile": shipping}]}),
             ]):
            preview = backend.normalize_rotate_detach_listing(
                42, False, 40, 29.99, "manual verified"
            )
        self.assertTrue(preview["affected"])
        self.assertFalse(preview["applied"])

        expected, _changes, _counts = backend._normalize_rotate_inventory(
            before, backend.Decimal("5.85"), 40, 29.99
        )
        verified = json.loads(json.dumps(expected))
        verified["products"][0]["property_values"][1]["value_ids"] = [987654]
        verified["products"][0]["offerings"][0]["price"] = {
            "amount": 4023, "divisor": 100, "currency_code": "EUR"
        }
        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[
                 (response, listing),
                 (response, before),
                 (response, {"results": [{"listing_id": 42, "shipping_profile": shipping}]}),
                 (response, {}),
                 (response, verified),
             ]) as request:
            result = backend.normalize_rotate_detach_listing(
                42, True, 40, 29.99, "manual verified",
                preview["inventory_sha256"], preview["pricing_context_sha256"],
            )
        self.assertTrue(result["applied"])
        self.assertTrue(result["verified"])
        self.assertEqual(request.call_args_list[3].args[:2], ("PUT", "/listings/42/inventory"))
        self.assertEqual(
            request.call_args_list[3].kwargs["json_data"]["products"][0]["property_values"][1]["values"],
            ["Rotate&Detach"],
        )

        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[
                 (response, listing), (response, before),
             ]) as request:
            with self.assertRaises(backend.HTTPException) as raised:
                backend.normalize_rotate_detach_listing(
                    42, True, 40, 29.99, "manual verified",
                    "stale", preview["pricing_context_sha256"],
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(request.call_count, 2)

    def test_delivered_apply_rejects_changed_pricing_context(self):
        response = type("Response", (), {"status_code": 200})()
        inventory = delivered_inventory("Clear Back No Strap", cents=2500)
        shipping = {"profile_type": "manual", "shipping_profile_destinations": [{
            "destination_country_iso": "FR",
            "primary_cost": {"amount": 600, "divisor": 100, "currency_code": "EUR"},
        }]}
        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[
                 (response, {"listing_id": 42, "shop_id": 7}),
                 (response, inventory),
                 (response, {"results": [{"listing_id": 42, "shipping_profile": shipping}]}),
             ]) as request:
            with self.assertRaises(backend.HTTPException) as raised:
                backend.listing_price_adjustment(
                    42, "0", True, "full", "inventory-hash", "delivered_targets", 40,
                    json.dumps(DELIVERED_CONFIG), json.dumps({"0:0": 15}),
                    "stale-context", "saisie manuelle vérifiée",
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(request.call_count, 3)

    def test_delivered_route_never_assumes_a_promotion(self):
        response = self.client.post(
            "/listing-prices/42/adjust",
            headers=API_HEADERS,
            params={
                "amount": 0,
                "pricing_profile": "delivered_targets",
                "delivered_targets_json": json.dumps(DELIVERED_CONFIG),
                "promotion_source": "manual verified",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("promotion", response.json()["detail"].lower())

    def test_price_apply_uses_complete_json_payload_and_verifies(self):
        response = type("Response", (), {"status_code": 200})()
        before = {
            "products": [{
                "sku": "CASE-1",
                "property_values": [],
                "offerings": [{
                    "quantity": 5,
                    "is_enabled": True,
                    "price": {"amount": 1000, "divisor": 100, "currency_code": "EUR"},
                }],
            }],
            "price_on_property": [],
            "quantity_on_property": [],
            "sku_on_property": [],
        }
        after = {
            **before,
            "products": [{
                **before["products"][0],
                "offerings": [{
                    **before["products"][0]["offerings"][0],
                    "price": {"amount": 1100, "divisor": 100, "currency_code": "EUR"},
                }],
            }],
        }
        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[
                 (response, {"listing_id": 42, "shop_id": 7, "title": "Case"}),
                 (response, before),
                 (response, {"products": []}),
                 (response, after),
             ]) as request:
            result = backend.listing_price_adjustment(42, "1.00", True)
        self.assertTrue(result["applied"])
        self.assertTrue(result["verified"])
        update_call = request.call_args_list[2]
        self.assertEqual(update_call.args[:2], ("PUT", "/listings/42/inventory"))
        self.assertEqual(update_call.kwargs["json_data"]["products"][0]["offerings"][0]["price"], 11.0)

    def test_price_apply_rejects_inventory_changed_since_preview(self):
        response = type("Response", (), {"status_code": 200})()
        inventory = {
            "products": [{
                "sku": "CASE-1",
                "property_values": [],
                "offerings": [{
                    "quantity": 1,
                    "is_enabled": True,
                    "price": {"amount": 1000, "divisor": 100},
                }],
            }],
        }
        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[
                 (response, {"listing_id": 42, "shop_id": 7}),
                 (response, inventory),
             ]) as request:
            with self.assertRaises(backend.HTTPException) as raised:
                backend.listing_price_adjustment(
                    42, "1.00", True, "full", "stale-fingerprint"
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(request.call_count, 2)

    def test_price_catalog_returns_all_active_listing_pages(self):
        response = type("Response", (), {"status_code": 200})()
        first_page = {
            "results": [
                {"listing_id": index, "title": f"Listing {index}", "state": "active"}
                for index in range(100)
            ]
        }
        second_page = {
            "results": [{"listing_id": 100, "title": "Last", "state": "active"}]
        }
        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[
                 (response, first_page),
                 (response, second_page),
                 (response, {"results": []}),
                 (response, {"results": [{
                     "listing_id": 100,
                     "shipping_profile": {"shipping_profile_id": 9},
                 }]}),
             ]) as request:
            result = backend.listing_price_catalog()
        self.assertEqual(result["count"], 101)
        self.assertEqual(request.call_args_list[1].kwargs["params"]["offset"], 100)
        self.assertEqual(result["listings"][100]["shipping_profile"]["shipping_profile_id"], 9)

    def test_restore_inventory_rewrites_and_verifies_saved_prices(self):
        response = type("Response", (), {"status_code": 200})()
        backup = {
            "products": [{
                "sku": "CASE-1",
                "property_values": [],
                "offerings": [{
                    "quantity": 2,
                    "is_enabled": True,
                    "price": 12.5,
                }],
            }],
        }
        verified = {
            "products": [{
                **backup["products"][0],
                "offerings": [{
                    **backup["products"][0]["offerings"][0],
                    "price": {"amount": 1250, "divisor": 100},
                }],
            }],
        }
        with patch.object(backend, "get_shop_id_for_user", return_value=7), \
             patch.object(backend, "etsy_request", side_effect=[
                 (response, {"listing_id": 42, "shop_id": 7}),
                 (response, {}),
                 (response, verified),
             ]) as request:
            result = backend.restore_listing_inventory(42, backup)
        self.assertTrue(result["verified"])
        self.assertEqual(request.call_args_list[1].args[:2], ("PUT", "/listings/42/inventory"))

    def test_oauth_rejects_invalid_state_without_token_exchange(self):
        backend.STATE.update({"code_verifier": "verifier", "oauth_state": "expected", "oauth_started_at": __import__("time").time()})
        with patch.object(backend.ETSY_SESSION, "post") as post:
            response = self.client.get("/callback?code=code&state=wrong")
        self.assertEqual(response.status_code, 400)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
