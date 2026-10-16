import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ETSY_API_KEY", "etsy-client")
os.environ.setdefault("ETSY_SECRET", "etsy-secret")
os.environ.setdefault("ETSY_REFRESH_TOKEN", "refresh-token")
os.environ.setdefault("REDIRECT_URI", "https://example.test/callback")
os.environ.setdefault("ORDERIFY_API_KEY", "orderify-test-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import main as backend


API_HEADERS = {"X-Orderify-Key": "orderify-test-key"}


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

    def test_oauth_rejects_invalid_state_without_token_exchange(self):
        backend.STATE.update({"code_verifier": "verifier", "oauth_state": "expected", "oauth_started_at": __import__("time").time()})
        with patch.object(backend.ETSY_SESSION, "post") as post:
            response = self.client.get("/callback?code=code&state=wrong")
        self.assertEqual(response.status_code, 400)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
