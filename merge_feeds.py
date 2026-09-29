#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_feeds.py
==============

Завантажує XML-фіди кількох постачальників, витягує з них артикул (SKU),
ціну, залишок та наявність товару, і формує ОДИН підсумковий YML-файл
(yml_catalog / shop / offers / offer) для "Автоматичного оновлення каталогу
за посиланням" на Prom.ua.

Важливо (вимога №4 користувача): фінальні файли навмисно НЕ містять назв,
описів, зображень чи категорій товару — тільки id (артикул) і кілька полів
для оновлення. Це зроблено для того, щоб оновлення каталогу за посиланням
не могло випадково змінити структуру папок/категорій чи інші поля вже
існуючих на сайті товарів: Prom підставляє значення лише у товари, які
збіглися за id (артикулом), а решту полів (назву, опис, категорію) залишає
без змін, оскільки в фіді їх просто немає.

Скрипт формує ДВА окремі файли, оскільки ціна і залишки оновлюються
з різною періодичністю:

  1. "Файл залишків" (за замовчуванням merged_feed.xml) — містить тільки
     id + available + quantity_in_stock. Саме цей файл призначений для
     щоденного автозапуску через GitHub Actions (наявність/залишки
     міняються часто).

  2. "Файл ціни" (за замовчуванням price_feed.xml) — містить тільки
     id + price + currencyId. Він НЕ генерується щодня автоматично;
     запускайте його вручну (локально або кнопкою "Run workflow" в
     окремому workflow'і) лише тоді, коли справді потрібно оновити ціни.

Який файл(и) створити за один запуск, визначає прапорець --mode
(stock / price / both, за замовчуванням stock).

!!! ОБОВ'ЯЗКОВО перевірте у налаштуваннях імпорту на Prom.ua (розділ
"Прайс-листи" -> ваш фід -> "Налаштування"), що увімкнено опцію
"Оновлювати тільки ціну та наявність" (або аналогічну) — це додатковий
запобіжник на випадок, якщо Prom все ж очікує повний набір полів.

Формати фідів постачальників можуть відрізнятися. Скрипт намагається
автоматично розпізнати офіційний YML-формат (<yml_catalog>/<shop>/<offers>/
<offer>) та типовий "продуктовий" формат (<product>/<item> замість <offer>).
Якщо автоматичне визначення поля дає порожній результат — запустіть скрипт
у режимі діагностики:

    python merge_feeds.py --inspect

Це завантажить кожен фід і виведе на екран сирий XML першого товару з
нього, а також список усіх тегів, які зустрічаються всередині товару.
За цими даними скоригуйте списки SKU_TAGS / PRICE_TAGS / QTY_TAGS /
AVAILABLE_TAGS нижче (достатньо додати назву тега, який реально
використовує постачальник).
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
# 1. КОНФІГУРАЦІЯ ДЖЕРЕЛ
# --------------------------------------------------------------------------
# Додавайте / видаляйте постачальників тут. "priority" визначає, чий товар
# переможе, якщо однаковий SKU трапився у кількох фідах одночасно
# (менше число = вищий пріоритет).

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

# Назви елементів-товарів, які скрипт шукатиме в дереві XML (перевіряються
# в цьому порядку; підходить будь-яка комбінація великих/малих літер).
ITEM_TAGS = ["offer", "product", "item", "position", "good"]

# Можливі назви полів SKU/артикулу — як дочірніх тегів, так і атрибутів
# самого товару (перевіряються в порядку пріоритету).
SKU_TAGS = [
    "vendorCode", "vendor_code", "sku", "article", "articul",
    "code", "Код_товара", "Код_товару", "id", "offer_id", "productId",
]
SKU_ATTRS = ["id", "sku", "article", "code"]

PRICE_TAGS = ["price", "Цена", "Ціна", "priceuah", "price_uah"]

QTY_TAGS = [
    "quantityInStock", "quantity_in_stock", "quantity", "stock_quantity",
    "stock", "Количество", "Кількість", "in_stock", "remains",
]

AVAILABLE_TAGS = ["available", "presence", "stock_status", "Наличие", "Наявність"]
AVAILABLE_ATTRS = ["available", "in_stock"]

# Текстові значення, які трактуємо як "товар є в наявності".
TRUE_WORDS = {
    "true", "1", "yes", "y", "in_stock", "instock", "available",
    "в наявності", "в наличии", "є", "так",
}
FALSE_WORDS = {
    "false", "0", "no", "n", "out_of_stock", "outofstock", "немає в наявності",
    "нет в наличии", "under_order",  # під замовлення трактуємо як "немає в наявності"
}

REQUEST_TIMEOUT = 30  # секунд на завантаження одного фіду
STOCK_OUTPUT_FILE = "merged_feed.xml"   # id + available + quantity_in_stock (щодня)
PRICE_OUTPUT_FILE = "price_feed.xml"    # id + price + currencyId (вручну, за потреби)
SHOP_NAME = "Мій магазин"
SHOP_COMPANY = "Мій магазин"
SHOP_URL = "https://example.prom.ua"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("merge_feeds")


