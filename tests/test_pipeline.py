import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from clean import build_tables, write_outputs
import fetch


def fixture_doctor(**overrides):
    record = {
        "id": 1,
        "vorname": "Anna",
        "name": "Muster",
        "titel": "Dr. med.",
        "namezusatz": "",
        "anrede": "Frau",
        "geschlecht": "w",
        "tel": "+49 (30) 123 45",
        "fax": "",
        "email": " NOT-AN-EMAIL ",
        "web": "http://https://praxis.example",
        "strasse": "Musterstraße",
        "hausnummer": "1",
        "plz": "10115",
        "ort": "Berlin",
        "lat": 52.5,
        "lon": 13.4,
        "fg": [{"code": "05", "name": "Gynaekologie", "children": [{"code": "516", "name": "Fertilitaet"}]}],
        "fs": [{"code": "03", "name": "Englisch"}],
        "bf": [{"code": "101", "name": "Rollstuhl"}],
        "bfk2025": [],
        "zb": [],
        "psy": False,
        "zm": [],
        "kvg": [{"title": "Ultraschall", "services": ["Abdomen"]}],
        "keineSprechzeiten": False,
        "tsz": [],
    }
    record.update(overrides)
    return record


class PipelineFixtureTest(unittest.TestCase):
    def test_duplicate_pages_cleaning_and_availability(self):
        today = date.today()
        calendar_days = [today + timedelta(days=offset) for offset in range(28)]
        saturday = next(day for day in calendar_days if day.weekday() == 5)
        doctor = fixture_doctor(tsz=[{"d": day.isoformat(), "typTsz": []} for day in calendar_days])
        doctor["tsz"][0]["typTsz"] = [
            {"typ": "01", "sprechzeiten": [{"z": "07:30 - 10:00"}]},
            {"typ": "02", "sprechzeiten": [{"z": "09:00 - 12:00"}]},
            {"typ": "03", "sprechzeiten": [{"z": "not a time"}]},
        ]
        saturday_entry = next(entry for entry in doctor["tsz"] if entry["d"] == saturday.isoformat())
        saturday_entry["typTsz"].append({"typ": "03", "sprechzeiten": [{"z": "17:00 - 19:00"}]})
        second_doctor = fixture_doctor(
            id=2,
            vorname="Bernd",
            name="Beispiel",
            strasse="Musterstr.",
            email="BERND@EXAMPLE.DE",
            web="https://example.de",
            fg=[],
            fs=[],
            bf=[],
            tsz=[],
        )
        duplicate = {**doctor, "id": 119}

        with tempfile.TemporaryDirectory() as temporary:
            raw_dir = Path(temporary) / "raw" / "fixture"
            raw_dir.mkdir(parents=True)
            (raw_dir / "page_0000.json").write_text(
                json.dumps({"arztPraxisDatas": [doctor, second_doctor], "moreResults": True}), encoding="utf-8"
            )
            (raw_dir / "page_0001.json").write_text(
                json.dumps({"arztPraxisDatas": [duplicate], "moreResults": False}), encoding="utf-8"
            )
            (raw_dir / "manifest.json").write_text(
                json.dumps({"snapshot": "fixture", "page_count": 2}), encoding="utf-8"
            )

            tables, qa = build_tables(raw_dir)
            self.assertEqual(qa["raw_records"], 3)
            self.assertEqual(qa["unique_records"], 2)
            self.assertEqual(qa["duplicate_records"], 1)
            self.assertEqual(qa["unparseable_times"], 1)
            self.assertEqual(qa["practices"], 1)
            self.assertEqual(tables["practices"][0]["doctor_count"], 2)
            self.assertEqual(tables["doctors"][0]["focus"], "fertility")
            self.assertEqual(tables["doctors"][0]["access_level"], "full")
            self.assertEqual(tables["doctors"][0]["language_count"], 1)
            self.assertEqual(tables["doctors"][0]["open_days"], 2)
            self.assertEqual(tables["doctors"][0]["weekly_open_hours"], 1.62)
            self.assertEqual(tables["doctors"][0]["has_early_slots"], 1)
            self.assertEqual(tables["doctors"][0]["has_evening_slots"], 1)
            self.assertEqual(tables["doctors"][0]["has_saturday_slots"], 1)
            self.assertEqual(tables["doctors"][0]["no_hours_listed"], 0)
            self.assertEqual(tables["doctors"][0]["email"], "")
            self.assertEqual(tables["doctors"][0]["web"], "https://praxis.example")
            self.assertEqual(len(tables["availability_slots"]), 3)

            filtered, filtered_qa = build_tables(raw_dir, berlin_only=True)
            self.assertEqual(len(filtered["doctors"]), 2)
            self.assertEqual(filtered_qa["berlin_practices"], 1)

            output_dir = Path(temporary) / "processed" / "fixture"
            write_outputs(tables, qa, output_dir)
            with sqlite3.connect(output_dir / "directory.sqlite") as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM v_berlin_doctors").fetchone()[0], 2)
            web_data = json.loads((output_dir / "practices_web.json").read_text(encoding="utf-8"))
            self.assertEqual(len(web_data), 1)
            self.assertNotIn("tsz", json.dumps(web_data))
            self.assertTrue((output_dir / "qa_report.txt").exists())

    def test_fetch_saves_pages_and_stops_when_page_has_no_new_records(self):
        first = fixture_doctor(id=1)
        second = fixture_doctor(id=2, vorname="Bernd", name="Beispiel", strasse="Nebenweg")
        third = fixture_doctor(id=3, vorname="Clara", name="Neu", strasse="Neuweg")
        responses = [
            {"arztPraxisDatas": [first, second], "moreResults": True},
            {"arztPraxisDatas": [second, third], "moreResults": True},
            {"arztPraxisDatas": [first, second], "moreResults": True},
        ]
        args = Namespace(
            snapshot="fetch-fixture",
            output="",
            page_field="page",
            page_start=2,
            page_step=1,
            max_pages=None,
            delay=1.5,
        )

        with tempfile.TemporaryDirectory() as temporary:
            args.output = temporary
            with patch.object(fetch.requests, "Session"), patch.object(fetch, "post_json", side_effect=responses) as post:
                with patch.object(fetch.time, "sleep"):
                    fetch.fetch_snapshot(args, {})

            snapshot_dir = Path(temporary) / "fetch-fixture"
            manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["page_count"], 3)
            self.assertEqual(manifest["unique_records"], 3)
            self.assertEqual(manifest["duplicate_count"], 3)
            self.assertEqual(post.call_count, 3)
            self.assertEqual(post.call_args_list[1].args[2]["page"], 2)
            self.assertEqual(post.call_args_list[2].args[2]["page"], 3)
            self.assertTrue((snapshot_dir / "page_0002.json").exists())
            with patch.object(fetch.requests, "Session"), patch.object(fetch, "post_json") as post:
                fetch.fetch_snapshot(args, {})
                post.assert_not_called()


if __name__ == "__main__":
    unittest.main()