# Berlin Gynecology Directory Pipeline

Fetches public search results from the 116117 doctor-search API and writes raw snapshots plus cleaned CSV, SQLite, JSON, and QA outputs.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Create a local `.env` file with `ARZTSUCHE_UA`, `ARZTSUCHE_AUTH`, and `ARZTSUCHE_REQ_VAL`; `ARZTSUCHE_COOKIE` is optional. These values are required for API requests; `.env` and `data/` are ignored by git. Refresh the values when the API responds with HTTP 401 or 403.

## Run

The supplied Berlin gynecology query uses only the `fgg=05` specialty filter:

```sh
python fetch.py --probe-pagination --gynecology-only
```

The common paging fields tested so far (`page`, `pageNumber`, `pageIndex`, `offset`, `start`, `startIndex`, and `from`) all returned the same first 120 doctors. No continuation request has been verified. Do not treat `moreResults: true` as proof that an additional page can be fetched.

Fetch a bounded snapshot of the current result page:

```sh
python fetch.py --snapshot berlin-gynecology-20261011 --gynecology-only --page-field none --max-pages 1
python clean.py berlin-gynecology-20261011 --berlin-only
```

Each response is checkpointed under `data/raw/<snapshot>/page_0000.json`; the manifest records the request body, page counts, and deduplication. Use `--snapshot` again to resume an interrupted fetch. A full export requires discovering a valid continuation mechanism first.

Clean the snapshot into all-area outputs:

```sh
python clean.py berlin-2026-10
```

Restrict outputs to records with both a Berlin city and PLZ 10115-14199:

```sh
python clean.py berlin-2026-10 --berlin-only
```

Outputs are written under `data/processed/<snapshot>/`: one CSV per table in `csv/`, `directory.sqlite` (including `v_berlin_doctors`), `practices_web.json` without slot-level data, and `qa_report.txt`. Availability retains source slot types `01`/`02`/`03`; derived hours union overlapping intervals across all types. `r=900` is sent as supplied, but its units and geographic effect have not been verified.

## Test

```sh
python -m unittest discover -s tests -v
```