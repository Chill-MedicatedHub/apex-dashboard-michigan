"""
LeafLink Sales Report Scraper
-----------------------------
Pulls "Orders Received" from the LeafLink Marketplace V2 API, flattens each
order into per-line-item rows, keeps only the configured brand (default
"Chill Medicated"), trims each row to the fields the dashboard needs, and
saves a compact sales_data.json.

Why the trimming matters: pulling orders with include_children=line_items
returns the full nested product object for every line. Saved raw, that's
hundreds of MB and GitHub rejects files over 100 MB. We keep only scalar
fields we actually use, so the output stays small.

AUTH: an Application API key (Settings > Applications). Header is
      Authorization: App <key>   (single space after "App").
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
load_dotenv()

# App tokens use the Marketplace V2 API on the main domain under /api/v2/.
# (api.leaflink.com is a DIFFERENT, JWT-based API — not for App tokens.)
# Sandbox equivalent: https://sandbox.leaflink.com
API_BASE = os.getenv("LEAFLINK_API_BASE", "https://www.leaflink.com")
ENDPOINT = os.getenv("LEAFLINK_ENDPOINT", "/api/v2/orders-received/")

# Application API key. NEVER commit this. App tokens are scoped to the single
# company they were created in — so this should already be Medfarms - 100 Shafer
# Processing if that's where you made the key. The scraper prints the seller
# name(s) it sees so you can confirm.
API_KEY = os.getenv("LEAFLINK_API_KEY", "")

# Keep only this brand (case-insensitive substring match). Blank = keep all.
BRAND_FILTER = os.getenv("LEAFLINK_BRAND", "Chill Medicated")

# Pull the line items nested in each order.
INCLUDE_CHILDREN = os.getenv("LEAFLINK_INCLUDE_CHILDREN", "line_items")

# Keep only orders on/after this date (matches order date). Blank = no floor.
FROM_DATE = os.getenv("LEAFLINK_FROM_DATE", "2025-05-01")

# Optional server-side payload reducer (documented filter). Off by default to
# avoid an unverified param failing the run; the client-side FROM_DATE floor is
# what guarantees correctness. Set e.g. "2025-05-01" once confirmed to speed pulls.
MODIFIED_AFTER = os.getenv("LEAFLINK_MODIFIED_AFTER", "")

# Page size 1..500 (anything else 404s); LeafLink defaults to 50.
PAGE_SIZE = int(os.getenv("LEAFLINK_PAGE_SIZE", "500"))

# Safety cap on pages (0 = no cap).
MAX_PAGES = int(os.getenv("LEAFLINK_MAX_PAGES", "0"))

OUTPUT_FILE = Path(__file__).parent / "sales_data.json"
SAMPLE_FILE = Path(__file__).parent / "sample_raw_order.json"


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def auth_headers() -> dict:
    return {
        "Authorization": f"App {API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "chill-sales-dashboard",
    }


def _handle_status(resp: requests.Response) -> None:
    if resp.status_code == 401:
        print("ERROR: 401 Unauthorized — key missing/wrong/revoked.")
        sys.exit(1)
    if resp.status_code == 403:
        print("ERROR: 403 Forbidden — the app lacks read permission for Orders.")
        sys.exit(1)
    if resp.status_code == 404:
        print(f"ERROR: 404 Not Found for {resp.url}")
        print("Check the host/path. App tokens use www.leaflink.com/api/v2/...")
        sys.exit(1)
    if resp.status_code == 429:
        return
    if resp.status_code != 200:
        print(f"ERROR: unexpected status {resp.status_code}")
        print(resp.text[:500])
        sys.exit(1)


def fetch_all() -> list:
    if not API_KEY:
        print("ERROR: LEAFLINK_API_KEY is empty.")
        sys.exit(1)

    url = f"{API_BASE}{ENDPOINT}"
    params = {"page_size": PAGE_SIZE, "page": 1}
    if INCLUDE_CHILDREN:
        params["include_children"] = INCLUDE_CHILDREN
    if MODIFIED_AFTER:
        params["modified__gte"] = MODIFIED_AFTER

    orders = []
    page = 0
    while url:
        for attempt in range(4):
            resp = requests.get(
                url, headers=auth_headers(),
                params=params if page == 0 else None, timeout=120,
            )
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  rate limited (429) — backing off {wait}s")
                time.sleep(wait)
                continue
            break
        _handle_status(resp)

        data = resp.json()
        batch = data.get("results", data if isinstance(data, list) else [])
        orders.extend(batch)
        page += 1
        total = data.get("count", "?")
        print(f"  page {page}: +{len(batch)} orders (running {len(orders)} / {total})")

        url = data.get("next") if isinstance(data, dict) else None
        if MAX_PAGES and page >= MAX_PAGES:
            print(f"  reached MAX_PAGES={MAX_PAGES}, stopping early")
            break
    return orders


# ----------------------------------------------------------------------------
# Flatten + filter + slim
# ----------------------------------------------------------------------------
def _first(d: dict, *keys):
    """Return the first present, non-empty value among keys."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", []):
            return d.get(k)
    return None


