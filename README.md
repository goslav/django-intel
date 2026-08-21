# Django Market Intel

A server-rendered Django application for Serbian used-car market intelligence. It helps dealers import marketplace listings, preserve historical asking-price observations, browse active advertisements, and compare similar vehicles.

The project is intentionally a Django modular monolith. It currently uses SQLite and plain Django templates without a JavaScript framework or background-job infrastructure.

## Current features

- Manual purchase-opportunity tracking through the existing `Vehicle` model
- CSV marketplace-listing imports
- Idempotent listing updates using marketplace source and external ID
- Historical asking-price and mileage snapshots
- Full-snapshot removal detection scoped to one source
- Import-run audit records and row-level validation errors
- Active-listing browser with filters, sorting, and pagination
- Listing detail pages with configurable comparable criteria
- Asking-price minimum, maximum, average, and median statistics
- Chronological price history
- Django admin support for vehicles, listings, snapshots, and import runs
- Dealer API inventory ingestion, daily snapshots, pricing metrics, and disappearance tracking
- Immutable dealer snapshot history with captured price, mileage, status, timestamp, and raw observations

## Requirements

- Python 3.14 or another version supported by the pinned Django release
- Dependencies from `requirements.txt`

## Local setup

Create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Apply migrations and start the development server:

```powershell
python manage.py migrate
python manage.py runserver
```

Open <http://127.0.0.1:8000/>.

To use Django administration, create a local superuser if one does not already exist:

```powershell
python manage.py createsuperuser
```

## Application routes

- `/` — project homepage
- `/market/` — active marketplace listings
- `/market/<listing_id>/` — listing details, comparables, and price history
- `/vehicles/` — manually entered purchase opportunities
- `/admin/` — Django administration

## Importing marketplace listings

Import a partial listing snapshot:

```powershell
python manage.py import_listings path\to\listings.csv --source example
```

Import a complete snapshot for a source:

```powershell
python manage.py import_listings path\to\listings.csv --source example --full-snapshot
```

A successful full snapshot marks active listings from the same source as removed when they are absent from the file. Partial or failed imports never mark listings as removed.

### CSV format

The CSV must be UTF-8 and contain the following columns. Column order is not significant.

```csv
source,external_id,source_url,title,make,model,year,mileage,fuel,transmission,asking_price,observed_at
example,12345,https://example.test/12345,Volkswagen Golf 1.6 TDI,Volkswagen,Golf,2018,125000,diesel,manual,12900.00,2026-07-01T10:00:00Z
```

Optional values:

- `source_url`
- `title`
- `transmission`

All other values are required. `observed_at` must be an ISO-8601 datetime. Naive datetimes use Django's configured time zone. Every row's `source` must match the command's `--source` value.

Each command creates an `ImportRun`. Listings are upserted by `(source, external_id)`, and repeated observations with the same listing and timestamp do not create duplicate snapshots.

## Comparable listings

The detail page selects active comparables with a latest asking-price observation using these defaults:

- Same make, model, and fuel, matched case-insensitively
- Model year within ±1 year
- Mileage within ±30,000 km
- Selected listing excluded
- Transmission matching disabled by default

Users can change year and mileage tolerances and optionally require the same transmission. Displayed statistics describe advertised asking prices, not confirmed sale prices.

## Dealer intelligence

Seed the configured active Polovni Automobili dealers (including Kia Centar):

```powershell
python manage.py seed_tracked_dealers
```

The seed is idempotent and updates matching dealers by storefront URL or stable
Polovni identifier. AK Kompresor is retained and labelled as new-cars-only;
Kia Centar and Auto Nena Still Peugeot are labelled as mixed new-and-used dealers;
Autoland is tracked separately as a used-car dealer. Holliday is marked as the QA dealer for validating collection behavior. You can also create a dealer in Django administration with its API URL, source, external dealer
ID, and desired price-bracket size. The API must return a JSON array (or an object
with a `vehicles` array) containing `external_id`, `make`, `model`, `year`,
`mileage`, `fuel`, and `asking_price`. Optional fields are `title`, `source_url`,
`thumbnail_url`, `transmission`, and `published_at`.

If the API uses bearer authentication, configure the environment-variable name on
the dealer; the token itself is never stored. Create the complete daily snapshot:

```powershell
python manage.py refresh_dealer <dealer_id>
```

