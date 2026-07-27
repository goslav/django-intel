# Polovni Automobili Proof-of-Concept Runbook

This workflow collects one broad Peugeot 3008 diesel/automatic search and lets local classification separate pre-facelift, facelift, and unknown listings. Do not use registration year as a facelift classifier.

## 1. Prepare Django

From the repository root in PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python manage.py migrate
python manage.py seed_priority_market_profiles
python manage.py runserver
```

## 2. Configure the search

Open `http://127.0.0.1:8000/source-searches/new/` or Django Admin at `http://127.0.0.1:8000/admin/`.

Create one active `polovniautomobili` source search with the public Peugeot 3008 diesel/automatic search-results URL. Start with:

- Maximum pages: `2`
- Request delay: `2.00` seconds

The URL must use HTTPS and point to a passenger-car search, not an individual advertisement. The source-search list and detail URL show its database ID.

## 3. Run discovery without writes

Replace `<id>` with the configured source-search ID:

```powershell
python manage.py refresh_source_search <id> --dry-run --max-pages 2 --request-delay 2
```

Review the reported advertisement IDs and public URLs. Dry run fetches search-result pages only: it does not request details or change runs, listings, snapshots, memberships, or missing status.

## 4. Run the first collection

```powershell
python manage.py refresh_source_search <id> --max-pages 2 --request-delay 2
```

Inspect:

1. `/source-searches/<id>/` for status and counters.
2. The latest run link for each advertisement's detail status and errors.
3. `/listings/classification-review/` for unknown or low-confidence classifications.
4. `/market-profiles/` and the Peugeot 3008 II pre-facelift/facelift 1.5 BlueHDi automatic dashboards.

Do not treat missing advertisements as sold. Memberships are marked missing only after a complete, untruncated, error-free run.

## 5. Observe changes later

Run the same normal-refresh command later. Changed price or mileage creates a new immutable snapshot; an unchanged observation does not duplicate a snapshot. Compare the new run, profile statistics, and listing price history with the first run.

If HTTP 403/429, a challenge page, pagination truncation, or any detail error occurs, inspect the failed or partial run before retrying. Such runs do not mark memberships missing.
