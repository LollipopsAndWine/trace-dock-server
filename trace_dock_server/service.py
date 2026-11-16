import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .db import COUNTERS


class APIError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def require(condition, code="invalid_request", status=400):
    if not condition:
        raise APIError(status, code)


def now():
    return datetime.now(timezone.utc).isoformat()


def digest_json(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


SHA = re.compile(r"^[0-9a-f]{64}$")
CRC64 = re.compile(r"^(0|[1-9][0-9]{0,19})$")
ID = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
TOKEN_PATH = re.compile(r"^[A-Za-z0-9_-]{16,4096}$")
STATES = {
    "created": {"created", "collecting", "cancelled", "failed"},
    "collecting": {"collecting", "awaiting_upload_confirmation", "resumable", "cancelled", "failed"},
    "awaiting_upload_confirmation": {"awaiting_upload_confirmation", "uploading", "cancelled", "failed"},
    "uploading": {"uploading", "verifying", "resumable", "cancelled", "failed"},
    "verifying": {"verifying", "resumable", "completed", "failed"},
    "resumable": {"resumable", "collecting", "awaiting_upload_confirmation", "uploading", "verifying", "failed", "cancelled"},
    "completed": {"completed"}, "failed": {"failed"}, "cancelled": {"cancelled"},
}


class Service:
    def __init__(self, db, storage, pepper, rules=None):
        require(len(pepper) >= 32, "server_pepper_too_short", 500)
        self.db, self.storage, self.pepper = db, storage, pepper.encode()
        self.rules = rules or json.loads(Path(__file__).with_name("rules.json").read_text())

    def close(self):
        if hasattr(self.storage, "close"):
            self.storage.close()

    def authenticate(self, authorization):
        require(isinstance(authorization, str) and authorization.startswith("Bearer "), "unauthorized", 401)
        token = authorization[7:]
        require(TOKEN_PATH.fullmatch(token), "unauthorized", 401)
        hashed = hmac.new(self.pepper, token.encode(), hashlib.sha256).digest()
        with self.db.connect() as db:
            p = db.one("SELECT * FROM participants WHERE token_hash=? AND status='active'", (hashed,))
        require(p is not None, "unauthorized", 401)
        p["_token"] = token
        return p

    @staticmethod
    def object_prefix(p, row):
        created = row["created_at"]
        date = created.date().isoformat() if hasattr(created, "date") else str(created)[:10]
        return f"trajectory/token={p['_token']}/dt={date}/job={row['job_id']}/"

    def owned(self, db, job, participant):
        row = db.one("SELECT * FROM collections WHERE job_id=? AND participant_id=?", (job, participant), lock=True)
        require(row is not None, "job_not_found", 404)
        return row

    def verify_identity(self, p):
        name, domain = p["email"].rsplit("@", 1)
        return {"participant_id": p["participant_id"], "masked_email": name[:1] + "***@" + domain}

    def create(self, p, body, key):
        fields = {k: body.get(k) for k in ("client_operation_id", "device_id", "machine_name", "tool", "cli_version")}
        require(all(isinstance(v, str) and 0 < len(v) <= 256 for v in fields.values()))
        require(fields["tool"] in ("codex", "claude"), "single_tool_required")
        require(key == fields["client_operation_id"] and ID.fullmatch(key), "idempotency_key_required")
        fingerprint = digest_json(fields)
        job = secrets.token_hex(16)
        # ON CONFLICT and the unique constraint handle requests landing on two API nodes.
        with self.db.connect() as db:
            db.execute("""INSERT INTO collections(job_id,participant_id,device_id,machine_name,
                client_operation_id,create_fingerprint,rules_json,state,tool,created_at)
                VALUES(?,?,?,?,?,?,?,'created',?,?) ON CONFLICT(participant_id,client_operation_id) DO NOTHING""",
                (job, p["participant_id"], fields["device_id"], fields["machine_name"], key, fingerprint, json.dumps(self.rules), fields["tool"], now()))
            row = db.one("SELECT * FROM collections WHERE participant_id=? AND client_operation_id=?", (p["participant_id"], key), lock=True)
            require(row["create_fingerprint"] == fingerprint, "idempotency_conflict", 409)
        return {"job_id": row["job_id"], "rules": json.loads(row["rules_json"]), "object_prefix": self.object_prefix(p, row)}

    def patch(self, p, job, body):
        allowed = {"state", "selection_mode", "selected_count", "discovered_count", "consented_at", "upload_confirmed_at", "selection_sha256"}
        require(set(body) <= allowed, "unsupported_patch_field")
        with self.db.connect() as db:
            row = self.owned(db, job, p["participant_id"])
            target = body.get("state", row["state"])
            require(target in STATES[row["state"]] and target not in ("completed", "verifying"), "invalid_state_transition", 409)
            updates = dict(body)
            for k in ("selected_count", "discovered_count"):
                if k in updates:
                    require(type(updates[k]) is int and updates[k] >= 0)
                    require(row["state"] == "created" or updates[k] == row[k], "selection_frozen", 409)
            if "selection_mode" in body:
                require(body["selection_mode"] in ("all", "partial"))
                require(row["selection_mode"] is None or row["selection_mode"] == body["selection_mode"], "selection_frozen", 409)
            if "selection_sha256" in body:
                require(isinstance(body["selection_sha256"], str) and SHA.fullmatch(body["selection_sha256"]))
                require(row["selection_sha256"] is None or row["selection_sha256"] == body["selection_sha256"], "selection_frozen", 409)
            for k in ("consented_at", "upload_confirmed_at"):
                if k in body:
                    try:
                        parsed = datetime.fromisoformat(body[k].replace("Z", "+00:00"))
                        require(parsed.tzinfo is not None and parsed <= datetime.now(timezone.utc) + timedelta(minutes=5))
                    except (ValueError, TypeError, AttributeError):
                        raise APIError(400, "invalid_confirmation_time")
                    require(row[k] is None or iso(row[k]).replace("+00:00", "Z") == body[k].replace("+00:00", "Z"), "confirmation_frozen", 409)
            merged = row | updates
            require(merged["selected_count"] <= merged["discovered_count"])
            if target == "collecting":
                require(merged["consented_at"] and merged["selection_mode"] and merged["selected_count"] > 0 and merged["selection_sha256"], "consent_required", 409)
            if "upload_confirmed_at" in body:
                require(row["state"] in ("awaiting_upload_confirmation", "uploading", "resumable"), "collection_not_ready", 409)
            if target == "uploading":
                require(merged["consented_at"] and merged["upload_confirmed_at"] and row["state"] != "collecting", "upload_confirmation_required", 409)
            if updates:
                names = list(updates)
                db.execute("UPDATE collections SET " + ",".join(k + "=?" for k in names) + " WHERE job_id=?", tuple(updates[k] for k in names) + (job,))
        return {"job_id": job, "state": target}

    def credentials(self, p, job):
        with self.db.connect() as db:
            row = self.owned(db, job, p["participant_id"])
            require(row["upload_confirmed_at"] is not None and row["state"] in ("uploading", "resumable", "verifying"), "upload_confirmation_required", 409)
        return self.storage.credentials(job, self.object_prefix(p, row))

    def complete(self, p, job, body):
        key, crc64, size = body.get("manifest_key"), body.get("manifest_crc64"), body.get("manifest_size_bytes")
        with self.db.connect() as db:
            prefix_row = self.owned(db, job, p["participant_id"])
        prefix = self.object_prefix(p, prefix_row)
        require(key == prefix+"manifest.json" and isinstance(crc64, str) and CRC64.fullmatch(crc64) and int(crc64) <= 18446744073709551615 and type(size) is int and size > 0)
        with self.db.connect() as db:
            row = self.owned(db, job, p["participant_id"])
            require(row["upload_confirmed_at"] and row["state"] in ("uploading", "verifying", "resumable", "completed"), "upload_confirmation_required", 409)
            if row["manifest_crc64"] is not None:
                require(row["manifest_crc64"] == crc64 and row["manifest_size_bytes"] == size, "manifest_conflict", 409)
            if row["state"] == "completed":
                require(row["manifest_crc64"] is not None, "legacy_task_not_supported", 409)
                return self.receipt(row, prefix)
            db.execute("UPDATE collections SET state='verifying',manifest_key=?,manifest_crc64=?,manifest_size_bytes=? WHERE job_id=?", (key, crc64, size, job))
        try:
            manifest = self.storage.read_manifest(key, size, crc64)
            summary, objects = self.validate_manifest(manifest, row, prefix)
            total = size + sum(a["size_bytes"] for a in objects)
            for artifact in objects:
                self.storage.verify(artifact["object_key"], artifact["size_bytes"], artifact["crc64"])
        except (ValueError, KeyError, TypeError):
            with self.db.connect() as db:
                self.owned(db, job, p["participant_id"])
                db.execute("UPDATE collections SET state='resumable' WHERE job_id=? AND state='verifying'", (job,))
            raise APIError(422, "manifest_or_object_verification_failed")
        with self.db.connect() as db:
            current = self.owned(db, job, p["participant_id"])
            if current["state"] == "completed":
                return self.receipt(current, prefix)
            require(current["state"] == "verifying", "invalid_state_transition", 409)
            counts = {k: summary[k] for k in COUNTERS if k in summary and k not in ("discovered_count", "uploaded_bytes", "verified_object_count")}
            counts.update(uploaded_bytes=total, verified_object_count=len(objects) + 1)
            names = list(counts)
            db.execute("UPDATE collections SET state='completed',completed_at=?," + ",".join(k + "=?" for k in names) + " WHERE job_id=?", (now(),) + tuple(counts[k] for k in names) + (job,))
            return self.receipt(self.owned(db, job, p["participant_id"]), prefix)

    def validate_manifest(self, m, row, prefix):
        def check(condition):
            if not condition:
                raise ValueError("invalid_manifest")
        check(m["manifest_schema"] == "1.1" and m["job_id"] == row["job_id"] and m["participant_id"] == row["participant_id"])
        check(m["device_id"] == row["device_id"] and m["machine_name"] == row["machine_name"] and m["tool"] == row["tool"])
        check(m["object_prefix"] == prefix and m["object_prefix"].startswith("trajectory/token=") and m["object_prefix"].endswith("/"))
        check(m["rules"] == json.loads(row["rules_json"]) and m["selection_mode"] == row["selection_mode"])
        for field in ("consented_at", "upload_confirmed_at"):
            check(datetime.fromisoformat(m[field].replace("Z", "+00:00")) == datetime.fromisoformat(iso(row[field]).replace("Z", "+00:00")))
        selection = sorted(m["selection"])
        check(len(selection) == len(set(selection)) == row["selected_count"])
        selection_sha = hashlib.sha256(json.dumps(selection, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        check(selection_sha == m["selection_sha256"] == row["selection_sha256"])
        check(m["discovered_count"] == row["discovered_count"])
        check({s["session_id"] for s in m["sessions"]} == set(selection))
        objects = m["artifacts"]
        check(len(objects) <= 200000)
        ids, keys = set(), set()
        for a in objects:
            key = a["object_key"]
            check(isinstance(key, str) and key.startswith(prefix) and not any(x in key.split("/") for x in ("..", ".", "")) and "\\" not in key and "\x00" not in key)
            check(key not in keys and a["artifact_id"] not in ids and key != prefix+"manifest.json")
            check(a["kind"] in ("trajectory", "workspace", "exclusions", "large_files"))
            check(type(a["size_bytes"]) is int and a["size_bytes"] >= 0 and isinstance(a["crc64"], str) and CRC64.fullmatch(a["crc64"]) and int(a["crc64"]) <= 18446744073709551615)
            keys.add(key); ids.add(a["artifact_id"])
        check(sum(a["kind"] == "exclusions" for a in objects) == 1)
        sessions = []
        def visit(s):
            check(s["tool"] == row["tool"] and s["reversibility"] != "reconstructable")
            check(s["validity"] in ("valid", "invalid") and s["archive_eligibility"] in ("candidate", "archive_only", "excluded"))
            check(s.get("trajectory_artifact_id", "") in ids | {""} and s.get("workspace_artifact_id", "") in ids | {""})
            sessions.append(s)
            for c in s.get("children", []):
                visit(c)
        for s in m["sessions"]:
            visit(s)
        summary = m["summary"]
        check(all(type(v) is int and v >= 0 for k, v in summary.items() if k.endswith("count")))
        check(summary["selected_count"] == len(m["sessions"]))
        check(summary["subagent_count"] == len(sessions) - len(m["sessions"]))
        check(summary["workspace_snapshot_count"] == sum(a["kind"] == "workspace" for a in objects))
        check(summary["workspace_exists_count"] == sum(s["workspace"]["exists"] for s in m["sessions"]))
        check(summary["invalid_count"] == sum(s["validity"] == "invalid" for s in m["sessions"]))
        check(summary["archive_only_count"] == sum(s["archive_eligibility"] != "candidate" for s in m["sessions"]))
        check(summary["reversible_candidate_count"] == sum(s["archive_eligibility"] == "candidate" for s in m["sessions"]))
        check(summary["reconstructable_count"] == 0)
        return summary, objects

    @staticmethod
    def receipt(row, prefix):
        return {"job_id": row["job_id"], "participant_id": row["participant_id"], "state": "completed",
                "verification": "crc64_verified",
                "object_prefix": prefix, "manifest_key": row["manifest_key"], "manifest_crc64": row["manifest_crc64"],
                "uploaded_bytes": row["uploaded_bytes"], "verified_object_count": row["verified_object_count"],
                "completed_at": iso(row["completed_at"]), "summary": {k: row[k] for k in COUNTERS}}
