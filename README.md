# Merge Prom Feed — merging supplier feeds into YML for Prom.ua

The `merge_feeds.py` script downloads XML feeds from several suppliers,
matches products by SKU (article number), and builds **two separate**
YML files for Prom.ua's **"Automatic catalog update via link"** feature:

| File | Fields | Update frequency |
|---|---|---|
| `merged_feed.xml` (stock file) | `id`, `available`, `quantity_in_stock` | **updated automatically every day** (GitHub Actions) |
| `price_feed.xml` (price file) | `id`, `price`, `currencyId` | **updated manually**, whenever you decide to update prices |

Neither file contains descriptions, images, or categories — only the
fields needed to update an existing product by its article number, so
that the update can't accidentally change the folder structure on your
site (see the comment at the top of `merge_feeds.py` for details).

**One exception: `<name>` is included too.** Prom.ua's YML import rejects
the *entire* file if any offer is missing a product name ("Поле Назва
позиції: Обов'язкове поле") — even for update-only offers matched by id.
So both files include `<name>`, taken from the supplier's own feed; if a
supplier doesn't provide one, the SKU is used as a placeholder so the
import doesn't fail outright (you'll see a warning about this when you
run the script — see section 1). In practice this means Prom will also
overwrite the product's name with the supplier's name on every update,
which is a limitation of Prom's import format, not something this script
can avoid.

On Prom.ua these two files are connected as **two separate sources** for
automatic catalog updates via link: one with a "daily" schedule, the
other triggered by you manually with the "Update now" button in that
source's settings (or, if Prom requires a schedule there too, pick the
least frequent option, e.g. "once a month", and actually refresh it by
re-running the workflow below whenever you want).

---

## 1. Testing locally (optional, but strongly recommended)

```bash
pip install -r requirements.txt

# First, check how each supplier's feed is actually structured:
python merge_feeds.py --inspect
```

`--inspect` downloads each feed and prints:
- the document's root tag;
- how many products were found;
- the raw XML of the first product;
- what the script recognized as SKU / price / quantity.

If the result is empty for some supplier, or fields are recognized
incorrectly, open `merge_feeds.py` and add the real tag name to the
matching list at the top of the file: `SKU_TAGS`, `PRICE_TAGS`,
`QTY_TAGS`, or `AVAILABLE_TAGS`. These are lines like:

```python
SKU_TAGS = ["vendorCode", "vendor_code", "sku", "article", ...]
```

Just add the needed tag name there and run `--inspect` again.

The script supports a `--mode` flag:

```bash
python merge_feeds.py --mode stock                  # stock/availability only (default)
python merge_feeds.py --mode price                  # price only
python merge_feeds.py --mode both                   # both files in one run

# You can also specify custom file paths/names:
python merge_feeds.py --mode stock --output merged_feed.xml
python merge_feeds.py --mode price --price-output price_feed.xml
```

---

## 2. Deploying on GitHub Actions (daily auto-run + a permanent link)

### Step 1. Create a GitHub repository

