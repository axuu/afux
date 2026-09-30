import json
import os
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from unittest.mock import patch

import server


TEST_PROFILE = {
    "nickname": "测试用户",
    "height_cm": 180,
    "birth_year": 1990,
    "birth_month": 6,
    "sex": "male",
}


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        server.DB_PATH = f"{self.temporary_directory.name}/measurements.db"
        server.API_TOKEN = "test-api-token-with-safe-length"
        server.initialize_database()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.ScaleHandler)
        self.httpd.index_html = server.INDEX_TEMPLATE
        self.httpd.profile = TEST_PROFILE
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.temporary_directory.cleanup()

    def request(self, method, path, payload=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=2)
        body = json.dumps(payload) if payload is not None else None
        request_headers = dict(headers or {})
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        content = response.read()
        connection.close()
        if not content:
            parsed = None
        elif response.getheader("Content-Type", "").startswith("application/json"):
            parsed = json.loads(content)
        else:
            parsed = content.decode()
        return response.status, parsed

    def test_public_read_protected_write_and_idempotency(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Afux 体重记录", page)
        self.assertIn("体重与体脂趋势", page)
        for private_field in ("测试用户", "height_cm", "birth_year", "birth_month", "sex", "1990"):
            self.assertNotIn(private_field, page)

        status, listing = self.request("GET", "/api/measurements")
        self.assertEqual(status, 200)
        self.assertEqual(listing["total"], 0)
        self.assertNotIn("profile", listing)

        status, _ = self.request(
            "POST",
            "/api/measurements",
            {"device_id": "body-scale", "weight_g": 78100, "impedance_raw": 1330},
        )
        self.assertEqual(status, 401)

        api_headers = {"X-API-Key": server.API_TOKEN}
        status, generated = self.request(
            "POST",
            "/api/measurements",
            {"device_id": "body-scale", "weight_g": 78100, "impedance_raw": 1330},
            api_headers,
        )
        self.assertEqual(status, 201)
        self.assertTrue(generated["measurement"]["measurement_id"])
        self.assertTrue(generated["measurement"]["measured_at"].endswith("Z"))

        measurement = {
            "measurement_id": "scale-1-100",
            "device_id": "body-scale",
            "measured_at": "2026-09-05T08:00:00Z",
            "weight_g": 78350,
            "impedance_raw": 1344,
        }
        status, created = self.request("POST", "/api/measurements", measurement, api_headers)
        self.assertEqual(status, 201)
        self.assertTrue(created["created"])

        status, duplicate = self.request("POST", "/api/measurements", measurement, api_headers)
        self.assertEqual(status, 200)
        self.assertFalse(duplicate["created"])

        conflict = measurement | {"weight_g": 79000}
        status, _ = self.request("POST", "/api/measurements", conflict, api_headers)
        self.assertEqual(status, 409)

        status, listing = self.request("GET", "/api/measurements?limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(listing["total"], 2)
        self.assertIn(78100, [row["weight_g"] for row in listing["activity_measurements"]])
        self.assertCountEqual(
            [row["weight_g"] for row in listing["measurements"]], [78100, 78350]
        )
        saved = next(row for row in listing["measurements"] if row["measurement_id"] == "scale-1-100")
        self.assertEqual((saved["bmi"], saved["body_fat_pct_estimate"]), (24.2, 25.8))
        without_impedance = server.estimate_body_metrics(
            measurement | {"impedance_raw": 0}, TEST_PROFILE
        )
        female = server.estimate_body_metrics(measurement, TEST_PROFILE | {"sex": "female"})
        self.assertIsNone(without_impedance["body_fat_pct_estimate"])
        self.assertEqual(female["body_fat_pct_estimate"], 31.1)

        self.assertEqual(server.profile_age(TEST_PROFILE, datetime(2026, 5, 31, tzinfo=timezone.utc)), 35)
        self.assertEqual(server.profile_age(TEST_PROFILE, datetime(2026, 6, 1, tzinfo=timezone.utc)), 36)

    def test_activity_is_not_capped_by_history_limit(self):
        measured_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with server.connect_database() as database:
            database.executemany(
                "INSERT INTO measurements (measurement_id, device_id, measured_at, received_at, weight_g, impedance_raw) VALUES (?, ?, ?, ?, ?, ?)",
                ((f"weight-{index}", "body-scale", measured_at, measured_at, 78000, 0) for index in range(501)),
            )
        status, listing = self.request("GET", "/api/measurements?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["measurements"]), 1)
        self.assertEqual(len(listing["activity_measurements"]), 501)

    def test_guest_weights_do_not_affect_profile_readings(self):
        self.httpd.profile = TEST_PROFILE | {"weight_min_g": 75000, "weight_max_g": 85000}
        api_headers = {"X-API-Key": server.API_TOKEN}
        measured_at = datetime.now(timezone.utc) - timedelta(days=1)
        for index, (measurement_id, weight_g) in enumerate((
            ("mine", 78000), ("minimum", 75000), ("maximum", 85000),
            ("guest-light", 74999), ("guest-heavy", 85001),
        )):
            status, _ = self.request("POST", "/api/measurements", {
                "measurement_id": measurement_id,
                "device_id": "body-scale",
                "measured_at": (measured_at + timedelta(seconds=index)).isoformat(),
                "weight_g": weight_g,
                "impedance_raw": 1300,
            }, api_headers)
            self.assertEqual(status, 201)

        status, listing = self.request("GET", "/api/measurements?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(listing["measurements"][0]["measurement_id"], "maximum")
        self.assertEqual(listing["total"], 3)
        self.assertCountEqual(
            [row["weight_g"] for row in listing["activity_measurements"]],
            [78000, 75000, 85000],
        )
        status, listing = self.request("GET", "/api/measurements")
        self.assertEqual(status, 200)
        self.assertCountEqual(
            [row["measurement_id"] for row in listing["measurements"]],
            ["mine", "minimum", "maximum"],
        )
        self.assertNotIn("weight_min_g", listing)
        with server.connect_database() as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM measurements").fetchone()[0], 5)

        self.httpd.profile = TEST_PROFILE
        _, listing = self.request("GET", "/api/measurements")
        self.assertEqual(listing["total"], 5)

    def test_record_management_is_protected_and_deletion_is_reversible(self):
        api_headers = {"X-API-Key": server.API_TOKEN}
        measured_at = datetime.now(timezone.utc) - timedelta(days=1)
        records = [
            {"measurement_id": name, "device_id": "body-scale", "weight_g": weight,
             "impedance_raw": 1300, "measured_at": (measured_at + timedelta(seconds=index)).isoformat()}
            for index, (name, weight) in enumerate((("mine", 78000), ("guest", 60000)))
        ]
        for record in records:
            self.assertEqual(self.request("POST", "/api/measurements", record, api_headers)[0], 201)
        self.httpd.profile = TEST_PROFILE | {"weight_min_g": 75000, "weight_max_g": 85000}
        self.assertEqual(self.request("GET", "/api/measurements")[1]["total"], 1)
        self.assertEqual(self.request("GET", "/api/measurements?include_excluded=1")[0], 401)
        status, listing = self.request("GET", "/api/measurements?include_excluded=1", headers=api_headers)
        self.assertEqual((status, listing["total"]), (200, 2))

        payload = {"measurement_ids": ["guest", "guest"]}
        for action in ("delete", "restore"):
            for headers in (None, {"X-API-Key": "wrong"}, {"X-API-Key": "é"}):
                self.assertEqual(self.request("POST", f"/api/measurements/{action}", payload, headers)[0], 401)
        status, deleted = self.request("POST", "/api/measurements/delete", payload, api_headers)
        self.assertEqual((status, deleted), (200, {"deleted": 1, "measurement_ids": ["guest"]}))
        self.assertEqual(self.request("POST", "/api/measurements/delete", payload, api_headers)[1], {"deleted": 0, "measurement_ids": []})
        status, listing = self.request("GET", "/api/measurements?include_excluded=1", headers=api_headers)
        self.assertEqual((status, listing["total"]), (200, 1))
        self.assertEqual([row["measurement_id"] for row in listing["measurements"]], ["mine"])
        self.assertEqual([row["weight_g"] for row in listing["activity_measurements"]], [78000])
        with server.connect_database() as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM measurements").fetchone()[0], 2)
            self.assertIsNotNone(database.execute("SELECT deleted_at FROM measurements WHERE measurement_id = 'guest'").fetchone()[0])

        self.assertEqual(self.request("POST", "/api/measurements", records[1], api_headers)[0], 200)
        self.assertEqual(self.request("GET", "/api/measurements?include_excluded=1", headers=api_headers)[1]["total"], 1)
        self.assertEqual(self.request("POST", "/api/measurements/restore", payload, api_headers), (200, {"restored": 1, "measurement_ids": ["guest"]}))
        self.assertEqual(self.request("POST", "/api/measurements/restore", payload, api_headers)[1], {"restored": 0, "measurement_ids": []})
        self.assertEqual(self.request("GET", "/api/measurements?include_excluded=1", headers=api_headers)[1]["total"], 2)

        mine = {"measurement_ids": ["mine"]}
        self.assertEqual(self.request("POST", "/api/measurements/delete", mine, api_headers)[1]["deleted"], 1)
        _, listing = self.request("GET", "/api/measurements")
        self.assertEqual((listing["total"], listing["measurements"], listing["activity_measurements"]), (0, [], []))
        self.assertEqual(self.request("POST", "/api/measurements/restore", mine, api_headers)[1]["restored"], 1)
        self.assertEqual(self.request("GET", "/api/measurements")[1]["total"], 1)

        for invalid in ({}, {"measurement_ids": []}, {"measurement_ids": "guest"},
                        {"measurement_ids": [True]}, {"measurement_ids": [" "]},
                        {"measurement_ids": ["x" * 129]}, {"measurement_ids": ["guest\n"]},
                        {"measurement_ids": ["guest"], "all": True},
                        {"measurement_ids": ["guest"] * 501}):
            with self.subTest(payload=invalid):
                self.assertEqual(self.request("POST", "/api/measurements/delete", invalid, api_headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/measurements/delete", {"measurement_ids": ["missing"]}, api_headers)[1], {"deleted": 0, "measurement_ids": []})
        self.assertEqual(self.request("GET", "/api/measurements?include_excluded=1", headers=api_headers)[1]["total"], 2)

    def test_existing_database_migrates_without_losing_records(self):
        with patch.object(server, "DB_PATH", f"{self.temporary_directory.name}/legacy.db"):
            with server.connect_database() as database:
                database.execute("CREATE TABLE measurements (measurement_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, measured_at TEXT NOT NULL, received_at TEXT NOT NULL, weight_g INTEGER NOT NULL, impedance_raw INTEGER NOT NULL)")
                database.execute("INSERT INTO measurements VALUES (?, ?, ?, ?, ?, ?)", ("legacy", "body-scale", server.utc_now(), server.utc_now(), 78000, 0))
            server.initialize_database()
            server.initialize_database()
            with server.connect_database() as database:
                row = database.execute("SELECT * FROM measurements").fetchone()
                self.assertEqual(row["measurement_id"], "legacy")
                self.assertIsNone(row["deleted_at"])

    def test_profile_environment_and_early_response_connection_close(self):
        environment = {
            "PROFILE_NICKNAME": "测试用户",
            "PROFILE_HEIGHT_CM": "180",
            "PROFILE_BIRTH_YEAR": "1990",
            "PROFILE_BIRTH_MONTH": "6",
            "PROFILE_SEX": "male",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(server.load_profile(), TEST_PROFILE)
            with patch.dict(os.environ, {"PROFILE_WEIGHT_MIN_KG": "75.001", "PROFILE_WEIGHT_MAX_KG": "85"}):
                self.assertEqual(server.load_profile(), TEST_PROFILE | {"weight_min_g": 75001, "weight_max_g": 85000})
            with patch.dict(os.environ, {"PROFILE_WEIGHT_MIN_KG": "75"}):
                self.assertEqual(server.load_profile(), TEST_PROFILE | {"weight_min_g": 75000})
            with patch.dict(os.environ, {"PROFILE_WEIGHT_MAX_KG": "85"}):
                self.assertEqual(server.load_profile(), TEST_PROFILE | {"weight_max_g": 85000})
            with patch.dict(os.environ, {"PROFILE_WEIGHT_MIN_KG": "", "PROFILE_WEIGHT_MAX_KG": ""}):
                self.assertEqual(server.load_profile(), TEST_PROFILE)
            for value in ("bad", "NaN", "Infinity", "0", "500.001", "75.0001"):
                with self.subTest(value=value), patch.dict(os.environ, {"PROFILE_WEIGHT_MIN_KG": value}):
                    with self.assertRaises(SystemExit):
                        server.load_profile()
            with patch.dict(os.environ, {"PROFILE_WEIGHT_MIN_KG": "85", "PROFILE_WEIGHT_MAX_KG": "75"}):
                with self.assertRaises(SystemExit):
                    server.load_profile()

        request = (
            b"POST /not-found HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}"
            b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=2) as connection:
            connection.sendall(request)
            response = b""
            while chunk := connection.recv(4096):
                response += chunk

        self.assertTrue(response.startswith(b"HTTP/1.0 404"))
        self.assertEqual(response.count(b"HTTP/1.0"), 1)
        self.assertNotIn(b"501", response)


if __name__ == "__main__":
    unittest.main()
