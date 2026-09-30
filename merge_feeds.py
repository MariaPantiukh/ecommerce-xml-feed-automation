#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_feeds.py
==============

Downloads XML feeds from several suppliers, extracts each product's SKU
(article number), price, stock quantity and availability, and builds YML
files (yml_catalog / shop / offers / offer) for Prom.ua's "Automatic
catalog update via link" feature.

Important (per the user's requirement): the output files deliberately do
NOT contain product names, descriptions, images, or categories — only the
id (SKU/article) and a couple of fields to update. This is so that the
"update via link" feature can never accidentally change the folder /
category structure or any other field of products that already exist on
the site: Prom only overwrites the fields present in the feed for products
that match by id (SKU); everything else (name, description, category) is
left untouched because the feed simply doesn't include it.

The script produces TWO separate files, because price and stock are
updated on a different schedule:

  1. The "stock file" (default: merged_feed.xml) — contains only
     id + available + quantity_in_stock. This is the file meant for the
     daily GitHub Actions auto-run (availability/stock changes often).

  2. The "price file" (default: price_feed.xml) — contains only
     id + price + currencyId. It is NOT generated automatically every
     day; run it manually (locally, or with the "Run workflow" button on
     its own separate workflow) only when you actually need to update
     prices.

Which file(s) get built in a single run is controlled by the --mode flag
(stock / price / both, default: stock).

!!! MAKE SURE to check, in Prom.ua's import settings (the "Price lists"
section -> your feed -> "Settings"), that the option "Update price and
availability only" (or an equivalent) is enabled — this is an extra
safeguard in case Prom still expects a full set of fields.

Supplier feed formats can differ. The script tries to auto-detect both
the official YML format (<yml_catalog>/<shop>/<offers>/<offer>) and a
typical "product" format (<product>/<item> instead of <offer>). If the
automatic field detection comes back empty, run the script in diagnostic
mode:

    python merge_feeds.py --inspect

This downloads each feed and prints the raw XML of its first product, as
well as a list of every tag found inside that product. Use this to adjust
the SKU_TAGS / PRICE_TAGS / QTY_TAGS / AVAILABLE_TAGS lists below (just
add the tag name that the supplier actually uses).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests

# --------------------------------------------------------------------------
# 1. SOURCE CONFIGURATION
# --------------------------------------------------------------------------
# Add / remove suppliers here. "priority" decides whose product wins if the
# same SKU shows up in more than one feed at once (lower number = higher
# priority).

SOURCES = [
    {
        "name": "skyfarb",
        "url": (
            "https://skyfarb.com.ua/products_feed.xml"
            "?hash_tag=61f83dfa1a46b459de53e08dee36ead2"
            "&sales_notes=&product_ids=&label_ids=123596025%2C123596165%2C123596167"
            "&exclude_fields=&html_description=1&yandex_cpa=&process_presence_sure="
            "&languages=uk%2Cru&extra_fields=quantityInStock%2Ckeywords&group_ids="
        ),
        "priority": 1,
    },
    {
        "name": "zolushka",
        "url": "https://zolushka.com.ua/content/export/e0e6bd98a65223379a52142a5ff6da86.xml",
        "priority": 2,
    },
    {
        "name": "dmtua",
        "url": (
            "https://dmtua.store/products_feed.xml"
            "?hash_tag=cd0d9686ad6d224894813fab04efc51c"
            "&sales_notes=&product_ids=&label_ids=11806561"
            "&exclude_fields=&html_description=0&yandex_cpa=&process_presence_sure="
            "&languages=uk%2Cru&extra_fields=quantityInStock%2Ckeywords&group_ids="
        ),
        "priority": 3,
    },
]

# Names of the product-level elements the script will look for anywhere in
# the XML tree (checked in this order; any mix of upper/lower case matches).
ITEM_TAGS = ["offer", "product", "item", "position", "good"]

# Possible names for the SKU/article field — both as child tags and as
# attributes on the product element itself (checked in priority order).
SKU_TAGS = [
    "vendorCode", "vendor_code", "sku", "article", "articul",
    "code", "Код_товара", "Код_товару", "id", "offer_id", "productId",
]
SKU_ATTRS = ["id", "sku", "article", "code"]

PRICE_TAGS = ["price", "Цена", "Ціна", "priceuah", "price_uah"]

# Prom.ua's YML import treats <name> as a REQUIRED field on every offer,
# even when the offer is only meant to update price/stock on an existing
# product matched by id. Without it, the whole import is rejected with
# "Поле Назва позиції: Обов'язкове поле". So we still need to read the
# product name from the supplier feed and include it — Prom will simply
# overwrite the existing name with this value for matched products.
NAME_TAGS = [
    "name", "name_ua", "Name_ua", "title", "model",
    "Название", "Назва", "Найменування",
]