1. Go to [github.com](https://github.com) → **New repository**.
2. Name it, e.g. `prom-feed`. Public or private doesn't matter much
   (Pages works for both on paid plans; on the free plan, GitHub Pages
   for private repos isn't available, so if you don't have a Pro plan —
   make the repository **public**; an XML feed with prices isn't
   sensitive information).
3. Don't add a README when creating it — you'll upload the files manually
   (step 2).

### Step 2. Upload the project files

The easiest way is through the web interface:

1. Open the newly created repository → **Add file → Upload files**.
2. Drag in `merge_feeds.py`, `requirements.txt`, `README.md`, and the
   entire `.github` folder (it contains two files: `update-feed.yml` —
   the daily stock update, and `update-price.yml` — the manual price
   update; GitHub preserves the nested folder structure correctly when
   you drag a folder in).
3. Click **Commit changes**.

*(Alternative for those used to git: `git init`, `git add .`,
`git commit -m "init"`, `git remote add origin <repo URL>`,
`git push -u origin main`.)*

### Step 3. Enable write permissions for Actions

1. In the repository: **Settings → Actions → General**.
2. Scroll down to **Workflow permissions**.
3. Select **Read and write permissions**.
4. Click **Save**.

(Without this step, the workflow won't be able to commit the updated
`merged_feed.xml` / `price_feed.xml` back into the repository.)

### Step 4. Enable GitHub Pages — this is what gives you the permanent link

1. **Settings → Pages**.
2. Under **Source**, choose **Deploy from a branch**.
3. Branch: **main**, folder: **/docs**.
4. **Save**.

Within 1–2 minutes GitHub will show the base Pages address, and you'll
have **two permanent links** (the files don't exist yet, but you can
already paste the links into Prom — they'll start working as soon as
you run the corresponding workflow for the first time, step 5):

```
https://<your-username>.github.io/<repo-name>/merged_feed.xml   ← stock/availability (daily)
https://<your-username>.github.io/<repo-name>/price_feed.xml    ← price (manual)
```

Paste the first link into Prom.ua as the source for automatic
stock/availability updates, and the second as a separate source for
price.

### Step 5. Test both workflows manually

1. Open the **Actions** tab in the repository.
2. On the left you'll see two workflows: **Update Prom stock feed
   (daily)** and **Update Prom price feed (manual)**.
3. Open the first one → click **Run workflow → Run workflow** on the
   right. Wait for the green checkmark (usually 20–40 seconds) and
   check that `docs/merged_feed.xml` appeared and that the link opens.
4. Do the same for **Update Prom price feed (manual)** —
   `docs/price_feed.xml` should appear.

After this:
- the **stock file** will keep updating itself automatically every day
  on schedule (`cron: "0 3 * * *"` in
  `.github/workflows/update-feed.yml`);
- the **price file** will NEVER update on its own — only when you go to
  the Actions tab and manually click **Run workflow** on
  **Update Prom price feed (manual)**. This is the "manual" price
  update mode you asked for.

---

## 3. About the exact run time (06:00 Kyiv time)

GitHub Actions schedules jobs in UTC and doesn't account for daylight
saving time. The current `cron: "0 3 * * *"` (03:00 UTC) gives:

- **in summer** (UTC+3, EEST) → roughly **06:00** Kyiv time;
- **in winter** (UTC+2, EET) → roughly **05:00** Kyiv time.

If you need it to be exactly 06:00 year-round, there are two options:

**Option A (simpler):** twice a year (late March and late October, when
Ukraine switches to/from daylight saving time), open
`.github/workflows/update-feed.yml` and change `"0 3 * * *"` to
`"0 4 * * *"` (winter) or back to `"0 3 * * *"` (summer).

**Option B (automatic, more complex):** add two `cron` entries
(`"0 3 * * *"` and `"0 4 * * *"`) to the workflow and, at the start of
the job, check the current date — whether Europe is currently in
daylight saving time — and exit the step early (`exit 0`) if this run is
the "extra" one. I can write this version of the workflow for you if
needed.

You can also always trigger an update manually at any time with the
**Run workflow** button (step 5 above) — for example, if you need to
update prices urgently without waiting for the schedule.

---

## 4. Adding a new supplier later

Open `merge_feeds.py`, find the `SOURCES` list near the top of the file,
and add a new entry:

```python
{
    "name": "new_supplier",
    "url": "https://.../feed.xml",
    "priority": 4,  # lower number = higher priority when the same SKU appears twice
},
```

Run `python merge_feeds.py --inspect` to check that fields are recognized
correctly for the new supplier (see section 1).

---

## 5. Repository structure

```
.
├── merge_feeds.py                      # main script
├── requirements.txt                    # dependencies (requests)
├── README.md                           # this file
├── docs/
│   ├── merged_feed.xml                 # stock/availability — updated automatically every day
│   └── price_feed.xml                  # price — updated only manually
└── .github/
    └── workflows/
        ├── update-feed.yml             # daily auto-run for the stock file
        └── update-price.yml            # manual-only run for the price file
```
