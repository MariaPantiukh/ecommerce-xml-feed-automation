# Prom Feed Merger

Automated Python pipeline for updating product prices and stock levels on Prom.ua via Excel export matching.

This project automatically syncs price, availability, and quantity updates from multiple supplier XML feeds directly to a Prom.ua store. By leveraging Prom's permanent internal product identifiers, it reliably updates existing items without creating duplicates or disrupting localized titles and descriptions.

---

## 📌 Architecture & Logic

### Core Problem Solved
Prom.ua's built-in "update catalog via YML link" matches products strictly by the `<offer id="...">` attribute (Prom's system ID). It cannot match by the supplier's SKU for pre-existing items, often leading to:
* **Duplicate listings** if items weren't originally imported via that exact YML feed.
* **Overwritten titles**, replacing custom or Ukrainian localized product names with raw supplier strings.

### Solution Design
This tool uses a hybrid export/merge approach:

1. **Source of Truth:** Reads `base_export.xlsx` (a full standard export from Prom.ua), retaining all critical product data (Ukrainian/Russian titles, descriptions, categories, images, and characteristics).
2. **Deterministic Matching:** Uses Prom's `Унікальний_ідентифікатор` (Prom's immutable internal ID) alongside supplier SKUs to target exact catalog rows.
3. **Selective Cell Mutation:** Overwrites **only** target fields (`Ціна`, `Наявність`, `Кількість`). All other cells — including translation columns like `*_укр` — remain untouched, eliminating localization loss.
4. **Isolated Operational Files:** Generates separate output files for stock and pricing, allowing distinct update schedules (daily background stock syncs vs. manual price triggers).

---

## 🛠 Repository Structure

```text
├── .github/
│   └── workflows/
│       ├── update-feed.yml    # Daily automated workflow (Stock & Availability)
│       └── update-price.yml   # On-demand workflow (Pricing)
├── docs/                      # Output directory for GitHub Pages distribution
│   ├── prom_stock.xlsx        # Daily generated stock feed
│   └── prom_price.xlsx        # Manually triggered price feed
├── base_export.xlsx           # Reference export from Prom.ua (Catalog baseline)
├── build_prom_import.py       # Core CLI tool & Excel builder
├── merge_feeds.py             # Supplier feed parsers & normalization module
└── requirements.txt           # Project dependencies
