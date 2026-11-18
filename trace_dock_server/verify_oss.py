"""Verify the configured STS role and OSS v2 read/write/delete path."""
import datetime
import io
import uuid

import alibabacloud_oss_v2 as oss

from .storage import OSS
from .settings import get_settings


def main():
    settings = get_settings()
    participant_id = "healthcheck-" + uuid.uuid4().hex
    job_id = "healthcheck-" + uuid.uuid4().hex
    prefix = f"trajectory/participant_id={participant_id}/dt={datetime.datetime.now(datetime.timezone.utc).date().isoformat()}/job={job_id}/"
    key = prefix + "healthcheck-" + uuid.uuid4().hex + ".txt"
    payload = b"trace-dock oss sdk v2 healthcheck\n"
    storage = OSS(settings)
    credentials = storage.credentials(job_id, prefix)
    cfg = oss.config.load_default()
    cfg.region = credentials["region"]
    cfg.endpoint = credentials["endpoint"]
    cfg.credentials_provider = oss.credentials.StaticCredentialsProvider(
        credentials["access_key_id"], credentials["access_key_secret"], credentials["security_token"])
    client = oss.Client(cfg)
    uploaded = False
    try:
        client.put_object(oss.PutObjectRequest(
            bucket=credentials["bucket"], key=key, body=io.BytesIO(payload),
            metadata={"trace-dock-healthcheck": "true"}))
        uploaded = True
        head = client.head_object(oss.HeadObjectRequest(bucket=credentials["bucket"], key=key))
        if head.content_length != len(payload):
            raise RuntimeError("OSS healthcheck size mismatch")
        result = client.get_object(oss.GetObjectRequest(bucket=credentials["bucket"], key=key))
        try:
            data = b"".join(result.body.iter_bytes())
        finally:
            result.body.close()
        if data != payload:
            raise RuntimeError("OSS healthcheck body mismatch")
        print(f"OSS SDK v2 healthcheck passed: bucket={credentials['bucket']} prefix={prefix} object_deleted=true")
    finally:
        if uploaded:
            client.delete_object(oss.DeleteObjectRequest(bucket=credentials["bucket"], key=key))


if __name__ == "__main__":
    main()
