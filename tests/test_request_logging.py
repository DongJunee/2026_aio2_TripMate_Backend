import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.request_logging import ApiRequestLoggingMiddleware


class RequestLoggingTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.add_middleware(ApiRequestLoggingMiddleware)

        @self.app.get("/health")
        def health():
            return {"status": "ok"}

        @self.app.get("/trips/{trip_id}")
        def trip(trip_id: str):
            return {"trip_id": trip_id}

        @self.app.get("/failure")
        def failure():
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail="bad request")

    @patch("app.request_logging.write_api_request_log")
    def test_success_request_is_logged(self, write_log):
        response = TestClient(self.app).get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["X-Request-ID"])
        write_log.assert_called_once()
        log = write_log.call_args.args[0]
        self.assertEqual(log["method"], "GET")
        self.assertEqual(log["endpoint"], "/health")
        self.assertEqual(log["status_code"], 200)
        self.assertIsNone(log["error_type"])

    @patch("app.request_logging.write_api_request_log")
    def test_error_request_is_logged_as_failure(self, write_log):
        response = TestClient(self.app).get("/failure")

        self.assertEqual(response.status_code, 400)
        log = write_log.call_args.args[0]
        self.assertEqual(log["status_code"], 400)
        self.assertEqual(log["error_type"], "http_4xx")

    @patch("app.request_logging.write_api_request_log")
    def test_trip_id_is_collected_from_path(self, write_log):
        trip_id = "11111111-1111-4111-8111-111111111111"
        response = TestClient(self.app).get(f"/trips/{trip_id}")

        self.assertEqual(response.status_code, 200)
        log = write_log.call_args.args[0]
        self.assertEqual(log["trip_id"], trip_id)

    @patch("app.request_logging.write_api_request_log")
    def test_swagger_assets_are_not_logged(self, write_log):
        response = TestClient(self.app).get("/docs")

        self.assertEqual(response.status_code, 200)
        write_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
