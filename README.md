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
  management/commands/          CSV import command
  migrations/                   Database schema migrations
  services/import_listings.py   Ingestion and validation logic
  services/market_analysis.py   Latest-price, comparable, and summary logic
  templates/vehicles/           Server-rendered pages and shared layout
  admin.py                      Django admin configuration
  models.py                     Vehicle and marketplace data models
  views.py                      Homepage, browser, detail, and vehicle views
```

## Current limitations

- No marketplace scraper or automated import scheduling
- No price charts, recommendations, or maximum-bid calculations
- No confirmed sale-price data
- No API or separate frontend
- Development configuration uses SQLite and is not production hardened
