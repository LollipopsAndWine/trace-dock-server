import unittest

import httpx

from trace_dock_server.http import create_app
from trace_dock_server.service import APIError


class FakeService:
    def authenticate(self, authorization):
        if authorization != "Bearer valid-token":
            raise APIError(401, "unauthorized")
        return {"participant_id": "participant"}

    def verify_identity(self, participant):
        return {"participant_id": participant["participant_id"], "masked_email": "p***@example.com"}

    def create(self, participant, body, key):
        return {"job_id": "job", "operation_id": key, "tool": body.get("tool")}

    def patch(self, participant, job, body):
        return {"job_id": job, "state": body.get("state", "created")}

    def credentials(self, participant, job):
        return {"job_id": job}

    def complete(self, participant, job, body):
        return {"job_id": job, "state": "completed"}


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service=FakeService(), offload_service=False)),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_health_and_cache_headers(self):
        response = await self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_cli_routes_preserve_json_contract(self):
        headers = {"Authorization": "Bearer valid-token"}
        verify = await self.client.post("/v1/identity/verify", headers=headers, json={})
        created = await self.client.post(
            "/v1/collections",
            headers=headers | {"Idempotency-Key": "operation"},
            json={"tool": "codex"},
        )
        self.assertEqual(verify.status_code, 200)
        self.assertEqual(verify.json()["masked_email"], "p***@example.com")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["job_id"], "job")

    async def test_auth_errors_body_limits_and_unknown_paths(self):
        unauthorized = await self.client.post("/v1/identity/verify", json={})
        too_large = await self.client.post(
            "/v1/collections",
            headers={"Authorization": "Bearer valid-token"},
            content=b"x" * 65537,
        )
        missing = await self.client.get("/not-found")
        query = await self.client.get("/healthz?token=should-not-be-accepted")
        self.assertEqual((unauthorized.status_code, unauthorized.json()), (401, {"code": "unauthorized"}))
        self.assertEqual((too_large.status_code, too_large.json()), (413, {"code": "request_too_large"}))
        self.assertEqual((missing.status_code, missing.json()), (404, {"code": "not_found"}))
        self.assertEqual((query.status_code, query.json()), (404, {"code": "not_found"}))


if __name__ == "__main__":
    unittest.main()
