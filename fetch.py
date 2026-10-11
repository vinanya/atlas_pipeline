#!/usr/bin/env python3
"""Fetch and checkpoint pages from the 116117 public doctor search API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


API_URL = "https://arztsuche.116117.de/api/data"
REFERER = "https://arztsuche.116117.de/"
BASE_BODY: dict[str, Any] = {
    "r": 900,
    "locType": "OSM",
    "lat": None,
    "lon": None,
    "plz": None,
    "osmId": "62422",
    "osmType": "R",
    "filterSelections": [
        {"title": "Fremdsprache", "fieldName": "fs", "selectedCodes": ["03"]},
        {"title": "Fachgebiet Kategorie", "fieldName": "fgg", "selectedCodes": ["05"]},
    ],
    "locOrigin": "USER_INPUT",
    "searchTrigger": "INITIAL",
    "viaDeeplink": False,
}
PROBE_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("page", 2, 1),
    ("pageNumber", 2, 1),
    ("pageIndex", 1, 1),
    ("offset", 120, 120),
    ("start", 120, 120),
    ("startIndex", 120, 120),
    ("from", 120, 120),
)


class CredentialsError(RuntimeError):
    pass


class AuthorizationError(RuntimeError):
    pass


def identity_hash(record: dict[str, Any]) -> str:
    fields = ("vorname", "name", "strasse", "hausnummer", "plz", "tel")
    normalized = [" ".join(str(record.get(field) or "").strip().lower().split()) for field in fields]
    return hashlib.sha1("|".join(normalized).encode("utf-8")).hexdigest()


def load_credentials() -> dict[str, str]:
    load_dotenv(Path(__file__).with_name(".env"))
    names = ("ARZTSUCHE_UA", "ARZTSUCHE_AUTH", "ARZTSUCHE_REQ_VAL")
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        raise CredentialsError(
            "Missing required environment variables: " + ", ".join(missing)
            + ". Add them to your ignored .env file; do not commit credentials."
        )
    headers = {
        "User-Agent": os.environ["ARZTSUCHE_UA"],
        "Authorization": os.environ["ARZTSUCHE_AUTH"],
        "req-val": os.environ["ARZTSUCHE_REQ_VAL"],
    }
    if os.getenv("ARZTSUCHE_COOKIE"):
        headers["Cookie"] = "".join(os.environ["ARZTSUCHE_COOKIE"].splitlines()).strip()
    return headers


def retry_delay(response: requests.Response | None, attempt: int) -> float:
    if response is not None and response.headers.get("Retry-After"):
        value = response.headers["Retry-After"].strip()
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return min(60.0, 2**attempt) + random.uniform(0.0, 0.5)


def post_json(session: requests.Session, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    for attempt in range(5):
        response: requests.Response | None = None
        try:
            response = session.post(API_URL, headers=headers, json=body, timeout=(10, 60), allow_redirects=False)
        except requests.RequestException as error:
            if attempt == 4:
                raise RuntimeError(f"Request failed after retries: {error}") from error
            delay = retry_delay(None, attempt)
            print(f"Request failed; retrying in {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
            continue

        if response.status_code in (401, 403):
            raise AuthorizationError(
                f"API returned HTTP {response.status_code}. Refresh ARZTSUCHE_AUTH / "
                "ARZTSUCHE_REQ_VAL in .env, then resume the snapshot."
            )
        if 300 <= response.status_code < 400:
            raise AuthorizationError(
                f"API redirected the request with HTTP {response.status_code}. Refresh the API credentials "
                "or request-validation value in .env."
            )
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 4:
                response.raise_for_status()
            delay = retry_delay(response, attempt)
            print(f"API returned HTTP {response.status_code}; retrying in {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
            continue
        response.raise_for_status()
        try:
            payload = response.json()
        except requests.JSONDecodeError as error:
            raise RuntimeError("API response was not valid JSON") from error
        if not isinstance(payload, dict):
            raise RuntimeError("API response JSON was not an object")
        return payload
    raise RuntimeError("Request retries exhausted")


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def records_from(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("arztPraxisDatas") or []
    if not isinstance(records, list):
        raise RuntimeError("API field arztPraxisDatas was not a list")
    return [record for record in records if isinstance(record, dict)]


def run_probe(headers: dict[str, str], delay: float, base_body: dict[str, Any]) -> int:
    with requests.Session() as session:
        first = records_from(post_json(session, headers, base_body.copy()))
        first_hashes = {identity_hash(record) for record in first}
        print(f"Baseline: {len(first)} records, {len(first_hashes)} unique identities")
        if not first:
            return 1
        found = False
        for field, value, step in PROBE_FIELDS:
            time.sleep(random.uniform(max(0.0, delay - 0.25), delay + 0.25))
            body = base_body.copy()
            body[field] = value
            records = records_from(post_json(session, headers, body))
            hashes = {identity_hash(record) for record in records}
            new_count = len(hashes - first_hashes)
            overlap = len(hashes & first_hashes)
            print(f"{field}={value}: {len(records)} records, {new_count} new, {overlap} overlapping")
            if new_count:
                time.sleep(random.uniform(max(0.0, delay - 0.25), delay + 0.25))
                next_body = base_body.copy()
                next_body[field] = value + step
                next_records = records_from(post_json(session, headers, next_body))
                next_hashes = {identity_hash(record) for record in next_records}
                next_overlap = len(hashes & next_hashes)
                print(
                    f"  next {field}={value + step}: {len(next_records)} records, "
                    f"{len(next_hashes - hashes)} new vs previous, {next_overlap} overlapping"
                )
                baseline_overlap = len(first_hashes & next_hashes)
                if next_hashes - hashes and next_overlap == 0 and baseline_overlap == 0:
                    print(f"Verified three disjoint result sets for paging candidate: {field}")
                else:
                    print(f"{field} did not verify as disjoint sequential paging (overlap with baseline: {baseline_overlap})")
                found = True
        if not found:
            print("No tested field produced new identities. Capture the browser's load-more request before fetching all pages.")
        return 0


def run_radius_probe(headers: dict[str, str], delay: float, base_body: dict[str, Any]) -> int:
    with requests.Session() as session:
        for index, radius in enumerate((900, 10, 100)):
            if index:
                time.sleep(random.uniform(max(0.0, delay - 0.25), delay + 0.25))
            body = base_body.copy()
            body["r"] = radius
            records = records_from(post_json(session, headers, body))
            distances = []
            for record in records:
                try:
                    distances.append(float(record.get("distance")))
                except (TypeError, ValueError):
                    continue
            maximum = max(distances) if distances else None
            print(f"r={radius}: {len(records)} records, maximum returned distance={maximum} m")
        print("Compare result counts and returned distances to infer r's effect; the API's unit semantics may still need confirmation.")
    return 0


def fetch_snapshot(args: argparse.Namespace, headers: dict[str, str]) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.snapshot):
        raise ValueError("Snapshot names may contain only letters, digits, dot, underscore, and hyphen")

    snapshot_dir = Path(args.output) / args.snapshot
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    base_body = getattr(args, "base_body", None)
    if base_body is None:
        if getattr(args, "gynecology_only", False):
            base_body = {
                **BASE_BODY,
                "filterSelections": [{"title": "Fachgebiet Kategorie", "fieldName": "fgg", "selectedCodes": ["05"]}],
            }
        elif getattr(args, "no_filters", False):
            base_body = {**BASE_BODY, "filterSelections": []}
        else:
            base_body = BASE_BODY.copy()
    manifest_path = snapshot_dir / "manifest.json"
    manifest: dict[str, Any]
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("page_field") != args.page_field:
            raise ValueError("The existing snapshot uses a different --page-field")
        if manifest.get("request_body") != base_body:
            raise ValueError("The existing snapshot uses a different request body")
        if manifest.get("complete"):
            print(f"Snapshot {args.snapshot} is already complete ({manifest.get('stopped_reason')}); use a new snapshot name to refetch.")
            return 0
    else:
        manifest = {
            "snapshot": args.snapshot,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "request_body": base_body,
            "page_field": args.page_field,
            "complete": False,
            "stopped_reason": None,
            "pages": [],
        }

    seen: set[str] = set()
    for page in sorted(snapshot_dir.glob("page_*.json")):
        payload = json.loads(page.read_text(encoding="utf-8"))
        seen.update(identity_hash(record) for record in records_from(payload))

    page_index = 0
    while (snapshot_dir / f"page_{page_index:04d}.json").exists():
        page_index += 1
    if page_index:
        print(f"Resuming {args.snapshot} after {page_index} saved pages ({len(seen)} unique identities)")

    with requests.Session() as session:
        while args.max_pages is None or page_index < args.max_pages:
            body = base_body.copy()
            page_value: int | None = None
            if page_index > 0 and args.page_field:
                page_value = args.page_start + (page_index - 1) * args.page_step
                body[args.page_field] = page_value
            payload = post_json(session, headers, body)
            records = records_from(payload)
            hashes = [identity_hash(record) for record in records]
            new_hashes = set(hashes) - seen
            duplicate_count = len(hashes) - len(set(hashes)) + len(set(hashes) & seen)

            page_path = snapshot_dir / f"page_{page_index:04d}.json"
            atomic_json(page_path, payload)
            manifest["pages"] = [entry for entry in manifest.get("pages", []) if entry.get("index") != page_index]
            manifest["pages"].append({
                "index": page_index,
                "file": page_path.name,
                "page_value": page_value,
                "record_count": len(records),
                "new_count": len(new_hashes),
                "duplicate_count": duplicate_count,
                "more_results": payload.get("moreResults"),
            })
            manifest["pages"].sort(key=lambda entry: entry["index"])
            manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
            manifest["page_count"] = len(manifest["pages"])
            manifest["unique_records"] = len(seen | set(hashes))
            manifest["record_count"] = sum(entry["record_count"] for entry in manifest["pages"])
            manifest["duplicate_count"] = manifest["record_count"] - manifest["unique_records"]
            atomic_json(manifest_path, manifest)

            print(
                f"Saved {page_path}: {len(records)} records, {len(new_hashes)} new, "
                f"{duplicate_count} duplicate(s), moreResults={payload.get('moreResults')}"
            )
            if not records or not new_hashes:
                print("Stopping: page is empty or contains no new identities")
                manifest["complete"] = True
                manifest["stopped_reason"] = "empty_or_no_new_records"
                manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
                atomic_json(manifest_path, manifest)
                break
            seen.update(hashes)
            if payload.get("moreResults") is False:
                print("Stopping: API reports no more results")
                manifest["complete"] = True
                manifest["stopped_reason"] = "api_reports_no_more_results"
                manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
                atomic_json(manifest_path, manifest)
                break
            page_index += 1
            time.sleep(random.uniform(max(0.0, args.delay - 0.25), args.delay + 0.25))

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--output", default="data/raw")
    parser.add_argument("--page-field", default="page", help="Unverified API paging field; use 'none' to omit it")
    parser.add_argument("--page-start", type=int, default=2, help="Value used on the second request")
    parser.add_argument("--page-step", type=int, default=1, help="Increment per subsequent request")
    parser.add_argument("--delay", type=float, default=1.5, help="Jittered delay between page requests")
    parser.add_argument("--max-pages", type=int, help="Optional page cap, useful for a controlled initial run")
    parser.add_argument("--no-filters", action="store_true", help="Use filterSelections=[] as in the supplied browser request")
    parser.add_argument("--gynecology-only", action="store_true", help="Use only the fgg=05 gynecology filter, as in the latest Berlin request")
    parser.add_argument("--probe-pagination", action="store_true", help="Test common paging fields and consecutive results")
    parser.add_argument("--probe-radius", action="store_true", help="Compare result counts and distances for r=900, 10, and 100")
    args = parser.parse_args()
    if args.no_filters and args.gynecology_only:
        parser.error("--no-filters and --gynecology-only cannot be combined")
    if args.gynecology_only:
        args.base_body = {
            **BASE_BODY,
            "filterSelections": [{"title": "Fachgebiet Kategorie", "fieldName": "fgg", "selectedCodes": ["05"]}],
        }
    else:
        args.base_body = {**BASE_BODY, "filterSelections": []} if args.no_filters else BASE_BODY.copy()
    if args.page_field.lower() == "none":
        args.page_field = ""
    if args.delay < 0 or args.page_step < 1:
        parser.error("--delay must be nonnegative and --page-step must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    try:
        headers = {
            "Content-Type": "application/json",
            "Referer": REFERER,
            **load_credentials(),
        }
        if args.probe_pagination:
            return run_probe(headers, args.delay, args.base_body)
        if args.probe_radius:
            return run_radius_probe(headers, args.delay, args.base_body)
        return fetch_snapshot(args, headers)
    except (AuthorizationError, CredentialsError, requests.RequestException, RuntimeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())