# --------------------------------------------------------------------------
# 2. МОДЕЛЬ ТОВАРУ
# --------------------------------------------------------------------------

@dataclass
class Offer:
    sku: str
    price: Optional[str] = None
    quantity: Optional[str] = None
    available: Optional[bool] = None
    source: str = ""


def local_tag(tag: str) -> str:
    """Прибирає XML-namespace з назви тега: '{ns}offer' -> 'offer'."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def strip_ns(root: ET.Element) -> None:
    """Рекурсивно прибирає namespace з усіх тегів дерева (in-place)."""
    for elem in root.iter():
        elem.tag = local_tag(elem.tag)


# --------------------------------------------------------------------------
# 3. ЗАВАНТАЖЕННЯ ФІДІВ
# --------------------------------------------------------------------------

def download_feed(url: str, name: str) -> Optional[bytes]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PromFeedMerger/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        if not resp.content.strip():
            log.warning("[%s] Порожня відповідь від сервера — фід пропущено.", name)
            return None
        return resp.content
    except requests.RequestException as exc:
        log.error("[%s] Не вдалося завантажити фід: %s", name, exc)
        return None


def parse_xml(content: bytes, name: str) -> Optional[ET.Element]:
    try:
        root = ET.fromstring(content)
        strip_ns(root)
        return root
    except ET.ParseError as exc:
        log.error("[%s] Файл не є коректним XML: %s", name, exc)
        return None


# --------------------------------------------------------------------------
# 4. ВИТЯГУВАННЯ ПОЛІВ ІЗ ТОВАРУ
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
    # якщо значення схоже на число (лишок/кількість) — трактуємо >0 як "є"
    try:
        return float(low.replace(",", ".")) > 0
    except ValueError:
        return None


def extract_offer(item: ET.Element, source_name: str) -> Optional[Offer]:
    sku = find_attr(item, SKU_ATTRS) or find_child_text(item, SKU_TAGS)
    if not sku:
        return None  # без артикулу зіставити з товаром на сайті неможливо

    price = find_child_text(item, PRICE_TAGS) or find_attr(item, ["price"])
    qty = find_child_text(item, QTY_TAGS)
    available = parse_available(item)

    # якщо явного прапорця наявності немає, але є кількість — виводимо з неї
    if available is None and qty is not None:
        try:
            available = float(qty.replace(",", ".")) > 0
        except ValueError:
            pass

    return Offer(
        sku=sku.strip(),
        price=price,
        quantity=qty,
        available=available,
        source=source_name,
    )


def iter_items(root: ET.Element):
    """Повертає всі елементи товарів, незалежно від глибини вкладеності."""
    for elem in root.iter():
        if elem.tag.lower() in (t.lower() for t in ITEM_TAGS):
            yield elem


# --------------------------------------------------------------------------
# 5. ДІАГНОСТИЧНИЙ РЕЖИМ (--inspect)
# --------------------------------------------------------------------------

def inspect_source(src: dict) -> None:
    print(f"\n{'=' * 70}\nДжерело: {src['name']}\nURL: {src['url']}\n{'=' * 70}")
    content = download_feed(src["url"], src["name"])
    if content is None:
        return
    root = parse_xml(content, src["name"])
    if root is None:
        print("Не вдалося розпарсити XML. Перші 500 байт відповіді:")
        print(content[:500])
        return

    print(f"Кореневий тег: <{root.tag}>")
    items = list(iter_items(root))
    print(f"Знайдено елементів-товарів (за тегами {ITEM_TAGS}): {len(items)}")

    if not items:
        print("\nЖоден із очікуваних тегів товару не знайдено. "
              "Ось перші 3 рівні дерева документа:")
        for child in list(root)[:5]:
            print(f"  <{child.tag}> children: {[local_tag(c.tag) for c in list(child)[:10]]}")
        return

    sample = items[0]
    print("\nПриклад першого товару (сирий XML):")
    print(ET.tostring(sample, encoding="unicode")[:2000])

    tags_found = sorted({c.tag for c in sample})
    print(f"\nДочірні теги цього товару: {tags_found}")
    print(f"Атрибути цього товару: {list(sample.attrib.keys())}")

    offer = extract_offer(sample, src["name"])
    print("\nЩо розпізнав скрипт із цим набором SKU_TAGS/PRICE_TAGS/QTY_TAGS:")
    print(offer)


# --------------------------------------------------------------------------
# 6. ЗЛИТТЯ ДЖЕРЕЛ
# --------------------------------------------------------------------------

def collect_offers() -> dict[str, Offer]:
    merged: dict[str, tuple[int, Offer]] = {}  # sku -> (priority, offer)

    for src in sorted(SOURCES, key=lambda s: s.get("priority", 999)):
        name, url, priority = src["name"], src["url"], src.get("priority", 999)
        log.info("Завантаження фіду: %s", name)
        content = download_feed(url, name)
        if content is None:
            continue
        root = parse_xml(content, name)
        if root is None:
            continue

        items = list(iter_items(root))
        log.info("[%s] знайдено елементів товару: %d", name, len(items))
        if not items:
            log.warning(
                "[%s] жодного товару не розпізнано — можливо, у цього "
                "постачальника інша структура XML. Запустіть "
                "`python merge_feeds.py --inspect` для діагностики.",
                name,
            )
            continue

        count_ok, count_skipped = 0, 0
        for item in items:
            offer = extract_offer(item, name)
            if offer is None:
                count_skipped += 1
                continue
            existing = merged.get(offer.sku)
            if existing is None or priority < existing[0]:
                merged[offer.sku] = (priority, offer)
            count_ok += 1
        log.info(
            "[%s] оброблено: %d, пропущено (немає SKU): %d",
            name, count_ok, count_skipped,
        )

    return {sku: pair[1] for sku, pair in merged.items()}


# --------------------------------------------------------------------------
# 7. ФОРМУВАННЯ ПІДСУМКОВИХ YML-ФАЙЛІВ
# --------------------------------------------------------------------------

def _new_yml_root() -> tuple[ET.Element, ET.Element]:
    """Створює спільний каркас <yml_catalog><shop>...</shop></yml_catalog>."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    root = ET.Element("yml_catalog", {"date": now})
    shop = ET.SubElement(root, "shop")
    ET.SubElement(shop, "name").text = SHOP_NAME
    ET.SubElement(shop, "company").text = SHOP_COMPANY
    ET.SubElement(shop, "url").text = SHOP_URL
    currencies = ET.SubElement(shop, "currencies")
    ET.SubElement(currencies, "currency", {"id": "UAH", "rate": "1"})
    # Навмисно БЕЗ <categories> — щоб не чіпати структуру папок на сайті.
    return root, shop


