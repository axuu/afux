import json
import os
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timezone
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
        self.assertCountEqual(
            [row["weight_g"] for row in listing["measurements"]], [78100, 78350]
        )

        self.assertEqual(server.profile_age(TEST_PROFILE, datetime(2026, 5, 31, tzinfo=timezone.utc)), 35)
        self.assertEqual(server.profile_age(TEST_PROFILE, datetime(2026, 6, 1, tzinfo=timezone.utc)), 36)

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
