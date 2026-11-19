import hashlib
import hmac
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from trace_dock_server.db import Database
from trace_dock_server.models import Collection, Participant, User
from trace_dock_server.service import APIError, Service, now, record


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
        with self.db.session() as session:
            created_at = now()
            session.add(User(email="person@example.com", created_at=created_at))
            session.flush()
            session.add(Participant(
                participant_id="participant", email="person@example.com", token_hash=hashed,
                status="active", created_at=created_at,
            ))
        self.p = self.service.authenticate("Bearer " + token)
        self.body = dict(client_operation_id="operation", device_id="device", machine_name="machine", tool="codex", cli_version="0.1")

    def create(self):
        result = self.service.create(self.p, self.body, "operation")
        self.prefix = result["object_prefix"]
        expected = f"trajectory/participant_id={self.p['participant_id']}/dt="
        if not self.prefix.startswith(expected) or "/token=" in self.prefix:
            raise AssertionError(f"unsafe object prefix: {self.prefix}")
        return result["job_id"]

    def read_collection(self, job):
        with self.db.session() as session:
            return record(session.get(Collection, job))

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
        manifest = dict(manifest_schema="1.2", job_id=job, participant_id="participant", device_id="device", machine_name="machine", tool="codex", rules=self.service.rules, object_prefix=self.prefix, selection_mode="partial", selection=selection, selection_sha256=selection_sha, discovered_count=2, consented_at=consent, upload_confirmed_at=confirmation,
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

    def test_object_prefix_uses_stable_participant_id_not_bearer_token(self):
        first = self.service.create(self.p, self.body, "operation")
        prefix = first["object_prefix"]
        self.assertTrue(prefix.startswith("trajectory/participant_id=participant/dt="))
        self.assertTrue(prefix.endswith("/job=" + first["job_id"] + "/"))
        self.assertNotIn("random-high-entropy-test-token", prefix)

        other_token = "another-high-entropy-test-token"
        other_hash = hmac.new(b"p" * 32, other_token.encode(), hashlib.sha256).digest()
        with self.db.session() as session:
            session.get(Participant, "participant").token_hash = other_hash
        rotated = self.service.authenticate("Bearer " + other_token)
        retried = self.service.create(rotated, self.body, "operation")
        self.assertEqual(retried["object_prefix"], prefix)

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

    def test_confirmation_retries_compare_instants_and_preserve_frozen_values(self):
        job, _, _, _ = self.prepare_upload()
        for stored, submitted in (
            ("2000-01-02T13:39:48.137736+08:00", "2000-01-02T05:39:48.137736Z"),
            ("2000-01-02T05:39:48.123400+00:00", "2000-01-02T05:39:48.1234Z"),
            ("2000-01-02T05:39:48.000000+00:00", "2000-01-02T05:39:48Z"),
            ("2000-01-02T05:39:48Z", "2000-01-02T13:39:48+08:00"),
        ):
            with self.subTest(stored=stored, submitted=submitted):
                with self.db.session() as session:
                    model = session.get(Collection, job)
                    model.consented_at = stored
                    model.upload_confirmed_at = stored
                before = self.read_collection(job)
                for _ in range(2):
                    result = self.service.patch(self.p, job, {
                        "state": "uploading", "consented_at": submitted, "upload_confirmed_at": submitted,
                    })
                    self.assertEqual(result["state"], "uploading")
                    self.assertEqual(self.read_collection(job), before)

    def test_confirmation_retry_accepts_postgres_datetime_values(self):
        job, _, _, _ = self.prepare_upload()
        stored = datetime(2000, 1, 2, 13, 39, 48, 123400, tzinfo=timezone(timedelta(hours=8)))
        submitted = "2000-01-02T05:39:48.1234Z"
        original_owned = self.service.owned

        def postgres_row(session, job_id, participant):
            row = original_owned(session, job_id, participant)
            row.consented_at = stored
            row.upload_confirmed_at = stored
            return row

        with patch.object(self.service, "owned", side_effect=postgres_row):
            result = self.service.patch(self.p, job, {
                "state": "uploading", "consented_at": submitted, "upload_confirmed_at": submitted,
            })
        self.assertEqual(result["state"], "uploading")

    def test_changed_confirmation_time_is_still_rejected(self):
        job, _, _, _ = self.prepare_upload()
        frozen = "2000-01-02T05:39:48.137736Z"
        with self.db.session() as session:
            model = session.get(Collection, job)
            model.consented_at = frozen
            model.upload_confirmed_at = frozen
        before = self.read_collection(job)
        for field in ("consented_at", "upload_confirmed_at"):
            with self.subTest(field=field):
                with self.assertRaises(APIError) as error:
                    self.service.patch(self.p, job, {field: "2000-01-02T13:39:48.137737+08:00"})
                self.assertEqual((error.exception.status, error.exception.code), (409, "confirmation_frozen"))
                self.assertEqual(self.read_collection(job), before)

    def test_resuming_collection_reopens_upload_confirmation(self):
        job = self.create()
        self.service.patch(self.p, job, {"discovered_count": 2})
        consent = now()
        selection_sha = hashlib.sha256(b'["s"]').hexdigest()
        frozen = {
            "selection_mode": "partial",
            "selected_count": 1,
            "consented_at": consent,
            "selection_sha256": selection_sha,
        }
        self.service.patch(self.p, job, {"state": "collecting", **frozen})
        self.service.patch(self.p, job, {"state": "awaiting_upload_confirmation"})
        with self.db.session() as session:
            model = session.get(Collection, job)
            model.upload_confirmed_at = now()
            model.manifest_key = self.prefix + "manifest.json"
            model.manifest_crc64 = "123"
            model.manifest_size_bytes = 10
            model.uploaded_bytes = 20
            model.verified_object_count = 2

        self.service.patch(self.p, job, {"state": "collecting", **frozen})
        row = self.read_collection(job)
        self.assertEqual(row["state"], "collecting")
        self.assertIsNone(row["upload_confirmed_at"])
        self.assertIsNone(row["manifest_key"])
        self.assertIsNone(row["manifest_crc64"])
        self.assertIsNone(row["manifest_size_bytes"])
        self.assertEqual(row["uploaded_bytes"], 0)
        self.assertEqual(row["verified_object_count"], 0)

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

    def test_receipt_is_identical_for_database_timezone_representations(self):
        job, _, _, completion = self.prepare_upload()
        first = self.service.complete(self.p, job, completion)
        stored = self.read_collection(job)
        completed = datetime.fromisoformat(first["completed_at"])
        for offset in (timedelta(hours=8), timedelta(hours=-5, minutes=-30)):
            local = completed.astimezone(timezone(offset))
            for value in (local, local.isoformat()):
                with self.subTest(offset=offset, value_type=type(value).__name__):
                    receipt = self.service.receipt(stored | {"completed_at": value}, self.prefix)
                    self.assertEqual(receipt, first)
                    self.assertTrue(receipt["completed_at"].endswith("+00:00"))

    def test_invalid_manifest_time_types_are_recoverable(self):
        job, _, original, completion = self.prepare_upload()
        manifest_key = completion["manifest_key"]
        valid_manifest = json.loads(original)

        def reopen():
            self.service.patch(self.p, job, {"state": "collecting"})
            self.service.patch(self.p, job, {"state": "awaiting_upload_confirmation"})
            self.service.patch(self.p, job, {
                "state": "uploading",
                "upload_confirmed_at": valid_manifest["upload_confirmed_at"],
            })

        first = True
        for field in ("consented_at", "upload_confirmed_at"):
            for value in (123, None, [], {}):
                with self.subTest(field=field, value=value):
                    if not first:
                        reopen()
                    first = False
                    manifest = json.loads(original)
                    manifest[field] = value
                    body = json.dumps(manifest).encode()
                    self.store.objects[manifest_key] = body
                    with self.assertRaises(APIError) as error:
                        self.service.complete(self.p, job, completion | {"manifest_size_bytes": len(body)})
                    self.assertEqual(
                        (error.exception.status, error.exception.code),
                        (422, "manifest_or_object_verification_failed"),
                    )
                    stored = self.read_collection(job)
                    self.assertEqual(stored["state"], "resumable")
                    self.assertIsNone(stored["completed_at"])

        reopen()
        self.store.objects[manifest_key] = original
        self.assertEqual(self.service.complete(self.p, job, completion)["state"], "completed")

    def test_old_token_prefix_manifest_protocol_is_rejected(self):
        job, _, body, completion = self.prepare_upload()
        manifest = json.loads(body)
        manifest["manifest_schema"] = "1.1"
        changed = json.dumps(manifest).encode()
        self.store.objects[self.prefix + "manifest.json"] = changed
        completion["manifest_size_bytes"] = len(changed)
        with self.assertRaises(APIError) as error:
            self.service.complete(self.p, job, completion)
        self.assertEqual((error.exception.status, error.exception.code), (422, "manifest_or_object_verification_failed"))

    def test_updated_at_preserves_creation_and_idempotent_retries(self):
        job = self.create()
        initial = self.read_collection(job)
        self.assertEqual(initial["updated_at"], initial["created_at"])
        self.assertEqual(self.create(), job)
        self.assertEqual(self.read_collection(job), initial)


if __name__ == "__main__":
    unittest.main()
