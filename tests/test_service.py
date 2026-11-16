import hashlib
import hmac
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from trace_dock_server.db import Database
from trace_dock_server.service import APIError, Service, now


class Store:
    def __init__(self):
        self.objects = {}
        self.calls = 0
        self.verified = []

    def credentials(self, job, prefix):
        self.calls += 1
        return {"prefix": prefix}

    def read_manifest(self, key, size, crc64):
        body = self.objects[key]
        if len(body) != size:
            raise ValueError("manifest_checksum")
        return json.loads(body)

    def verify(self, key, size, crc64):
        self.verified.append(key)
        body = self.objects[key]
        if body == b"tampered" or len(body) != size:
            raise ValueError("object_checksum")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database("sqlite:///" + self.temp.name + "/test.db", allow_sqlite=True)
        self.db.initialize()
        self.store = Store()
        self.service = Service(self.db, self.store, "p" * 32)
        self.addCleanup(self.service.close)
        token = "random-high-entropy-test-token"
        hashed = hmac.new(b"p" * 32, token.encode(), hashlib.sha256).digest()
        with self.db.connect() as db:
            db.execute("INSERT INTO participants(participant_id,email,token_hash,status,created_at) VALUES(?,?,?,'active',?)", ("participant", "person@example.com", hashed, now()))
        self.p = self.service.authenticate("Bearer " + token)
        self.body = dict(client_operation_id="operation", device_id="device", machine_name="machine", tool="codex", cli_version="0.1")

    def create(self):
        result = self.service.create(self.p, self.body, "operation")
        self.prefix = result["object_prefix"]
        return result["job_id"]

    def prepare_upload(self):
        job = self.create()
        self.service.patch(self.p, job, {"discovered_count": 2})
        selection = ["s"]
        selection_sha = hashlib.sha256(b'["s"]').hexdigest()
        consent = now()
        self.service.patch(self.p, job, {"state": "collecting", "consented_at": consent, "selection_mode": "partial", "selected_count": 1, "selection_sha256": selection_sha})
        self.service.patch(self.p, job, {"state": "awaiting_upload_confirmation"})
        confirmation = now()
        self.service.patch(self.p, job, {"state": "uploading", "upload_confirmed_at": confirmation})
        key = self.prefix + "exclusions.jsonl"
        self.store.objects[key] = b""
        manifest = dict(manifest_schema="1.1", job_id=job, participant_id="participant", device_id="device", machine_name="machine", tool="codex", rules=self.service.rules, object_prefix=self.prefix, selection_mode="partial", selection=selection, selection_sha256=selection_sha, discovered_count=2, consented_at=consent, upload_confirmed_at=confirmation,
                        sessions=[dict(tool="codex", session_id="s", reversibility="not_reconstructable", validity="invalid", archive_eligibility="archive_only", workspace={"exists": False})],
                        artifacts=[dict(artifact_id="exclusions", kind="exclusions", object_key=key, size_bytes=0, crc64="0")],
                        summary=dict(selected_count=1, subagent_count=0, workspace_exists_count=0, workspace_snapshot_count=0, reversible_candidate_count=0, reconstructable_count=0, archive_only_count=1, invalid_count=1, excluded_count=0, large_file_count=0, large_file_missing_remote_path_count=0, large_file_missing_remote_path_ratio=None))
        body = json.dumps(manifest).encode()
        manifest_key = self.prefix + "manifest.json"
        self.store.objects[manifest_key] = body
        completion = dict(manifest_key=manifest_key, manifest_crc64="0", manifest_size_bytes=len(body))
        return job, key, body, completion

    def test_auth_revocation_and_ownership(self):
        with self.assertRaises(APIError) as error:
            self.service.authenticate("Bearer invalid-token-value")
        self.assertEqual(error.exception.status, 401)
        job = self.create()
        with self.assertRaises(APIError) as error:
            self.service.credentials({"participant_id": "other"}, job)
        self.assertEqual(error.exception.status, 404)

    def test_create_concurrent_idempotency_and_single_tool(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(lambda _: self.create(), range(8)))
        self.assertEqual(len(set(jobs)), 1)
        with self.assertRaises(APIError):
            self.service.create(self.p, self.body | {"tool": "claude"}, "operation")

    def test_consent_gates_and_frozen_selection(self):
        job = self.create()
        with self.assertRaises(APIError):
            self.service.credentials(self.p, job)
        self.service.patch(self.p, job, {"discovered_count": 3})
        self.service.patch(self.p, job, {"state": "collecting", "consented_at": now(), "selection_mode": "partial", "selected_count": 1, "selection_sha256": "a" * 64})
        self.service.patch(self.p, job, {"state": "awaiting_upload_confirmation"})
        self.service.patch(self.p, job, {"state": "uploading", "upload_confirmed_at": now()})
        self.service.credentials(self.p, job)
        self.assertEqual(self.store.calls, 1)

    def test_complete_uses_head_style_crc64_checks_and_is_idempotent(self):
        job, key, body, completion = self.prepare_upload()
        self.store.objects[key] = b"tampered"
        with self.assertRaises(APIError) as error:
            self.service.complete(self.p, job, completion)
        self.assertEqual(error.exception.status, 422)
        self.store.objects[key] = b""
        self.store.verified.clear()
        first = self.service.complete(self.p, job, completion)
        second = self.service.complete(self.p, job, completion)
        self.assertEqual(first, second)
        self.assertEqual(first["verification"], "crc64_verified")
        self.assertEqual(first["manifest_crc64"], "0")
        self.assertEqual(self.store.verified, [key])
        with self.assertRaises(APIError) as error:
            self.service.complete(self.p, job, completion | {"manifest_crc64": "1"})
        self.assertEqual(error.exception.status, 409)

    def test_updated_at_preserves_creation_and_idempotent_retries(self):
        job = self.create()
        with self.db.connect() as db:
            initial = self.service.owned(db, job, "participant")
        self.assertEqual(initial["updated_at"], initial["created_at"])
        self.assertEqual(self.create(), job)
        with self.db.connect() as db:
            self.assertEqual(self.service.owned(db, job, "participant"), initial)


if __name__ == "__main__":
    unittest.main()
