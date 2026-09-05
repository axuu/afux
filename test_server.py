import base64
import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection

import server


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        server.DB_PATH = f"{self.temporary_directory.name}/measurements.db"
        server.API_TOKEN = "test-api-token-with-safe-length"
        server.WEB_USERNAME = "admin"
        server.WEB_PASSWORD = "test-web-password"
        server.initialize_database()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.ScaleHandler)
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

    def test_authenticated_round_trip_and_idempotency(self):
        status, _ = self.request("GET", "/api/measurements")
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

        credentials = base64.b64encode(b"admin:test-web-password").decode()
        status, listing = self.request(
            "GET", "/api/measurements?limit=10", headers={"Authorization": f"Basic {credentials}"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(listing["total"], 2)
        self.assertCountEqual(
            [row["weight_g"] for row in listing["measurements"]], [78100, 78350]
        )


if __name__ == "__main__":
    unittest.main()
