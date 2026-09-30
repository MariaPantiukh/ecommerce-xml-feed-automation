# Prom Feed Merger — reliable price/stock updates for Prom.ua

This project updates prices, availability and quantity for **already
existing** products on your Prom.ua store, using data pulled from several
supplier XML feeds, matched by article number (SKU).

## Why this approach (and not a YML "update via link" feed)

An earlier version of this project used Prom's "automatic catalog update
via link" YML feature. In testing, it turned out that feature matches
products by the `id` attribute of `<offer>`, which has to equal a product
ID that Prom itself assigned — not the supplier's SKU. Products that were
never created through that exact YML feed don't have this ID, so Prom
couldn't recognize them and created duplicates instead of updating them.
It also required sending a `<name>` for every offer, which came from the
supplier (often in Russian) and overwrote the Ukrainian name Prom actually
displays as primary.

The current approach instead:

1. Starts from a **full product export you download from Prom.ua**
   (`base_export.xlsx`). This file already has every product's real name
   (RU + UK), description, images, characteristics, and — crucially — its
   **"Унікальний_ідентифікатор"** (Prom's permanent internal product ID).
   This is the field Prom.ua's Excel import actually uses to match a row
   to an existing product, and it's always present on any export.
2. Downloads the supplier feeds and, for every product whose article
   matches, overwrites **only** the Ціна / Наявність / Кількість cells for
   that row.
3. Leaves every other cell — name, translations, description, images,
   characteristics, group — exactly as exported. Prom.ua also specifically
   does not touch the Ukrainian translation at all when the `*_укр`
   columns are left as-is, which is another safeguard against the
   name-mismatch problem.
4. Products that exist in a supplier's feed but not in your base export
   (i.e. not yet on your site) are skipped — only existing products are
   ever touched, by design.

## Files

- `build_prom_import.py` — the main script. Reads `base_export.xlsx`,
  pulls supplier data, writes an updated `.xlsx` ready for import.
  Supports `--mode stock` (availability/quantity only), `--mode price`
  (price only), or `--mode both`.
- `merge_feeds.py` — supplier feed downloading/parsing logic, used as a
  library by `build_prom_import.py`.
- `base_export.xlsx` — **you provide this.** A full export of your
  current catalog from Prom.ua (Товари → Експорт → XLS/XLSX). Re-export
  and replace this file whenever you add new products you want covered by
  automatic updates.
- `.github/workflows/update-feed.yml` — daily job, builds
  `docs/prom_stock.xlsx` (availability + quantity only).
- `.github/workflows/update-price.yml` — manual-only job (Run workflow
  button), builds `docs/prom_price.xlsx` (price only).

As before, price and stock are kept as **two separate files**, since they
need updating on a different schedule: stock changes daily/automatically,
price only when you decide to trigger it.

## 1. Get your base export

In your Prom.ua cabinet: **Товари → Експорт**, format **XLS/XLSX**.
Save it, rename it to `base_export.xlsx`, and place it in the root of
this project (next to `build_prom_import.py`).

## 2. Test locally (recommended)

```bash
pip install -r requirements.txt

# Check that the script recognizes the right columns in your export:
python build_prom_import.py --base base_export.xlsx --inspect
```

If a column isn't detected, open `build_prom_import.py` and add the exact
header text from your file to the matching list in `HEADER_VARIANTS` near
the top of the file.

Then do a real run:

```bash
python build_prom_import.py --base base_export.xlsx --mode stock --output prom_stock.xlsx
python build_prom_import.py --base base_export.xlsx --mode price --output prom_price.xlsx
```

Open the resulting files and spot-check a couple of rows: only the
field(s) matching the mode should differ from your export, everything
else identical.

## 3. Deploying on GitHub Actions (daily auto-run + a permanent link)

### Step 1 — create a repository and upload files

Same as before: create a repository on GitHub, then **Add file → Upload
files** and add `build_prom_import.py`, `merge_feeds.py`,
`requirements.txt`, `README.md`, `base_export.xlsx`, and the `.github`
folder.

### Step 2 — enable write permissions for Actions

**Settings → Actions → General → Workflow permissions → Read and write
permissions → Save.**

### Step 3 — enable GitHub Pages

**Settings → Pages → Source: Deploy from a branch → Branch: main, folder:
/docs → Save.** You'll have two permanent links (once each file has been
generated at least once — see Step 4):

```
https://<your-username>.github.io/<repo-name>/prom_stock.xlsx   ← availability/quantity (daily)
https://<your-username>.github.io/<repo-name>/prom_price.xlsx   ← price (manual)
```

### Step 4 — run both workflows once manually

**Actions** tab → run **"Update Prom stock file (daily)"** and
**"Update Prom price file (manual)"** once each (Run workflow button).
Check that `docs/prom_stock.xlsx` and `docs/prom_price.xlsx` both appear
and the links above open them.

### Step 5 — point Prom.ua at the two links

In Prom.ua's import settings, add **two separate** "Імпорт за посиланням"
sources (available for XLS/XLSX/CSV files, not just YML):
- one pointing at `prom_stock.xlsx`, checked periodically (e.g. every few
  hours);
- one pointing at `prom_price.xlsx` — set it to the least frequent
  schedule Prom allows, since you'll trigger it yourself by running the
  price workflow whenever you actually want prices updated (Step 6).

### Step 6 — updating prices when you need to

Whenever you want to push fresh prices: **Actions → "Update Prom price
file (manual)" → Run workflow.** It rebuilds `prom_price.xlsx`; Prom will
pick up the change the next time it checks that source (or trigger the
import manually on Prom's side too, if you want it immediately).

## 4. Refreshing the base export

Whenever you add genuinely new products to your site that you want
covered by these automatic updates, or make manual name/description edits
you want the base file to reflect, re-export from Prom, replace
`base_export.xlsx` in the repository (edit the file the same way as any
other file — see below), and the next daily run will pick up the new
rows.

## 5. Safely replacing files without breaking anything already running

- Edit a file's content in place (the pencil icon on GitHub) rather than
  deleting and re-uploading — this keeps the same filename/path, so the
  workflow and the Pages link keep working without interruption.
- Never edit anything inside `docs/` by hand — `docs/prom_stock.xlsx` and
  `docs/prom_price.xlsx` are generated outputs, overwritten automatically
  by their respective workflow runs.
- Don't rename `build_prom_import.py`, `merge_feeds.py`,
  `requirements.txt`, or `base_export.xlsx` — the workflow refers to them
  by these exact names.

## 6. Adding or changing a supplier

Open `merge_feeds.py`, find the `SOURCES` list near the top, and add /
edit an entry there (see the comments in that file for the exact format).
Run `python merge_feeds.py --inspect` to check that SKU/price/quantity are
recognized correctly for the new supplier.

## 7. About the exact run time (06:00 Kyiv time)

GitHub Actions schedules run in UTC and don't account for daylight saving
time. `cron: "0 3 * * *"` (03:00 UTC) is roughly 06:00 Kyiv time in summer
(UTC+3) and roughly 05:00 in winter (UTC+2). Twice a year, if you need
minute-perfect accuracy, edit the cron value in
`.github/workflows/update-feed.yml` (`"0 4 * * *"` in winter, back to
`"0 3 * * *"` in summer). You can always trigger an update immediately
with the **Run workflow** button regardless of the schedule.
