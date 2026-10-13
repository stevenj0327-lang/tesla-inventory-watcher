"""Ensure failed page loads never masquerade as empty inventory."""
import unittest
from unittest.mock import MagicMock
from tracker.inventory import InventoryError, load_inventory_page

class PageInitializationTests(unittest.TestCase):
    def page(self, document_status=200, api_status=200):
        page = MagicMock()
        page.goto.return_value.status = document_status
        response = page.expect_response.return_value.__enter__.return_value.value
        response.status = api_status
        response.json.return_value = {"results": [], "total_matches_found": 0}
        return page

    def test_document_denial_stops_before_custom_query(self):
        with self.assertRaisesRegex(InventoryError, "page HTTP 403"):
            load_inventory_page(self.page(document_status=403))

    def test_website_request_denial_is_explicit(self):
        with self.assertRaisesRegex(InventoryError, "own inventory request HTTP 403"):
            load_inventory_page(self.page(api_status=403))

    def test_success_is_readiness_not_inventory_baseline(self):
        self.assertIsNone(load_inventory_page(self.page()))

    def test_html_challenge_cannot_count_as_readiness(self):
        page = self.page()
        page.expect_response.return_value.__enter__.return_value.value.json.return_value = {}
        with self.assertRaises(InventoryError):
            load_inventory_page(page)
