import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.console import router
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
        return SimpleNamespace(data=self.data)


class FakeClient:
    def __init__(self):
        self.auth = SimpleNamespace(
            admin=SimpleNamespace(
                list_users=lambda page=1, per_page=1000: SimpleNamespace(
                    users=[
                        SimpleNamespace(
                            id="user-1",
                            email="dongjun@gmail.com",
                            created_at="2026-09-01T00:00:00+00:00",
                            last_sign_in_at="2026-09-08T00:00:00+00:00",
                        )
                    ]
                )
            )
        )
        self.tables = {
            "profiles": [
                {
                    "id": "user-1",
                    "username": "박동준",
                    "created_at": "2026-09-01T00:00:00+00:00",
                }
            ],
            "trips": [
                {
                    "id": "trip-1",
                    "user_id": "user-1",
                    "title": "오사카",
                    "destination": "오사카, 일본",
                    "status": "planning",
                    "travel_intensity": 3,
                }
            ],
            "api_request_logs": [
                {
                    "user_id": "user-1",
                    "created_at": "2026-09-08T00:10:00+00:00",
                    "endpoint": "/me",
                    "status_code": 200,
                    "latency_ms": 40,
                }
            ],
            "activity_logs": [
                {
                    "id": "activity-1",
                    "user_id": "user-1",
                    "trip_id": "trip-1",
                    "event_type": "trip.create",
                    "created_at": "2026-09-08T00:00:00+00:00",
                },
                {
                    "id": "activity-2",
                    "user_id": "user-1",
                    "event_type": "feedback.submit",
                    "metadata": {"sentiment": "positive"},
                    "created_at": "2026-09-08T00:05:00+00:00",
                }
            ],
        }

    def table(self, name):
        return FakeQuery(self.tables[name])


class ConsoleApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = FakeClient()
        self.auth_client = FakeClient()
        self.auth_client.tables["profiles"][0]["is_admin"] = True
        self.app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            id="user-1", email="dongjun@gmail.com", token="test-user-token"
        )

    @patch("app.routers.console.get_service_client")
    @patch("app.routers.dashboard.get_user_client")
    def test_user_list_supports_search_and_counts(self, get_user_client, get_client):
        get_client.return_value = self.client
        get_user_client.return_value = self.auth_client
        response = TestClient(self.app).get(
            "/admin/console/users?search=dongjun",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["username"], "박동준")
        self.assertEqual(payload["items"][0]["trip_count"], 1)
        self.assertEqual(payload["items"][0]["request_count"], 1)

    @patch("app.routers.console.get_service_client")
    @patch("app.routers.dashboard.get_user_client")
    def test_user_detail_returns_trips_and_logs(self, get_user_client, get_client):
        get_client.return_value = self.client
        get_user_client.return_value = self.auth_client
        response = TestClient(self.app).get(
            "/admin/console/users/user-1",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["trips"][0]["title"], "오사카")
        self.assertEqual(payload["recent_requests"][0]["endpoint"], "/me")
        self.assertEqual(payload["recent_activities"][0]["event_type"], "trip.create")

    @patch("app.routers.console.get_service_client")
    @patch("app.routers.dashboard.get_user_client")
    def test_feedback_returns_aggregate_only(self, get_user_client, get_client):
        get_client.return_value = self.client
        get_user_client.return_value = self.auth_client
        response = TestClient(self.app).get(
            "/admin/console/feedback",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["feedback_count"], 1)
        self.assertEqual(payload["positive_count"], 1)
        self.assertEqual(payload["pace_count"], 1)
        self.assertNotIn("metadata", payload)

    @patch("app.routers.console.get_service_client")
    @patch("app.routers.dashboard.get_user_client")
    def test_system_status_uses_recent_request_window_shape(self, get_user_client, get_client):
        get_client.return_value = self.client
        get_user_client.return_value = self.auth_client
        response = TestClient(self.app).get(
            "/admin/console/system-status",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total_requests"], 1)
        self.assertEqual(len(payload["services"]), 4)
        self.assertTrue(any(item["request_count"] == 1 for item in payload["services"]))

    def test_missing_login_is_rejected(self):
        self.app.dependency_overrides.clear()
        response = TestClient(self.app).get("/admin/console/users")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