def _amount(v):
    """Pull a number out of a value or a {'amount': ...} money object."""
    if isinstance(v, dict):
        v = v.get("amount")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _name_of(v):
    """A brand/category/product/rep may be a string, id, object, or list of them."""
    if isinstance(v, list):
        parts = [_name_of(x) for x in v]
        return ", ".join(p for p in parts if p)
    if isinstance(v, dict):
        return _first(v, "name", "title", "display_name", "full_name") or ""
    if isinstance(v, str):
        return v
    return ""


def _date_key(s):
    """Return leading YYYY-MM-DD from an ISO date/datetime string, else None.
    ISO dates compare correctly as plain strings, so we use that for the floor."""
    if not s:
        return None
    s = str(s)
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def _buyer(order: dict) -> dict:
    cust = order.get("customer") or order.get("buyer") or {}
    if not isinstance(cust, dict):
        cust = {}
    name = _first(order, "customer_name", "buyer_name") or _first(
        cust, "name", "company_name", "display_name"
    ) or ""
    state = _first(cust, "state", "region") or _first(order, "buyer_state") or ""
    license_ = _first(cust, "license_number", "license") or _first(order, "buyer_license") or ""
    return {"name": name, "state": state, "license": license_}


def _line_brand(li: dict) -> str:
    prod = li.get("product") if isinstance(li.get("product"), dict) else {}
    cand = (
        _name_of(li.get("brand"))
        or _name_of(li.get("product_brand"))
        or _name_of(prod.get("brand"))
        or _name_of(prod.get("brand_name"))
        or _first(prod, "brand_name") or ""
    )
    return cand or ""


def flatten(orders: list, brand_q: str, from_date: str = ""):
    rows = []
    sellers = set()
    matched, total_lines = 0, 0
    skipped_old = 0
    brand_q = (brand_q or "").strip().lower()
    from_date = (from_date or "").strip()

    for o in orders:
        seller = _name_of(o.get("seller")) or _first(o, "seller_name") or ""
        if seller:
            sellers.add(seller)

        order_date = _first(o, "created_on", "created", "order_placed_date", "date")
        # Date floor: skip whole order if its date is known and before from_date.
        if from_date:
            dk = _date_key(order_date)
            if dk and dk < from_date:
                skipped_old += 1
                continue

        buyer = _buyer(o)
        order_common = {
            "order_number": _first(o, "number", "short_id", "id"),
            "order_status": _first(o, "status", "order_status"),
            "order_date": order_date,
            "modified": o.get("modified"),
            "delivery_date": _first(o, "delivery_date", "shipping_date", "ship_date"),
            "buyer_name": buyer["name"],
            "buyer_state": buyer["state"],
            "buyer_license": buyer["license"],
            "sales_rep": _name_of(_first(o, "sales_rep", "sales_reps")),
        }

        line_items = o.get("line_items") or o.get("lineitems") or []
        for li in line_items:
            if not isinstance(li, dict):
                continue
            total_lines += 1
            brand = _line_brand(li)
            if brand_q and brand_q not in brand.lower():
                continue
            matched += 1
            prod = li.get("product") if isinstance(li.get("product"), dict) else {}
            unit_price = _amount(_first(li, "ordered_unit_price", "unit_price", "wholesale_price"))
            sale_price = _amount(li.get("sale_price"))
            qty = _amount(li.get("quantity")) or 0
            line_total = _amount(_first(li, "total", "line_total"))
            if line_total is None:
                eff = sale_price if (sale_price or 0) > 0 else unit_price
                line_total = (eff or 0) * qty
            rows.append({
                **order_common,
                "brand": brand,
                "product_name": _first(li, "product_name") or _name_of(prod) or "",
                "product_category": _name_of(_first(li, "category", "product_category")) or _name_of(prod.get("category")),
                "product_type": _name_of(_first(li, "product_type", "type")) or _name_of(prod.get("category_type")),
                "quantity": qty,
                "unit_price": unit_price,
                "sale_price": sale_price,
                "line_total": line_total,
            })

    return rows, sellers, matched, total_lines, skipped_old


