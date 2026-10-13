"""Public inventory adapter. No login, proxy rotation, or challenge bypass."""
import json
import logging
import math
import re
import time
from urllib.parse import quote, urlencode


class InventoryError(RuntimeError):
    pass


def query(model, radius, offset=0):
    # Location coordinates and query shape observed in Tesla's own website.
    return {
        "query": {
            "model": model, "condition": "new", "options": {},
            "arrangeby": "Price", "order": "asc", "market": "US",
            "language": "en", "super_region": "north america",
            "PaymentType": "cash", "zip": "91766", "region": "CA",
            "lat": 34.0397162, "lng": -117.7549295, "range": radius,
        },
        "offset": offset, "count": 50, "outsideOffset": 0,
        "outsideSearch": False, "isFalconDeliverySelectionEnabled": True,
        "version": "v2",
    }


def parse_page(payload):
    if not isinstance(payload, dict) or "results" not in payload:
        raise InventoryError("Inventory response schema changed")
    total = payload.get("total_matches_found")
    if isinstance(total, str) and total.isdigit():
        total = int(total)
    if type(total) is not int or total < 0:
        raise InventoryError("Inventory response lacks a valid exact-match count")
    rows = payload["results"]
    if isinstance(rows, dict):
        # Similar and outside-area suggestions are not qualifying inventory.
        rows = rows.get("exact")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise InventoryError("Inventory results are not a vehicle list")
    if len(rows) > total or (total and not rows):
        raise InventoryError("Inventory results are incomplete")
    return rows, total