Every successful response is treated as complete inventory. Ads absent from the
response are timestamped as disappeared, and a later return is tracked as a
reappearance. To refresh every active dealer, use the automation-safe batch command:

```powershell
python manage.py refresh_active_dealers
```

The batch command creates a new immutable snapshot after each successful refresh,
skips dealers that already have a completed snapshot for the current local day,
and continues when one dealer fails. It exits with an error after the remaining
dealers have run if any refresh failed, so a retry only attempts dealers that do
not yet have a successful snapshot that day. Validation, listing updates, and the
new snapshot are committed atomically; partial or failed responses do not replace
the last successful state.

The single-dealer command follows the same daily limit and skips a dealer that
already has a completed snapshot for the requested local calendar day.

Daily automation includes every dealer marked active. Dealers added through the
tracked-dealer seed list or Django administration are therefore included in the
next daily batch automatically; the Windows scheduled task does not need to be
updated for each new dealer.

Dealer snapshots are source-specific. For example, the British Motors dealer
record tracks its Polovni Automobili storefront only; inventory published on the
dealer's separate `bmpolovnavozila.rs` platform is not included in these totals.

For Windows Task Scheduler, create a daily task whose **Program/script** is the
project virtual environment's Python executable, for example
`C:\path\to\django-intel\.venv\Scripts\python.exe`. Set **Add arguments** to
`manage.py refresh_active_dealers` and **Start in** to `C:\path\to\django-intel`.
Configure any dealer token environment variables for the task's Windows account,
and set the task's "If the task is already running" option to "Do not start a new
instance."

Completed captures are never overwritten. Each snapshot preserves its summary and
captured listing set, while each snapshot item stores the observed price, mileage,
status, timestamp, and raw source record. Browse dealer history at
`/dealers/<dealer_id>/snapshots/`.

For Polovni Automobili dealers, the collector checks the transient `description`
(`Opis`) text for service/commission-sale and new/unused-vehicle wording. Only the
resulting reasons are saved, not the description. Use the **Opis flagged** dealer
inventory view to review those ads and exclude or later restore selected vehicles.
To rescan the currently present ads without creating a snapshot, run:

```powershell
python manage.py refresh_dealer_description_flags <dealer_id>
```

Open `/dealers/` for the dealer dashboard and select a dealer to inspect its latest
inventory and historical changes. The detail view shows newly observed and
disappeared advertisements with first-registration year, captured asking price,
mileage, observation timestamps, current presence, and links to the public ad.
It also reports additions observed during the last 7 and 30 days and an average
weekly replenishment rate. The initial baseline inventory is excluded from these
replenishment counts.

Open `/dealers/activity/` to rank active dealers over a 7- or 30-day window by new
inventory, disappeared advertisements, total activity, net inventory flow, and
weekly addition rate. A disappeared advertisement may have been sold, withdrawn,
or expired, so the application never presents disappearance as a confirmed sale.

Compare a make/model across the latest complete snapshots for all active dealers at
`/dealers/compare/`. The comparison supports dealer, year, fuel, and transmission
filters (including all dealers or any multi-selected subset), ranks the most
frequently observed models under the current filters, and
links to a per-dealer vehicle drill-down. Thirty-day changes come from
completed historical snapshots; disappeared advertisements are labelled as no
longer observed, never as confirmed sales.

## Tests and checks

Run the complete verification set:

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

## Project structure

```text
config/                         Django project configuration and root routes
vehicles/
  management/commands/          Import, dealer seed, and daily refresh commands
  migrations/                   Database schema migrations
  services/dealer_intelligence.py Dealer inventory collection and snapshots
  services/dealer_comparison.py Cross-dealer comparison and activity analysis
  services/import_listings.py   Ingestion and validation logic
  services/market_analysis.py   Latest-price, comparable, and summary logic
  templates/vehicles/           Server-rendered pages and shared layout
  admin.py                      Django admin configuration
  models.py                     Vehicle and marketplace data models
  views.py                      Homepage, browser, detail, and vehicle views
```

## Current limitations

- Dealer collection depends on public storefront/detail pages and may require
  parser updates when the source website changes
- Daily automation is local-only through Windows Task Scheduler; there is no
  deployed worker or hosted scheduler
- No price charts, recommendations, or maximum-bid calculations
- No confirmed sale-price data
- No API or separate frontend
- Development configuration uses SQLite and is not production hardened
