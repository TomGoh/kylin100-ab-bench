import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from abbench.geekbench import DatabaseExportError, export_database


class GeekbenchExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.db"

    def database(self, cpu=True, gpu=True):
        con = sqlite3.connect(self.path)
        con.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY, json TEXT)")
        if cpu:
            con.execute("CREATE TABLE cpu_documents(document_id INTEGER, score INTEGER, multicore_score INTEGER)")
        if gpu:
            con.execute("CREATE TABLE gpu_documents(document_id INTEGER, score INTEGER, api INTEGER)")
        self.addCleanup(con.close)
        return con

    def add_cpu(self, con, did=1, **changes):
        doc = {"uuid": f"cpu-{did}", "version": "Geekbench 6.7.1", "valid": 1,
               "complete_benchmark": 1, "score": 700, "multicore_score": 2100,
               "runtime": 649.781161074, "sections": [{"name": "Single-Core", "workloads": [
                   {"name": "Clang", "score": 860, "runtime": 1.85, "runtimes": [1.8, 1.9]}]}]}
        doc.update(changes)
        con.execute("INSERT INTO documents VALUES (?, ?)", (did, json.dumps(doc)))
        con.execute("INSERT INTO cpu_documents VALUES (?, ?, ?)", (did, 700, 2100))
        con.commit()
        return doc

    def test_cpu_gpu_and_full_document_are_preserved(self):
        con = self.database()
        cpu = self.add_cpu(con)
        gpu = {"uuid": "gpu-2", "version": "Geekbench 6.7.1", "valid": True,
               "complete_benchmark": True, "score": 4000,
               "sections": [{"name": "Sobel", "score": 3900}], "driver": "test-driver"}
        con.execute("INSERT INTO documents VALUES (?, ?)", (2, json.dumps(gpu)))
        con.execute("INSERT INTO gpu_documents VALUES (2, 4000, 4321)")
        con.commit()
        before = self.path.read_bytes()
        output = export_database(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(output["counts"], {"total": 2, "valid": 2, "invalid": 0, "cpu": 1, "gpu": 1})
        self.assertEqual(output["records"][0]["document"], cpu)
        self.assertEqual(output["records"][0]["metrics"], {"single": 700, "multi": 2100})
        self.assertEqual(output["records"][1]["document"], gpu)
        self.assertEqual(output["records"][1]["api_raw"], 4321)
        self.assertNotIn("api_name", output["records"][1])

    def test_absent_gpu_schema_is_unavailable_not_zero(self):
        con = self.database(gpu=False)
        self.add_cpu(con)
        output = export_database(self.path)
        self.assertEqual(output["schema"]["capabilities"]["gpu"], "unavailable")
        self.assertEqual(output["counts"]["gpu"], 0)
        self.assertEqual(output["counts"]["valid"], 1)
        self.assertIn("schema_unavailable", [issue["code"] for issue in output["issues"]])

    def test_absent_cpu_schema_still_exports_gpu(self):
        con = self.database(cpu=False)
        gpu = {"uuid": "gpu", "version": "Geekbench 6.7.1", "valid": 1,
               "complete_benchmark": 1, "score": 4000}
        con.execute("INSERT INTO documents VALUES (1, ?)", (json.dumps(gpu),))
        con.execute("INSERT INTO gpu_documents VALUES (1, 4000, 7)")
        con.commit()
        output = export_database(self.path)
        self.assertEqual(output["schema"]["capabilities"]["cpu"], "unavailable")
        self.assertTrue(output["records"][0]["valid"])

    def test_incomplete_and_app_invalid_are_kept(self):
        con = self.database()
        self.add_cpu(con, valid=False, complete_benchmark=0)
        rec = export_database(self.path)["records"][0]
        self.assertFalse(rec["valid"])
        self.assertEqual(rec["app_valid"], False)
        self.assertIn("application_invalid", rec["exclusion_reasons"])
        self.assertIn("benchmark_incomplete", rec["exclusion_reasons"])
        self.assertEqual(rec["metrics"]["single"], 700)

    def test_truthy_strings_are_not_valid_flags(self):
        con = self.database()
        self.add_cpu(con, valid="false", complete_benchmark="1")
        self.assertFalse(export_database(self.path)["records"][0]["valid"])

    def test_duplicate_uuid_excludes_both_without_removing_raw_results(self):
        con = self.database()
        self.add_cpu(con, did=1, uuid="duplicate")
        self.add_cpu(con, did=2, uuid="duplicate")
        output = export_database(self.path)
        self.assertEqual(len(output["records"]), 2)
        self.assertEqual(output["counts"]["valid"], 0)
        for rec in output["records"]:
            self.assertIn("duplicate_uuid", rec["exclusion_reasons"])

    def test_malformed_json_is_a_retained_failed_record(self):
        con = self.database()
        con.execute("INSERT INTO documents VALUES (1, '{broken')")
        con.execute("INSERT INTO cpu_documents VALUES (1, 0, 0)")
        con.commit()
        rec = export_database(self.path)["records"][0]
        self.assertEqual(rec["raw_json"], "{broken")
        self.assertFalse(rec["valid"])
        self.assertIsNone(rec["metrics"]["single"])
        self.assertIn("invalid_json", rec["exclusion_reasons"])

    def test_score_mismatch_and_orphan_are_visible(self):
        con = self.database()
        self.add_cpu(con, score=900)
        con.execute("INSERT INTO gpu_documents VALUES (999, 4000, 7)")
        con.commit()
        output = export_database(self.path)
        self.assertIn("score_mismatch:single", output["records"][0]["exclusion_reasons"])
        self.assertEqual(output["records"][1]["exclusion_reasons"], ["orphan_reference"])

    def test_corrupt_database_raises_instead_of_reporting_empty_success(self):
        self.path.write_bytes(b"this is deliberately not sqlite")
        with self.assertRaises(DatabaseExportError):
            export_database(self.path)

    def test_missing_documents_and_file_are_rejected(self):
        con = sqlite3.connect(self.path)
        con.execute("CREATE TABLE irrelevant(id INTEGER)")
        con.close()
        with self.assertRaises(DatabaseExportError):
            export_database(self.path)
        with self.assertRaises(DatabaseExportError):
            export_database(self.path.with_name("missing.db"))


if __name__ == "__main__":
    unittest.main()