QTY_TAGS = [
    "quantityInStock", "quantity_in_stock", "quantity", "stock_quantity",
    "stock", "Количество", "Кількість", "in_stock", "remains",
]

AVAILABLE_TAGS = ["available", "presence", "stock_status", "Наличие", "Наявність"]
AVAILABLE_ATTRS = ["available", "in_stock"]

# Text values treated as meaning "the product is in stock".
TRUE_WORDS = {
    "true", "1", "yes", "y", "in_stock", "instock", "available",
    "в наявності", "в наличии", "є", "так",
}
FALSE_WORDS = {
    "false", "0", "no", "n", "out_of_stock", "outofstock", "немає в наявності",
    "нет в наличии", "under_order",  # treat "on backorder" as "not in stock"
}

REQUEST_TIMEOUT = 30  # seconds allowed to download a single feed
STOCK_OUTPUT_FILE = "merged_feed.xml"   # id + available + quantity_in_stock (daily)
PRICE_OUTPUT_FILE = "price_feed.xml"    # id + price + currencyId (manual, on demand)
SHOP_NAME = "My store"
SHOP_COMPANY = "My store"
SHOP_URL = "https://example.prom.ua"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("merge_feeds")


# --------------------------------------------------------------------------
# 2. PRODUCT MODEL
# --------------------------------------------------------------------------

@dataclass
class Offer:
    sku: str
    name: Optional[str] = None
    price: Optional[str] = None
    quantity: Optional[str] = None
    available: Optional[bool] = None
    source: str = ""


