"""ParKit local verifier-style tests (PRD §12)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="parkit-test-")
os.environ["DATABASE_PATH"] = str(Path(_TMP) / "test.db")
os.environ["OWNER_PASSWORD"] = "testpass"
os.environ["BUSINESS_NAME"] = "Harbor HVAC"
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
os.environ["MARKETING_URL"] = ""
os.environ["SECRET_KEY"] = "test-secret"
for k in (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "RUNNER_EMAIL",
    "RUNNER_PHONE",
    "OLLAMA_BASE_URL",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
):
    os.environ.pop(k, None)

import app as app_module  # noqa: E402
import helpers as H  # noqa: E402

SAMPLE_CSV = Path(__file__).resolve().parent / "sample-hvac-par.csv"


class ParKitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_module.init_db()
        cls.app = app_module.app
        cls.app.config["TESTING"] = True

    def setUp(self):
        self.client = self.app.test_client()
        db_path = os.environ["DATABASE_PATH"]
        for suffix in ("", "-wal", "-shm"):
            p = Path(db_path + suffix)
            if p.exists():
                p.unlink()
        with self.app.app_context():
            H.init_schema(H.get_db())

    def _login(self):
        return self.client.post(
            "/login",
            data={"password": "testpass", "next": "/"},
            follow_redirects=False,
        )

    def test_01_health_public_ok_smtp_llm_false(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIs(data["smtp_configured"], False)
        self.assertIs(data["sms_configured"], False)
        self.assertIs(data["llm_configured"], False)

    def test_02_auth_gates_home_health_public(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))
        r2 = self.client.get("/health")
        self.assertEqual(r2.status_code, 200)

    def test_03_create_truck_import_par_gap_sku(self):
        self._login()
        r = self.client.post(
            "/trucks",
            data={"name": "Truck 1", "notes": "Van"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn("/trucks/", r.headers["Location"])
        truck_id = int(r.headers["Location"].rstrip("/").split("/")[-2])
        csv_text = SAMPLE_CSV.read_text(encoding="utf-8")
        r2 = self.client.post(
            f"/trucks/{truck_id}/par",
            data={"action": "import", "par_text": csv_text},
            follow_redirects=True,
        )
        self.assertEqual(r2.status_code, 200)
        with self.app.app_context():
            db = H.get_db()
            txv = db.execute(
                "SELECT * FROM par_items WHERE truck_id = ? AND sku = ?",
                (truck_id, "TXV-3T"),
            ).fetchone()
            self.assertIsNotNone(txv)
            self.assertLess(txv["on_hand"], txv["min_qty"])
            tags = H.tags_from_row(txv)
            self.assertIn("ac_no_cool", tags)

    def test_04_no_cool_job_build_gap_qty(self):
        self._login()
        self.client.post("/trucks", data={"name": "Truck 1"})
        with self.app.app_context():
            db = H.get_db()
            truck = db.execute("SELECT id FROM trucks WHERE name = 'Truck 1'").fetchone()
            tid = truck["id"]
            items = H.parse_par_csv(SAMPLE_CSV.read_text(encoding="utf-8"))
            H.upsert_par_items(db, tid, items)
        for_date = H.tomorrow_date_str()
        r = self.client.post(
            "/jobs/import",
            data={
                "date": for_date,
                "jobs_text": "Truck 1|Carrier no-cool — Smith",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        r2 = self.client.post(
            "/runs/build",
            data={"for_date": for_date},
            follow_redirects=False,
        )
        self.assertEqual(r2.status_code, 302)
        loc = r2.headers["Location"]
        self.assertIn("/runs/", loc)
        detail = self.client.get(loc)
        self.assertEqual(detail.status_code, 200)
        html = detail.get_data(as_text=True)
        self.assertIn("TXV-3T", html)
        self.assertIn("CAP-45", html)
        self.assertIn("qty", html.lower())
        with self.app.app_context():
            db = H.get_db()
            run_id = int(loc.rstrip("/").split("/")[-1])
            lines = db.execute(
                "SELECT * FROM restock_lines WHERE run_id = ?", (run_id,)
            ).fetchall()
            skus = {ln["sku"]: ln for ln in lines}
            self.assertIn("TXV-3T", skus)
            self.assertGreaterEqual(skus["TXV-3T"]["qty_to_pull"], 1)
            self.assertIn("CAP-45", skus)
            self.assertGreaterEqual(skus["CAP-45"]["qty_to_pull"], 1)

        csv_r = self.client.get(f"/runs/{run_id}.csv")
        self.assertEqual(csv_r.status_code, 200)
        body = csv_r.get_data(as_text=True)
        self.assertIn("TXV-3T", body)
        self.assertTrue(len(body.strip().splitlines()) > 1)

        self.assertIn("copyFrom(", html)
        self.assertNotIn('onclick="copyText({{\'', html)

    def test_05_works_without_llm_twilio_smtp(self):
        self.assertFalse(H.smtp_configured())
        self.assertFalse(H.sms_configured())
        self.assertFalse(H.llm_configured())
        self.assertEqual(H.resolve_job_tag("Carrier no-cool call"), "ac_no_cool")
        self.assertEqual(H.resolve_job_tag("Replace water heater"), "water_heater")
        self.assertEqual(H.resolve_job_tag("GFCI outlet dead"), "electrical_gfci")
        self.assertEqual(H.resolve_job_tag("Mystery tick"), "general_service")

    def test_06_no_partping_customer_routes(self):
        r = self.client.get("/p/faketoken")
        self.assertEqual(r.status_code, 404)
        self.assertIsNone(self.app.view_functions.get("barcode_scan"))

    def test_07_empty_marketing_no_powered_by(self):
        self._login()
        r = self.client.get("/")
        html = r.get_data(as_text=True)
        self.assertNotIn("Powered by ParKit", html)

    def test_08_yaml_par_import(self):
        self._login()
        self.client.post("/trucks", data={"name": "Truck Y"})
        with self.app.app_context():
            db = H.get_db()
            tid = db.execute("SELECT id FROM trucks WHERE name='Truck Y'").fetchone()["id"]
        yaml_text = (Path(__file__).parent / "sample-hvac-par.yaml").read_text()
        r = self.client.post(
            f"/trucks/{tid}/par",
            data={"action": "import", "par_text": yaml_text},
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            db = H.get_db()
            n = db.execute(
                "SELECT COUNT(*) AS c FROM par_items WHERE truck_id=?", (tid,)
            ).fetchone()["c"]
            self.assertGreaterEqual(n, 2)

    def test_09_pipe_and_csv_jobs(self):
        self._login()
        self.client.post("/trucks", data={"name": "Truck 1"})
        for_date = H.tomorrow_date_str()
        r = self.client.post(
            "/jobs/import",
            data={
                "date": for_date,
                "jobs_text": "truck,title,tag,job_ref,date\nTruck 1,No cool unit,,J-1,\n",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            db = H.get_db()
            c = db.execute(
                "SELECT COUNT(*) AS c FROM jobs_import WHERE scheduled_date=?",
                (for_date,),
            ).fetchone()["c"]
            self.assertGreaterEqual(c, 1)


if __name__ == "__main__":
    unittest.main()