# ----------------------------------------------------------------------------
def main():
    print(f"Pulling {ENDPOINT} from {API_BASE}"
          + (f"  (modified__gte={MODIFIED_AFTER})" if MODIFIED_AFTER else ""))
    orders = fetch_all()
    print(f"Fetched {len(orders)} orders.")

    # Save the first raw order (tiny) so field names can be verified/locked.
    if orders:
        SAMPLE_FILE.write_text(json.dumps(orders[0], indent=2, default=str))
        first = orders[0]
        print(f"First order keys: {sorted(first.keys())}")
        # Print order-level fields WITHOUT line_items (keeps the log small),
        # plus the first line item — enough to lock the dashboard mapping.
        order_lite = {k: v for k, v in first.items() if k not in ("line_items", "lineitems")}
        print("\n--- FIRST ORDER (line_items removed) ---")
        print(json.dumps(order_lite, default=str)[:3000])
        lis = first.get("line_items") or first.get("lineitems") or []
        if lis and isinstance(lis[0], dict):
            print("\n--- FIRST LINE ITEM ---")
            print(json.dumps(lis[0], default=str)[:3000])
        print("--- end sample ---\n")

    rows, sellers, matched, total_lines, skipped_old = flatten(orders, BRAND_FILTER, FROM_DATE)

    # Safety net: if a brand filter was set but matched nothing while lines exist,
    # keep everything and warn — better a working dashboard than an empty one.
    if BRAND_FILTER.strip() and matched == 0 and total_lines > 0:
        print(f"WARNING: brand filter '{BRAND_FILTER}' matched 0 of {total_lines} "
              "lines — the brand field wasn't where expected.")
        print("Keeping ALL rows so you still get data. Paste sample_raw_order.json")
        print("and the brand path can be locked exactly.")
        rows, sellers, matched, total_lines, skipped_old = flatten(orders, "", FROM_DATE)

    print(f"\nSeller/company in data: {sorted(sellers) or '(none found on orders)'}")
    print(f"Date floor: {FROM_DATE or '(none)'}  ->  skipped {skipped_old} older orders")
    print(f"Brand filter: '{BRAND_FILTER or '(none)'}'")
    print(f"Line items: {total_lines} in range -> {matched} kept after brand filter")

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "leaflink",
        "company": sorted(sellers),
        "brand_filter": BRAND_FILTER,
        "from_date": FROM_DATE,
        "order_count": len(orders),
        "row_count": len(rows),
        "rows": rows,
    }
    # Compact (no indent) keeps the file small.
    OUTPUT_FILE.write_text(json.dumps(payload, separators=(",", ":"), default=str))
    size_mb = OUTPUT_FILE.stat().st_size / 1e6
    print(f"Saved -> {OUTPUT_FILE} ({size_mb:.2f} MB, {len(rows)} rows)")


if __name__ == "__main__":
    main()