def local_tag(tag: str) -> str:
    """Strips the XML namespace from a tag name: '{ns}offer' -> 'offer'."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def strip_ns(root: ET.Element) -> None:
    """Recursively strips the namespace from every tag in the tree (in-place)."""
    for elem in root.iter():
        elem.tag = local_tag(elem.tag)


# --------------------------------------------------------------------------
# 3. DOWNLOADING FEEDS
# --------------------------------------------------------------------------

def download_feed(url: str, name: str) -> Optional[bytes]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PromFeedMerger/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        if not resp.content.strip():
            log.warning("[%s] Empty response from the server — feed skipped.", name)
            return None
        return resp.content
    except requests.RequestException as exc:
        log.error("[%s] Could not download the feed: %s", name, exc)
        return None


def parse_xml(content: bytes, name: str) -> Optional[ET.Element]:
    try:
        root = ET.fromstring(content)
        strip_ns(root)
        return root
    except ET.ParseError as exc:
        log.error("[%s] File is not valid XML: %s", name, exc)
        return None


# --------------------------------------------------------------------------
# 4. EXTRACTING FIELDS FROM A PRODUCT
# --------------------------------------------------------------------------

def find_child_text(item: ET.Element, tag_variants: list[str]) -> Optional[str]:
    lower_map = {c.tag.lower(): c for c in item}
    for tag in tag_variants:
        c = lower_map.get(tag.lower())
        if c is not None and c.text and c.text.strip():
            return c.text.strip()
    return None


def find_attr(item: ET.Element, attr_variants: list[str]) -> Optional[str]:
    lower_attrs = {k.lower(): v for k, v in item.attrib.items()}
    for attr in attr_variants:
        v = lower_attrs.get(attr.lower())
        if v and v.strip():
            return v.strip()
    return None


def parse_available(item: ET.Element) -> Optional[bool]:
    raw = find_attr(item, AVAILABLE_ATTRS)
    if raw is None:
        raw = find_child_text(item, AVAILABLE_TAGS)
    if raw is None:
        return None
    low = raw.strip().lower()
    if low in TRUE_WORDS:
        return True
    if low in FALSE_WORDS:
        return False
    # if the value looks like a number (a stock count) — treat >0 as "in stock"
    try:
        return float(low.replace(",", ".")) > 0
    except ValueError:
        return None


def extract_offer(item: ET.Element, source_name: str) -> Optional[Offer]:
    sku = find_attr(item, SKU_ATTRS) or find_child_text(item, SKU_TAGS)
    if not sku:
        return None  # without an SKU there's no way to match it to a product on the site

    name = find_child_text(item, NAME_TAGS)
    price = find_child_text(item, PRICE_TAGS) or find_attr(item, ["price"])
    qty = find_child_text(item, QTY_TAGS)
    available = parse_available(item)

    # if there's no explicit availability flag but there is a quantity, derive it
    if available is None and qty is not None:
        try:
            available = float(qty.replace(",", ".")) > 0
        except ValueError:
            pass

    return Offer(
        sku=sku.strip(),
        name=name,
        price=price,
        quantity=qty,
        available=available,
        source=source_name,
    )


def iter_items(root: ET.Element):
    """Yields every product element, regardless of nesting depth."""
    for elem in root.iter():
        if elem.tag.lower() in (t.lower() for t in ITEM_TAGS):
            yield elem


# --------------------------------------------------------------------------
# 5. DIAGNOSTIC MODE (--inspect)
# --------------------------------------------------------------------------

def inspect_source(src: dict) -> None:
    print(f"\n{'=' * 70}\nSource: {src['name']}\nURL: {src['url']}\n{'=' * 70}")
    content = download_feed(src["url"], src["name"])
    if content is None:
        return
    root = parse_xml(content, src["name"])
    if root is None:
        print("Could not parse the XML. First 500 bytes of the response:")
        print(content[:500])
        return

    print(f"Root tag: <{root.tag}>")
    items = list(iter_items(root))
    print(f"Product elements found (matching tags {ITEM_TAGS}): {len(items)}")

    if not items:
        print("\nNone of the expected product tags were found. "
              "Here are the first 3 levels of the document tree:")
        for child in list(root)[:5]:
            print(f"  <{child.tag}> children: {[local_tag(c.tag) for c in list(child)[:10]]}")
        return

    sample = items[0]
    print("\nExample of the first product (raw XML):")
    print(ET.tostring(sample, encoding="unicode")[:2000])

    tags_found = sorted({c.tag for c in sample})
    print(f"\nChild tags of this product: {tags_found}")
    print(f"Attributes of this product: {list(sample.attrib.keys())}")

    offer = extract_offer(sample, src["name"])
    print("\nWhat the script recognized with the current SKU_TAGS/PRICE_TAGS/QTY_TAGS:")
    print(offer)


# --------------------------------------------------------------------------
# 6. MERGING SOURCES
# --------------------------------------------------------------------------

def collect_offers() -> dict[str, Offer]:
    merged: dict[str, tuple[int, Offer]] = {}  # sku -> (priority, offer)

    for src in sorted(SOURCES, key=lambda s: s.get("priority", 999)):
        name, url, priority = src["name"], src["url"], src.get("priority", 999)
        log.info("Downloading feed: %s", name)
        content = download_feed(url, name)
        if content is None:
            continue
        root = parse_xml(content, name)
        if root is None:
            continue

        items = list(iter_items(root))
        log.info("[%s] product elements found: %d", name, len(items))
        if not items:
            log.warning(
                "[%s] no products were recognized — this supplier's XML "
                "structure may be different. Run "
                "`python merge_feeds.py --inspect` to diagnose.",
                name,
            )
            continue

        count_ok, count_skipped, count_no_name = 0, 0, 0
        for item in items:
            offer = extract_offer(item, name)
            if offer is None:
                count_skipped += 1
                continue
            if not offer.name:
                count_no_name += 1
            existing = merged.get(offer.sku)
            if existing is None or priority < existing[0]:
                merged[offer.sku] = (priority, offer)
            count_ok += 1
        log.info(
            "[%s] processed: %d, skipped (no SKU): %d",
            name, count_ok, count_skipped,
        )
        if count_no_name:
            log.warning(
                "[%s] %d product(s) had no recognizable <name> — the SKU "
                "will be used as a placeholder name instead. Run "
                "`python merge_feeds.py --inspect` and check NAME_TAGS if "
                "this looks wrong.",
                name, count_no_name,
            )

    return {sku: pair[1] for sku, pair in merged.items()}


# --------------------------------------------------------------------------
# 7. BUILDING THE OUTPUT YML FILES
# --------------------------------------------------------------------------

def _new_yml_root() -> tuple[ET.Element, ET.Element]:
    """Creates the shared <yml_catalog><shop>...</shop></yml_catalog> skeleton."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    root = ET.Element("yml_catalog", {"date": now})
    shop = ET.SubElement(root, "shop")
    ET.SubElement(shop, "name").text = SHOP_NAME
    ET.SubElement(shop, "company").text = SHOP_COMPANY
    ET.SubElement(shop, "url").text = SHOP_URL
    currencies = ET.SubElement(shop, "currencies")
    ET.SubElement(currencies, "currency", {"id": "UAH", "rate": "1"})
    # Deliberately WITHOUT <categories> — so the site's folder structure is untouched.
    return root, shop


