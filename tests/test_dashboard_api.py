import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.dashboard import router
from app.deps import CurrentUser, get_current_user


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

    def eq(self, *_args):
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
        self.auth_client = FakeClient({"profiles": [{"id": "admin-1", "is_admin": True}]})
        self.app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            id="admin-1", email="admin@example.com", token="test-user-token"
        )

    @patch("app.routers.dashboard.get_service_client")
    @patch("app.routers.dashboard.get_user_client")
    def test_summary_returns_kpis_and_endpoint_stats(self, get_user_client, get_client):
        get_client.return_value = self.client
        get_user_client.return_value = self.auth_client
        response = TestClient(self.app).get("/admin/dashboard/summary")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["kpis"]["user_signup_count"], 2)
        self.assertEqual(payload["kpis"]["total_requests"], 2)
        self.assertEqual(payload["kpis"]["success_count"], 1)
        self.assertEqual(payload["kpis"]["failure_count"], 1)
        self.assertEqual(payload["kpis"]["error_rate_percent"], 50.0)
        self.assertEqual(payload["endpoint_usage"][0]["endpoint"], "/health")

    @patch("app.routers.dashboard.get_service_client")
    @patch("app.routers.dashboard.get_user_client")
    def test_errors_returns_recent_failures(self, get_user_client, get_client):
        get_client.return_value = self.client
        get_user_client.return_value = self.auth_client
        response = TestClient(self.app).get("/admin/dashboard/errors")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["items"][0]["status_code"], 500)

    def test_missing_login_is_rejected(self):
        self.app.dependency_overrides.clear()
        response = TestClient(self.app).get("/admin/dashboard/summary")

        self.assertEqual(response.status_code, 401)

    @patch("app.routers.dashboard.get_user_client")
    @patch("app.routers.dashboard.get_service_client")
    def test_non_admin_profile_is_rejected(self, get_client, get_user_client):
        get_client.return_value = self.client
        get_user_client.return_value = FakeClient(
            {"profiles": [{"id": "admin-1", "is_admin": False}]}
        )
        response = TestClient(self.app).get("/admin/dashboard/summary")

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