def collect_pages(fetch, model, radius):
    rows, seen, total = [], set(), None
    for offset in range(0, 1000, 50):
        page, current_total = parse_page(fetch(query(model, radius, offset)))
        if total is not None and current_total != total:
            raise InventoryError("Inventory changed during pagination; retry next cycle")
        total = current_total
        for row in page:
            vin = row.get("VIN")
            if not isinstance(vin, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", vin):
                raise InventoryError("Inventory vehicle is missing its identifier")
            if vin in seen:
                raise InventoryError("Inventory pagination repeated a vehicle")
            seen.add(vin)
            rows.append(row)
        if len(rows) == total:
            return rows
        if len(rows) > total or len(page) != 50:
            raise InventoryError("Inventory pagination was truncated")
    raise InventoryError("Inventory exceeds the 1,000-vehicle safety limit")


def load_inventory_page(page):
    """Wait for the site's own inventory request before issuing filtered queries."""
    responses = []
    def is_inventory(response):
        return "/inventory/api/" in response.url and "/inventory-results" in response.url
    def observe(response):
        if is_inventory(response):
            responses.append(response)
    page.on("response", observe)
    try:
        document = page.goto(
            "https://www.tesla.com/inventory/new/my?zip=91766&range=200",
            wait_until="domcontentloaded", timeout=30000)
        status = document.status if document else None
        logging.info("Tesla inventory page HTTP %s", status)
        if status != 200:
            raise InventoryError("Tesla inventory page HTTP %s; page did not load" % status)
        if responses:
            response = responses[0]
        else:
            with page.expect_response(is_inventory, timeout=30000) as initial:
                pass
            response = initial.value
    finally:
        page.remove_listener("response", observe)
    logging.info("Tesla website's own inventory request HTTP %s", response.status)
    if response.status != 200:
        raise InventoryError("Tesla website's own inventory request HTTP %s" % response.status)
    # This initial response may have default price/location filters. It is only
    # a readiness signal, never the matching inventory or a successful scan.
    parse_page(response.json())


class BrowserInventory:
    """Fetch the site's same-origin JSON in a normal Chromium session."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.launch(headless=True, chromium_sandbox=True)
            self.context = self.browser.new_context(locale="en-US")
            self.page = self.context.new_page()
            self.page.set_default_timeout(20000)
            load_inventory_page(self.page)
            return self
        except Exception as error:
            try:
                self.pw.stop()
            except Exception:
                logging.warning("Browser cleanup failed after initialization error")
            if isinstance(error, InventoryError):
                raise
            raise InventoryError("Inventory page initialization failed (%s)" % type(error).__name__) from None

    def __exit__(self, *args):
        self.context.close()
        self.browser.close()
        self.pw.stop()

    def fetch(self, request):
        url = "/inventory/api/v4/inventory-results?" + urlencode({"query": json.dumps(request)})
        try:
            result = self.page.evaluate("""async (url) => {
                const response = await fetch(url, {
                    credentials: 'same-origin', cache: 'no-store',
                    signal: AbortSignal.timeout(20000),
                    headers: {Accept: 'application/json'}
                });
                const body = await response.text();
                return {status: response.status, body};
            }""", url)
        except Exception:
            raise InventoryError("Tesla inventory request timed out or failed") from None
        if result["status"] != 200:
            raise InventoryError("Tesla inventory HTTP %s; no bypass attempted" % result["status"])
        try:
            return json.loads(result["body"])
        except (ValueError, TypeError):
            raise InventoryError("Tesla returned a challenge or non-JSON response") from None

    def scan(self):
        found = {}
        started = time.monotonic()
        for model in ("my", "ct"):
            for radius in (200, 0):
                if time.monotonic() - started > 90:
                    raise InventoryError("Inventory scan exceeded its time budget")
                for row in collect_pages(self.fetch, model, radius):
                    vehicle = match_vehicle(row, model)
                    if vehicle:
                        vehicle["area"] = ("Deliverable to 91766" if radius == 0
                                           else "Within Tesla's 200-mile search of 91766")
                        found[vehicle["vin"]] = vehicle
        return found


def text(value):
    return re.sub(r"\s+", " ", str(value or "").lower().replace("–", "-").replace("‑", "-")).strip()


def match_vehicle(row, model):
    """Return a confirmed configuration or None. Ambiguous candidates fail closed."""
    if row.get("Model") != model:
        raise InventoryError("Unexpected model in exact inventory results")
    expected = {"my": "premium rear-wheel drive", "ct": "premium all-wheel drive"}[model]
    options = row.get("OptionCodeData")
    groups = {}
    if isinstance(options, list):
        for option in options:
            if isinstance(option, dict):
                groups.setdefault(option.get("group"), []).append(option)
    trim = text(row.get("TrimName"))
    if not trim:
        trim = text(next((o.get("name") for o in groups.get("TRIM", [])), ""))
    if not trim:
        raise InventoryError("Cannot determine vehicle trim")
    if trim != expected:
        return None
    if row.get("IsDemo") is True:
        return None
    if row.get("IsDemo") is not False:
        raise InventoryError("Candidate has unknown demo status")
    if row.get("IsUsed") is True or text(row.get("Condition")) in ("used", "pre-owned"):
        return None
    if not groups:
        raise InventoryError("Candidate is missing equipment descriptions")

    def group_name(group):
        return " / ".join(str(o.get("name") or o.get("description") or "")
                          for o in groups.get(group, []))

    paint, wheels, interior = (group_name(g) for g in ("PAINT", "WHEELS", "INTERIOR"))
    if model == "my":
        if not paint:
            raise InventoryError("Candidate is missing paint information")
        if not any(word in text(paint) for word in ("black", "grey", "gray")):
            return None
        if not wheels or not interior or not group_name("REAR_SEATS"):
            raise InventoryError("Candidate is missing standard-equipment information")
        if not ("19" in wheels and "crossflow" in text(wheels)):
            return None
        if "black" not in text(interior) or "white" in text(interior):
            return None
        if not re.search(r"\b(five|5)[ -]seat", text(group_name("REAR_SEATS"))):
            return None
    else:
        if not wheels or not interior:
            raise InventoryError("Candidate is missing standard-equipment information")
        if not ("20" in wheels and "core" in text(wheels)):
            return None
        if not any(n in text(interior) for n in ("tactical grey", "tactical gray", "black")):
            return None

    pricing = row.get("OptionCodePricing")
    if not isinstance(pricing, list) or not pricing:
        raise InventoryError("Cannot verify absence of upgrades without option prices")
    priced_codes = set()
    for option in pricing:
        if not isinstance(option, dict):
            raise InventoryError("Invalid equipment pricing")
        price = option.get("price")
        if type(price) not in (int, float) or not math.isfinite(price) or price < 0:
            raise InventoryError("Unknown equipment price")
        priced_codes.add(str(option.get("code", "")).lstrip("$"))
        group = option.get("group")
        allowed = {"MODEL", "TRIM"} | ({"PAINT"} if model == "my" else set())
        if price > 0 and group not in allowed:
            return None
    codes = row.get("OptionCodeList")
    if not isinstance(codes, str) or not codes:
        raise InventoryError("Candidate has no complete equipment list")
    if any(code.strip().lstrip("$") not in priced_codes for code in codes.split(",")):
        raise InventoryError("Candidate has equipment without known pricing")
    vin = row.get("VIN")
    if not isinstance(vin, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", vin):
        raise InventoryError("Candidate is missing its identifier")
    price = row.get("PurchasePrice", row.get("InventoryPrice"))
    if type(price) not in (int, float) or not math.isfinite(price) or price <= 0:
        raise InventoryError("Candidate has no valid cash price")
    return {"vin": vin, "model": "Model Y" if model == "my" else "Cybertruck",
            "trim": row.get("TrimName") or expected, "paint": paint or "Stainless steel",
            "wheels": wheels, "interior": interior, "price": price,
            "location": ", ".join(str(row[k]) for k in ("City", "StateProvince") if row.get(k)),
            "url": "https://www.tesla.com/%s/order/%s?titleStatus=new" % (model, quote(vin, safe=""))}
