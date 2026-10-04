"""Integration checks; TESTAG_TEST_DATABASE_URL must name a disposable database."""
import csv
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import build_db as builder

URL = os.environ.get("TESTAG_TEST_DATABASE_URL")
ROOT = Path(builder.__file__).resolve().parent


@unittest.skipUnless(URL, "Set TESTAG_TEST_DATABASE_URL to a disposable PostgreSQL database")
class IncrementalTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.connection = psycopg.connect(URL, autocommit=True, client_encoding="UTF8")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.connection.close)
        self.data = Path(self.temp.name)
        sources = sorted((ROOT / "data").glob("*/*/*/scores.csv"), key=lambda p: p.stat().st_size)
        self.sources = sources[:2]
        self.first = self.copy_partition(self.sources[0])
        self.build()

    def copy_partition(self, source):
        target = self.data / source.parent.relative_to(ROOT / "data")
        shutil.copytree(source.parent, target)
        return target

    def build(self, **kwargs):
        return builder.build_database(ROOT, self.data, URL, builder.validate_archive(self.data), **kwargs)

    def snapshot(self):
        with self.connection.cursor() as cursor:
            result = []
            for table in ("contest_result_partition", "contest_result", "contest_result_operator", "testag_dataset_release"):
                cursor.execute(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY row_to_json(t)::text")
                result.append(cursor.fetchall())
            return result

    def change_metadata(self):
        path = self.first / "meta.json"
        meta = json.loads(path.read_text())
        meta["retrieved_at_utc"] = "2026-10-04T00:00:00Z"
        path.write_text(json.dumps(meta))

    def test_unchanged_and_new_partition(self):
        before = self.snapshot()
        result = self.build(incremental=True)
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(self.snapshot(), before)
        self.copy_partition(self.sources[1])
        result = self.build(incremental=True)
        self.assertEqual((result["added"], result["unchanged"]), (1, 1))
        self.assertTrue(set(before[1]).issubset(set(self.snapshot()[1])))

    def test_metadata_and_score_replacement(self):
        self.change_metadata()
        self.assertEqual(self.build(incremental=True)["replaced"], 1)
        path = self.first / "scores.csv"
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        rows[0]["score"] = "987654321"
        rows[0]["score_source"] = "987654321"
        rows[0]["operators_json"] = '[]'
        ranks = builder._calculate_partition_ranks(rows)
        for row in rows:
            for name, value in ranks[row["entry_id"]].items():
                row[name] = "" if value is None else str(value)
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=builder.SCORE_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        result = self.build(incremental=True)
        self.assertEqual(result["replaced"], 1)
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT score FROM contest_result WHERE entry_id=%s", (rows[0]["entry_id"],))
            self.assertEqual(cursor.fetchone()[0], 987654321)
            cursor.execute("SELECT count(*) FROM contest_result_operator WHERE entry_id=%s", (rows[0]["entry_id"],))
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_explicit_removals(self):
        second = self.copy_partition(self.sources[1])
        self.build(incremental=True)
        before = self.snapshot()
        shutil.rmtree(second)
        with self.assertRaisesRegex(ValueError, "allow-removals"):
            self.build(incremental=True)
        self.assertEqual(self.snapshot(), before)
        result = self.build(incremental=True, allow_removals=True)
        self.assertEqual((result["removed"], result["partitions"]), (1, 1))

    def test_failure_rolls_back_data_and_release(self):
        self.change_metadata()
        self.copy_partition(self.sources[1])
        before = self.snapshot()
        original = builder._load_partition
        calls = 0
        def failing_load(*args):
            nonlocal calls
            calls += 1
            result = original(*args)
            # A separate reader still sees the complete previously published dataset.
            self.assertEqual(self.snapshot(), before)
            if calls == 2:
                raise RuntimeError("injected failure after COPY")
            return result
        with patch.object(builder, "_load_partition", side_effect=failing_load):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                self.build(incremental=True)
        self.assertEqual(self.snapshot(), before)
        result = self.build(incremental=True)
        self.assertEqual((result["added"], result["replaced"]), (1, 1))

    def test_legacy_columns_and_builder_version(self):
        with self.connection.cursor() as cursor:
            cursor.execute("ALTER TABLE contest_result_partition DROP COLUMN archive_digest, DROP COLUMN builder_version")
        self.assertEqual(self.build(incremental=True)["replaced"], 1)
        with self.connection.cursor() as cursor:
            cursor.execute("UPDATE contest_result_partition SET builder_version='older'")
        self.assertEqual(self.build(incremental=True)["replaced"], 1)

    def test_resume_and_changed_archive_rejection(self):
        self.copy_partition(self.sources[1])
        result = self.build(resume=True)
        self.assertEqual(result["partitions"], 2)
        self.change_metadata()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Cannot resume"):
            self.build(resume=True)
        self.assertEqual(self.snapshot(), before)

    def test_return_to_previous_archive_publishes_current_release(self):
        path = self.first / "meta.json"
        original = path.read_bytes()
        initial_release = self.build(incremental=True)["release_id"]
        self.change_metadata()
        updated_release = self.build(incremental=True)["release_id"]
        self.assertNotEqual(initial_release, updated_release)
        path.write_bytes(original)
        self.assertEqual(self.build(incremental=True)["release_id"], initial_release)
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT release_id FROM testag_dataset_release")
            self.assertEqual(cursor.fetchall(), [(initial_release,)])

    def test_archive_changes_after_validation(self):
        stats = builder.validate_archive(self.data)
        self.change_metadata()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "changed after validation"):
            builder.build_database(ROOT, self.data, URL, stats, incremental=True)
        self.assertEqual(self.snapshot(), before)

    def test_count_mismatch_rolls_back(self):
        self.change_metadata()
        stats = builder.validate_archive(self.data)
        stats.entries += 1
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "count mismatch"):
            builder.build_database(ROOT, self.data, URL, stats, incremental=True)
        self.assertEqual(self.snapshot(), before)