def build_stock_yml(offers: dict[str, Offer]) -> ET.ElementTree:
    """Файл для щоденного автооновлення: тільки наявність і залишки."""
    root, shop = _new_yml_root()
    offers_el = ET.SubElement(shop, "offers")
    for sku, offer in sorted(offers.items()):
        available = "true" if offer.available else "false" if offer.available is not None else "true"
        offer_el = ET.SubElement(offers_el, "offer", {"id": sku, "available": available})
        if offer.quantity is not None:
            ET.SubElement(offer_el, "quantity_in_stock").text = normalize_qty(offer.quantity)
    return ET.ElementTree(root)


def build_price_yml(offers: dict[str, Offer]) -> ET.ElementTree:
    """Окремий файл для ручного/нечастого оновлення ціни. Товари без
    розпізнаної ціни в джерелах у цей файл не потрапляють."""
    root, shop = _new_yml_root()
    offers_el = ET.SubElement(shop, "offers")
    skipped = 0
    for sku, offer in sorted(offers.items()):
        if not offer.price:
            skipped += 1
            continue
        offer_el = ET.SubElement(offers_el, "offer", {"id": sku})
        ET.SubElement(offer_el, "price").text = normalize_price(offer.price)
        ET.SubElement(offer_el, "currencyId").text = "UAH"
    if skipped:
        log.warning("Файл ціни: %d товар(ів) без ціни в джерелах пропущено.", skipped)
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
    """Робить ElementTree-вивід охайним (Python < 3.9 не має ET.indent)."""
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
        help="Показати сиру структуру кожного фіду для налаштування тегів "
             "і завершити роботу без формування підсумкового файлу.",
    )
    parser.add_argument(
        "--mode", choices=["stock", "price", "both"], default="stock",
        help="stock — тільки наявність/залишки (для щоденного автозапуску, "
             "за замовчуванням); price — тільки ціна (запускати вручну, "
             "коли треба оновити ціни); both — обидва файли за один раз.",
    )
    parser.add_argument(
        "--output", default=STOCK_OUTPUT_FILE,
        help=f"Шлях до файлу залишків (за замовчуванням: {STOCK_OUTPUT_FILE}).",
    )
    parser.add_argument(
        "--price-output", default=PRICE_OUTPUT_FILE,
        help=f"Шлях до файлу ціни (за замовчуванням: {PRICE_OUTPUT_FILE}).",
    )
    args = parser.parse_args()

    if args.inspect:
        for src in SOURCES:
            inspect_source(src)
        return 0

    offers = collect_offers()
    if not offers:
        log.error(
            "Жодного товару не вдалося зібрати з жодного джерела. "
            "Файл не створено. Запустіть --inspect для діагностики."
        )
        return 1

    if args.mode in ("stock", "both"):
        tree = build_stock_yml(offers)
        indent(tree.getroot())
        out_path = Path(args.output)
        tree.write(out_path, encoding="UTF-8", xml_declaration=True)
        log.info("Готово: файл залишків (%d товарів) записано у %s",
                  len(offers), out_path.resolve())

    if args.mode in ("price", "both"):
        tree = build_price_yml(offers)
        indent(tree.getroot())
        out_path = Path(args.price_output)
        tree.write(out_path, encoding="UTF-8", xml_declaration=True)
        with_price = sum(1 for o in offers.values() if o.price)
        log.info("Готово: файл ціни (%d товарів) записано у %s",
                  with_price, out_path.resolve())

    return 0


if __name__ == "__main__":
    sys.exit(main())