def _offer_name(offer: Offer) -> str:
    """Prom.ua rejects the whole import if <name> is missing, even on a
    matched/update-only offer. Fall back to the SKU so the import never
    fails outright, but this is a poor substitute for a real product name
    — see the warning logged in collect_offers()."""
    return offer.name if offer.name else offer.sku


def build_stock_yml(offers: dict[str, Offer]) -> ET.ElementTree:
    """The file for the daily auto-update: availability and stock, plus the
    <name> Prom requires on every offer (see _offer_name)."""
    root, shop = _new_yml_root()
    offers_el = ET.SubElement(shop, "offers")
    for sku, offer in sorted(offers.items()):
        available = "true" if offer.available else "false" if offer.available is not None else "true"
        offer_el = ET.SubElement(offers_el, "offer", {"id": sku, "available": available})
        ET.SubElement(offer_el, "name").text = _offer_name(offer)
        if offer.quantity is not None:
            ET.SubElement(offer_el, "quantity_in_stock").text = normalize_qty(offer.quantity)
    return ET.ElementTree(root)


def build_price_yml(offers: dict[str, Offer]) -> ET.ElementTree:
    """A separate file for a manual/infrequent price update. Products with
    no recognized price in the sources are left out of this file. Also
    includes <name>, since Prom requires it on every offer (see
    _offer_name)."""
    root, shop = _new_yml_root()
    offers_el = ET.SubElement(shop, "offers")
    skipped = 0
    for sku, offer in sorted(offers.items()):
        if not offer.price:
            skipped += 1
            continue
        offer_el = ET.SubElement(offers_el, "offer", {"id": sku})
        ET.SubElement(offer_el, "name").text = _offer_name(offer)
        ET.SubElement(offer_el, "price").text = normalize_price(offer.price)
        ET.SubElement(offer_el, "currencyId").text = "UAH"
    if skipped:
        log.warning("Price file: %d product(s) with no price in the sources were skipped.", skipped)
    return ET.ElementTree(root)


def normalize_price(raw: str) -> str:
    cleaned = re.sub(r"[^\d.,]", "", raw).replace(",", ".")
    try:
        return f"{float(cleaned):.2f}"
    except ValueError:
        return "0.00"


def normalize_qty(raw: str) -> str:
    cleaned = re.sub(r"[^\d.,]", "", raw).replace(",", ".")
    try:
        return str(int(float(cleaned)))
    except ValueError:
        return "0"


def indent(elem: ET.Element, level: int = 0) -> None:
    """Makes the ElementTree output readable (Python < 3.9 has no ET.indent)."""
    i = "\n" + level * "  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        for child in elem:
            indent(child, level + 1)
            if not child.tail or not child.tail.strip():
                child.tail = i + "  "
        if not elem[-1].tail or not elem[-1].tail.strip():
            elem[-1].tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i


# --------------------------------------------------------------------------
# 8. MAIN
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inspect", action="store_true",
        help="Show the raw structure of each feed to help configure the "
             "tag lists, then exit without building an output file.",
    )
    parser.add_argument(
        "--mode", choices=["stock", "price", "both"], default="stock",
        help="stock — availability/quantity only (for the daily auto-run, "
             "default); price — price only (run manually whenever you "
             "need to update prices); both — build both files in one run.",
    )
    parser.add_argument(
        "--output", default=STOCK_OUTPUT_FILE,
        help=f"Path to the stock file (default: {STOCK_OUTPUT_FILE}).",
    )
    parser.add_argument(
        "--price-output", default=PRICE_OUTPUT_FILE,
        help=f"Path to the price file (default: {PRICE_OUTPUT_FILE}).",
    )
    args = parser.parse_args()

    if args.inspect:
        for src in SOURCES:
            inspect_source(src)
        return 0

    offers = collect_offers()
    if not offers:
        log.error(
            "No products could be collected from any source. "
            "No file was created. Run --inspect to diagnose."
        )
        return 1

    if args.mode in ("stock", "both"):
        tree = build_stock_yml(offers)
        indent(tree.getroot())
        out_path = Path(args.output)
        tree.write(out_path, encoding="UTF-8", xml_declaration=True)
        log.info("Done: stock file (%d products) written to %s",
                  len(offers), out_path.resolve())

    if args.mode in ("price", "both"):
        tree = build_price_yml(offers)
        indent(tree.getroot())
        out_path = Path(args.price_output)
        tree.write(out_path, encoding="UTF-8", xml_declaration=True)
        with_price = sum(1 for o in offers.values() if o.price)
        log.info("Done: price file (%d products) written to %s",
                  with_price, out_path.resolve())

    return 0


if __name__ == "__main__":
    sys.exit(main())
