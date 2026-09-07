import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.dashboard import router


class FakeQuery:
    def __init__(self, data):
        self.data = data

    def select(self, *_args):
        return self

    def gte(self, *_args):
        return self

    def lt(self, *_args):
        return self

    def range(self, *_args):
        return self

    def execute(self):
        return type("Result", (), {"data": self.data})()


class FakeClient:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return FakeQuery(self.tables[name])


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = FakeClient(
            {
                "profiles": [
                    {"created_at": "2026-09-07T09:00:00+09:00"},
                    {"created_at": "2026-09-07T10:00:00+09:00"},
                ],
                "api_request_logs": [
                    {
                        "created_at": "2026-09-07T09:10:00+09:00",
                        "request_id": "req-1",
                        "method": "GET",
                        "endpoint": "/health",
                        "status_code": 200,
                        "latency_ms": 100,
                        "error_type": None,
                        "user_id": "user-1",
                        "trip_id": None,
                        "model": None,
                    },
                    {
                        "created_at": "2026-09-07T09:20:00+09:00",
                        "request_id": "req-2",
                        "method": "GET",
                        "endpoint": "/health",
                        "status_code": 500,
                        "latency_ms": 300,
                        "error_type": "http_5xx",
                        "user_id": "user-1",
                        "trip_id": None,
                        "model": None,
                    },
                ],
            }
        )

    def headers(self):
        return {"X-Admin-Token": "test-admin-token"}

    @patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "test-admin-token"}, clear=False)
    @patch("app.routers.dashboard.get_service_client")
    def test_summary_returns_kpis_and_endpoint_stats(self, get_client):
        get_client.return_value = self.client
        response = TestClient(self.app).get("/admin/dashboard/summary", headers=self.headers())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["kpis"]["user_signup_count"], 2)
        self.assertEqual(payload["kpis"]["total_requests"], 2)
        self.assertEqual(payload["kpis"]["success_count"], 1)
        self.assertEqual(payload["kpis"]["failure_count"], 1)
        self.assertEqual(payload["kpis"]["error_rate_percent"], 50.0)
        self.assertEqual(payload["endpoint_usage"][0]["endpoint"], "/health")

    @patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "test-admin-token"}, clear=False)
    @patch("app.routers.dashboard.get_service_client")
    def test_errors_returns_recent_failures(self, get_client):
        get_client.return_value = self.client
        response = TestClient(self.app).get("/admin/dashboard/errors", headers=self.headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["items"][0]["status_code"], 500)

    @patch.dict(
        os.environ,
        {"DASHBOARD_ADMIN_TOKEN": "test-admin-token", "DASHBOARD_AUTH_DISABLED": "false"},
        clear=False,
    )
    def test_missing_admin_token_is_rejected(self):
        response = TestClient(self.app).get("/admin/dashboard/summary")

        self.assertEqual(response.status_code, 401)

    @patch.dict(
        os.environ,
        {"DASHBOARD_AUTH_DISABLED": "true"},
        clear=False,
    )
    @patch("app.routers.dashboard.get_service_client")
    def test_test_mode_allows_access_without_admin_token(self, get_client):
        get_client.return_value = self.client
        response = TestClient(self.app).get("/admin/dashboard/summary")

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
