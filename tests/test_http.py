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
        return {
            "job_id": "job",
            "object_prefix": "trajectory/participant_id=participant/job=job/",
            "rules": {
                "classification_version": "1.0.0",
                "secret_version": "2.2.0",
                "large_file_version": "1.2.0",
                "large_file_threshold_bytes": 1073741824,
                "whitelist": [],
                "blacklist": [],
                "secret_patterns": [],
            },
        }

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

    async def test_openapi_documents_request_models_and_bearer_auth(self):
        response = await self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        document = response.json()
        self.assertIn("/docs", (await self.client.get("/docs")).text)
        self.assertEqual(document["components"]["securitySchemes"]["HTTPBearer"]["scheme"], "bearer")
        self.assertIn("API 进程", document["components"]["schemas"]["HealthResponse"]["properties"]["status"]["description"])
        verify_operation = document["paths"]["/v1/identity/verify"]["post"]
        self.assertEqual(verify_operation["summary"], "验证参与者身份")
        self.assertNotIn("requestBody", verify_operation)
        self.assertIn("脱敏后的邮箱", document["components"]["schemas"]["IdentityResponse"]["properties"]["masked_email"]["description"])
        schema = document["components"]["schemas"]["CreateCollectionRequest"]
        self.assertEqual(
            set(schema["properties"]),
            {"client_operation_id", "device_id", "machine_name", "tool", "cli_version"},
        )
        self.assertEqual(schema["examples"][0]["tool"], "codex")
        patch_schema = document["components"]["schemas"]["CollectionPatchRequest"]
        self.assertEqual(len(patch_schema["examples"]), 3)
        complete_schema = document["components"]["schemas"]["CompleteCollectionRequest"]
        self.assertEqual(complete_schema["examples"][0]["manifest_size_bytes"], 12345)
        idempotency = document["paths"]["/v1/collections"]["post"]["parameters"][0]
        self.assertEqual(idempotency["schema"]["examples"][0], "01JCREATEOPERATION000000000000")
        self.assertEqual(document["paths"]["/v1/collections"]["post"]["requestBody"]["required"], True)
        self.assertEqual(document["paths"]["/v1/collections/{job_id}"]["patch"]["requestBody"]["required"], True)

    async def test_cli_routes_preserve_json_contract(self):
        headers = {"Authorization": "Bearer valid-token"}
        verify = await self.client.post("/v1/identity/verify", headers=headers)
        created = await self.client.post(
            "/v1/collections",
            headers=headers | {"Idempotency-Key": "operation"},
            json={
                "client_operation_id": "operation",
                "device_id": "device",
                "machine_name": "machine",
                "tool": "codex",
                "cli_version": "0.1",
            },
        )
        self.assertEqual(verify.status_code, 200)
        self.assertEqual(verify.json()["masked_email"], "p***@example.com")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["job_id"], "job")

    async def test_invalid_model_input_keeps_stable_error_contract(self):
        response = await self.client.post(
            "/v1/collections",
            headers={"Authorization": "Bearer valid-token", "Idempotency-Key": "operation"},
            json={"tool": "codex"},
        )
        self.assertEqual((response.status_code, response.json()), (400, {"code": "invalid_request"}))

    async def test_auth_errors_body_limits_and_unknown_paths(self):
        unauthorized = await self.client.post("/v1/identity/verify")
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
