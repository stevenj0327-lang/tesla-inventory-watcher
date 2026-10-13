"""Synthetic fixtures: these are not representations of available vehicles."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tracker.inventory import InventoryError, collect_pages, match_vehicle, parse_page, query
from tracker.state import State, idempotency_key
from tracker.worker import cycle, flush, send_email


def vehicle(model="my"):
    names = {"MODEL": "Model Y" if model == "my" else "Cybertruck",
             "TRIM": "Premium Rear-Wheel Drive" if model == "my" else "Premium All-Wheel Drive",
             "PAINT": "Stealth Grey" if model == "my" else "Stainless Steel",
             "WHEELS": '19" Crossflow Wheels' if model == "my" else '20" Core Wheels',
             "INTERIOR": "All Black Interior" if model == "my" else "Tactical Grey Interior",
             "REAR_SEATS": "Five Seat Interior"}
    return {"VIN": "SYNTHETIC_TEST_0001", "Model": model, "TrimName": names["TRIM"],
            "IsDemo": False, "PurchasePrice": 45990, "City": "Pomona", "StateProvince": "CA",
            "OptionCodeList": ",".join("$" + key for key in names),
            "OptionCodeData": [{"code": "$" + key, "group": key, "name": val} for key, val in names.items()],
            "OptionCodePricing": [{"code": "$" + key, "group": key, "price": 45990 if key == "MODEL" else 0}
                                  for key in names]}


def match():
    result = match_vehicle(vehicle(), "my")
    result["area"] = "Deliverable to 91766"
    return {result["vin"]: result}


class MatchingTests(unittest.TestCase):
    def test_both_requested_configurations(self):
        for model in ("my", "ct"):
            self.assertIsNotNone(match_vehicle(vehicle(model), model))

    def test_wrong_trim_not_accepted(self):
        for trim in ("Rear-Wheel Drive", "Premium All-Wheel Drive", "Performance All-Wheel Drive"):
            row = vehicle()
            row["TrimName"] = trim
            self.assertIsNone(match_vehicle(row, "my"))

    def test_black_paint_surcharge_allowed(self):
        row = vehicle()
        row["OptionCodeData"][2]["name"] = "Diamond Black"
        row["OptionCodePricing"][2]["price"] = 1500
        self.assertIsNotNone(match_vehicle(row, "my"))

    def test_red_white_seats_and_wheels_rejected(self):
        for idx, name in ((2, "Ultra Red"), (3, '20" Helix Wheels'), (4, "Black and White Interior"), (5, "Seven Seat Interior")):
            row = vehicle()
            row["OptionCodeData"][idx]["name"] = name
            self.assertIsNone(match_vehicle(row, "my"))

    def test_paid_upgrades_rejected(self):
        for group in ("AUTOPILOT_PACKAGE", "TOWING", "ACCESSORIES", "WHEELS", "UNKNOWN_NEW_UPGRADE"):
            row = vehicle()
            row["OptionCodePricing"].append({"code": "$EXTRA", "group": group, "price": 100})
            self.assertIsNone(match_vehicle(row, "my"))

    def test_missing_or_unpriced_equipment_is_unknown(self):
        for key in ("OptionCodePricing", "OptionCodeData", "IsDemo", "OptionCodeList", "PurchasePrice"):
            row = vehicle()
            del row[key]
            with self.assertRaises(InventoryError):
                match_vehicle(row, "my")
        row = vehicle()
        row["OptionCodeList"] += ",$UNKNOWN"
        with self.assertRaises(InventoryError):
            match_vehicle(row, "my")

    def test_demos_and_used_excluded(self):
        for key in ("IsDemo", "IsUsed"):
            row = vehicle()
            row[key] = True
            self.assertIsNone(match_vehicle(row, "my"))

    def test_invalid_price_unknown(self):
        for value in (None, True, float("nan"), -10):
            row = vehicle()
            row["PurchasePrice"] = value
            with self.assertRaises(InventoryError):
                match_vehicle(row, "my")

    def test_exact_empty_response_observed_live(self):
        self.assertEqual(parse_page({"results": {"exact": [], "approximate": [], "approximateOutside": []},
                                     "total_matches_found": 0}), ([], 0))

    def test_partial_malformed_or_challenge_not_empty(self):
        for payload in ({}, "Access Denied", {"results": [], "total_matches_found": 1},
                        {"results": [], "total_matches_found": None}):
            with self.assertRaises(InventoryError):
                parse_page(payload)

    def test_approximate_vehicles_ignored(self):
        payload = {"results": {"exact": [], "approximateOutside": [vehicle()]}, "total_matches_found": 0}
        self.assertEqual(parse_page(payload), ([], 0))

    def test_queries_have_no_price_limits_and_correct_location(self):
        for radius in (0, 200):
            q = query("ct", radius)["query"]
            self.assertEqual(q["range"], radius)
            self.assertEqual(q["zip"], "91766")
            self.assertNotIn("paymentRange", q)
            self.assertEqual(q["options"], {})

    def test_pagination_and_duplicate_detection(self):
        rows = []
        for i in range(51):
            row = vehicle()
            row["VIN"] = "SYNTHETIC_%08d" % i
            rows.append(row)
        def fetch(q):
            return {"results": rows[q["offset"]:q["offset"] + 50], "total_matches_found": 51}
        self.assertEqual(len(collect_pages(fetch, "my", 0)), 51)
        rows[50] = copy.deepcopy(rows[0])
        with self.assertRaises(InventoryError):
            collect_pages(fetch, "my", 0)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"
        self.state = State(self.path)

    def tearDown(self):
        self.state.db.close()
        self.tmp.cleanup()

    def test_first_match_once_across_restarts(self):
        self.state.success(match(), now=100)
        self.state.success(match(), now=200)
        self.assertEqual(len(self.state.pending(now=200)), 1)
        self.state.db.close()
        self.state = State(self.path)
        self.state.success(match(), now=300)
        self.assertEqual(len(self.state.pending(now=300)), 1)

    def test_reappearance_requires_two_confirmed_absences(self):
        self.state.success(match(), now=100)
        first = self.state.pending(now=100)[0]
        self.state.delivered(first["id"], "receipt-test")
        self.state.success({}, now=200)
        self.state.success(match(), now=300)
        self.assertEqual(len(self.state.pending(now=300)), 0)
        self.state.success({}, now=400)
        self.state.success({}, now=500)
        self.state.success(match(), now=600)
        self.assertEqual(len(self.state.pending(now=600)), 1)

    def test_error_preserves_previous_availability(self):
        self.state.success(match(), now=100)
        self.state.failure("HTTP 403", now=200)
        self.assertEqual(self.state.get("last_success"), 100)
        self.assertEqual(self.state.db.execute("SELECT active FROM vehicles").fetchone()[0], 1)
        self.assertEqual(len(self.state.pending(include_matches=False, now=200)), 0)

    def test_outage_and_recovery_never_send_email(self):
        for i in range(5):
            self.state.failure("HTTP 403", now=100 + i)
        self.assertEqual(len(self.state.pending(now=200)), 0)
        self.state.success({}, now=300)
        self.assertEqual(len(self.state.pending(now=300)), 0)

    def test_old_system_notifications_cancelled_on_upgrade(self):
        with self.state.db:
            self.state.enqueue("outage:old", "Old outage", "Do not send", 100)
        self.state.db.close()
        self.state = State(self.path)
        self.assertEqual(self.state.db.execute("SELECT status FROM outbox").fetchone()[0], "cancelled")

    def test_nonmatch_email_rejected_before_network(self):
        with patch("tracker.worker.urlopen") as network:
            with self.assertRaises(RuntimeError):
                send_email({"vin": None})
            network.assert_not_called()

    def test_stale_pending_match_cancelled(self):
        self.state.success(match(), now=100)
        self.state.success({}, now=200)
        self.assertEqual(len(self.state.pending(now=200)), 0)
        self.state.success(match(), now=300)
        self.assertEqual(len(self.state.pending(now=300)), 1)

    def test_retry_key_stable_and_window_bounded(self):
        self.state.success(match(), now=100)
        key = self.state.pending(now=100)[0]["id"]
        self.assertTrue(self.state.attempt(key, now=100))
        self.state.mail_failed(key, "timeout", now=100)
        self.assertEqual(len(self.state.pending(now=120)), 0)
        self.assertEqual(len(self.state.pending(now=200)), 1)
        self.assertFalse(self.state.attempt(key, now=100 + 24 * 3600))
        self.assertEqual(len(self.state.pending(now=100 + 24 * 3600)), 0)
        self.assertEqual(idempotency_key(key), idempotency_key(key))

    def test_provider_failure_leaves_email_pending(self):
        self.state.success(match())
        with patch("tracker.worker.send_email", side_effect=RuntimeError("temporary")):
            flush(self.state, True)
        row = self.state.db.execute("SELECT status FROM outbox").fetchone()
        self.assertEqual(row[0], "pending")

    def test_complete_cycle_with_fake_source_and_fake_email(self):
        class Source:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def scan(self): return match()
        with patch("tracker.worker.send_email", return_value="test-receipt") as sender:
            self.assertTrue(cycle(self.state, Source))
            self.assertTrue(cycle(self.state, Source))
            self.assertEqual(sender.call_count, 1)

    def test_failed_cycle_does_not_email_unverified_pending_match(self):
        self.state.success(match())
        class Source:
            def __enter__(self): raise InventoryError("blocked")
            def __exit__(self, *args): pass
        with patch("tracker.worker.send_email") as sender:
            self.assertFalse(cycle(self.state, Source))
            sender.assert_not_called()

    def test_dry_run_does_not_change_state_or_send_email(self):
        class Source:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def scan(self): return {}
        with patch("tracker.worker.send_email") as sender, patch("builtins.print"):
            self.assertTrue(cycle(None, Source, dry_run=True))
            sender.assert_not_called()

    def test_email_payload_and_idempotency_without_network(self):
        self.state.success(match())
        event = self.state.pending()[0]
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"id":"synthetic-receipt"}'
        with patch.dict("os.environ", {"MAIL_FROM":"test@example.com", "ALERT_TO":"recipient@example.com",
                                       "RESEND_API_KEY":"test-only-not-a-key"}):
            with patch("tracker.worker.urlopen", return_value=Response()) as network:
                self.assertEqual(send_email(event), "synthetic-receipt")
                request = network.call_args.args[0]
                self.assertEqual(json.loads(request.data)["to"], ["recipient@example.com"])
                self.assertEqual(request.get_header("Idempotency-key"), idempotency_key(event["id"]))


if __name__ == "__main__":
    unittest.main()
