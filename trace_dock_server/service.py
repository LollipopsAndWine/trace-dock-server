import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import COUNTERS, Collection, Participant


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


def record(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


SHA = re.compile(r"^[0-9a-f]{64}$")
CRC64 = re.compile(r"^(0|[1-9][0-9]{0,19})$")
ID = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
TOKEN_PATH = re.compile(r"^[A-Za-z0-9_-]{16,4096}$")
STATES = {
    "created": {"created", "collecting", "cancelled", "failed"},
    "collecting": {"collecting", "awaiting_upload_confirmation", "resumable", "cancelled", "failed"},
    "awaiting_upload_confirmation": {"awaiting_upload_confirmation", "collecting", "uploading", "cancelled", "failed"},
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
        with self.db.session() as session:
            p = session.scalar(select(Participant).where(
                Participant.token_hash == hashed, Participant.status == "active"
            ))
        require(p is not None, "unauthorized", 401)
        return record(p)

    @staticmethod
    def object_prefix(p, row):
        created = row["created_at"]
        date = created.date().isoformat() if hasattr(created, "date") else str(created)[:10]
        return f"trajectory/participant_id={p['participant_id']}/dt={date}/job={row['job_id']}/"

    def owned(self, session, job, participant):
        row = session.scalar(select(Collection).where(
            Collection.job_id == job, Collection.participant_id == participant
        ).with_for_update())
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
        created_at = now()
        values = dict(
            job_id=job,
            participant_id=p["participant_id"],
            device_id=fields["device_id"],
            machine_name=fields["machine_name"],
            client_operation_id=key,
            create_fingerprint=fingerprint,
            rules_json=json.dumps(self.rules),
            state="created",
            tool=fields["tool"],
            created_at=created_at,
            updated_at=created_at,
        )
        with self.db.session() as session:
            insert = sqlite_insert if self.db.sqlite else pg_insert
            session.execute(insert(Collection).values(**values).on_conflict_do_nothing(
                index_elements=[Collection.participant_id, Collection.client_operation_id]
            ))
            row = session.scalar(select(Collection).where(
                Collection.participant_id == p["participant_id"], Collection.client_operation_id == key
            ).with_for_update())
            row = record(row)
            require(row["create_fingerprint"] == fingerprint, "idempotency_conflict", 409)
        return {"job_id": row["job_id"], "rules": json.loads(row["rules_json"]), "object_prefix": self.object_prefix(p, row)}

    def patch(self, p, job, body):
        allowed = {"state", "selection_mode", "selected_count", "discovered_count", "consented_at", "upload_confirmed_at", "selection_sha256"}
        require(set(body) <= allowed, "unsupported_patch_field")
        with self.db.session() as session:
            model = self.owned(session, job, p["participant_id"])
            row = record(model)
            target = body.get("state", row["state"])
            require(target in STATES[row["state"]] and target not in ("completed", "verifying"), "invalid_state_transition", 409)
            updates = dict(body)
            if target == "collecting" and row["state"] in ("awaiting_upload_confirmation", "resumable"):
                # Rebuilding an already selected collection requires a fresh
                # upload confirmation and a new manifest verification record.
                updates.update(
                    upload_confirmed_at=None,
                    manifest_key=None,
                    manifest_crc64=None,
                    manifest_size_bytes=None,
                    uploaded_bytes=0,
                    verified_object_count=0,
                )
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
                    if row[k] is not None:
                        # PostgreSQL returns aware datetime values in the
                        # session timezone; Go may also omit fractional zeros.
                        # Freeze the instant, not its textual representation.
                        frozen = row[k] if isinstance(row[k], datetime) else datetime.fromisoformat(row[k].replace("Z", "+00:00"))
                        require(frozen == parsed, "confirmation_frozen", 409)
                        updates[k] = row[k]
            merged = row | updates
            require(merged["selected_count"] <= merged["discovered_count"])
            if target == "collecting":
                require(merged["consented_at"] and merged["selection_mode"] and merged["selected_count"] > 0 and merged["selection_sha256"], "consent_required", 409)
            if "upload_confirmed_at" in body:
                require(row["state"] in ("awaiting_upload_confirmation", "uploading", "resumable"), "collection_not_ready", 409)
            if target == "uploading":
                require(merged["consented_at"] and merged["upload_confirmed_at"] and row["state"] != "collecting", "upload_confirmation_required", 409)
            for name, value in updates.items():
                setattr(model, name, value)
        return {"job_id": job, "state": target}

    def credentials(self, p, job):
        with self.db.session() as session:
            row = record(self.owned(session, job, p["participant_id"]))
            require(row["upload_confirmed_at"] is not None and row["state"] in ("uploading", "resumable", "verifying"), "upload_confirmation_required", 409)
        return self.storage.credentials(job, self.object_prefix(p, row))

    def complete(self, p, job, body):
        key, crc64, size = body.get("manifest_key"), body.get("manifest_crc64"), body.get("manifest_size_bytes")
        with self.db.session() as session:
            prefix_row = record(self.owned(session, job, p["participant_id"]))
        prefix = self.object_prefix(p, prefix_row)
        require(key == prefix+"manifest.json" and isinstance(crc64, str) and CRC64.fullmatch(crc64) and int(crc64) <= 18446744073709551615 and type(size) is int and size > 0)
        with self.db.session() as session:
            model = self.owned(session, job, p["participant_id"])
            row = record(model)
            require(row["upload_confirmed_at"] and row["state"] in ("uploading", "verifying", "resumable", "completed"), "upload_confirmation_required", 409)
            if row["manifest_crc64"] is not None:
                require(row["manifest_crc64"] == crc64 and row["manifest_size_bytes"] == size, "manifest_conflict", 409)
            if row["state"] == "completed":
                require(row["manifest_crc64"] is not None, "legacy_task_not_supported", 409)
                return self.receipt(row, prefix)
            model.state = "verifying"
            model.manifest_key = key
            model.manifest_crc64 = crc64
            model.manifest_size_bytes = size
        try:
            manifest = self.storage.read_manifest(key, size, crc64)
            summary, objects = self.validate_manifest(manifest, row, prefix)
            total = size + sum(a["size_bytes"] for a in objects)
            for artifact in objects:
                self.storage.verify(artifact["object_key"], artifact["size_bytes"], artifact["crc64"])
        except (ValueError, KeyError, TypeError):
            with self.db.session() as session:
                model = self.owned(session, job, p["participant_id"])
                if model.state == "verifying":
                    model.state = "resumable"
            raise APIError(422, "manifest_or_object_verification_failed")
        with self.db.session() as session:
            current_model = self.owned(session, job, p["participant_id"])
            current = record(current_model)
            if current["state"] == "completed":
                return self.receipt(current, prefix)
            require(current["state"] == "verifying", "invalid_state_transition", 409)
            counts = {k: summary[k] for k in COUNTERS if k in summary and k not in ("discovered_count", "uploaded_bytes", "verified_object_count")}
            counts.update(uploaded_bytes=total, verified_object_count=len(objects) + 1)
            current_model.state = "completed"
            current_model.completed_at = now()
            for name, value in counts.items():
                setattr(current_model, name, value)
            session.flush()
            return self.receipt(record(current_model), prefix)

    def validate_manifest(self, m, row, prefix):
        def check(condition):
            if not condition:
                raise ValueError("invalid_manifest")
        check(m["manifest_schema"] == "1.2" and m["job_id"] == row["job_id"] and m["participant_id"] == row["participant_id"])
        check(m["device_id"] == row["device_id"] and m["machine_name"] == row["machine_name"] and m["tool"] == row["tool"])
        check(m["object_prefix"] == prefix and m["object_prefix"].startswith(f"trajectory/participant_id={row['participant_id']}/") and m["object_prefix"].endswith("/"))
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
