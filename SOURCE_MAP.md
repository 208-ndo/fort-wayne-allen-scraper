# Fort Wayne / Allen County Source Map

This file lists target public data sources for a future scraper. No live scraping is enabled in the first build.

## Property / Auditor Records

- Target: Allen County property and parcel records.
- Intended use: owner name, property address, parcel ID, assessed value, mailing address, land use, transfer history.
- Matching keys: parcel ID, normalized property address, normalized owner name.

## Treasurer / Tax Delinquent

- Target: Allen County Treasurer tax and delinquency records.
- Intended use: tax delinquent balances, payment pressure, unpaid property tax signals.
- Matching keys: parcel ID, property address, owner name.

## Recorder / Foreclosure / Lis Pendens Equivalent

- Target: Allen County Recorder public records and civil filing equivalents where available.
- Intended use: mortgage filings, foreclosure-related notices, liens, lis pendens style signals if exposed.
- Matching keys: owner name, parcel ID, legal description, property address.

## Sheriff Sales

- Target: Allen County Sheriff sales and sale notices.
- Intended use: scheduled sheriff sales, case number, plaintiff/lender, sale date, judgment amount.
- Matching keys: case number, property address, defendant/owner name.

## Probate / Estate Records

- Target: Allen County probate and estate public records.
- Intended use: decedent/estate filings, executor references, estate property leads.
- Matching keys: decedent name, owner name, mailing address, property address.

## Fort Wayne Code Violations

- Target: City of Fort Wayne code enforcement, building, unsafe structure, and violation records where public.
- Intended use: repair pressure, nuisance violations, unsafe structure flags, repeat owner signals.
- Matching keys: property address, parcel ID if available.

## Vacant / Nuisance Property Signals

- Target: Fort Wayne and Allen County vacant, nuisance, unsafe, demolition, or board-up signals where public.
- Intended use: vacant property leads, nuisance pressure, high grass/trash/unsafe building indicators.
- Matching keys: normalized property address, parcel ID.

## Absentee / Out-of-State Owner Matching

- Target: Derived from property/auditor mailing address compared with situs address.
- Intended use: absentee owner and out-of-state owner tags.
- Matching keys: parcel ID, owner name, property address, mailing address.

## First Live Scraper Notes

- Keep Fort Wayne output paths separate from Akron:
  - `data/records.json`
  - future `data/records.enriched.json`
  - future `dashboard/*.json` only if needed
- Do not add a county switcher until multiple Indiana counties are intentionally supported.
