"""Alibaba Cloud official OSS Python SDK v2 and STS SDK integration."""
import json
import threading
import alibabacloud_oss_v2 as oss
from alibabacloud_oss_v2.transport import RequestsHttpClient
from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401 import models as sts_models
from alibabacloud_tea_openapi import models as open_api_models
from .settings import Settings

READ_BLOCK_SIZE = 1024 * 1024


class OSS:
    def __init__(self, settings: Settings):
        self.endpoint = settings.oss_endpoint.rstrip("/")
        self.read_endpoint = (settings.oss_read_endpoint or settings.oss_endpoint).rstrip("/")
        self.bucket = settings.oss_bucket
        self.ak = settings.aliyun_access_key_id.get_secret_value()
        self.sk = settings.aliyun_access_key_secret.get_secret_value()
        self.role = settings.oss_role_arn
        self.region = settings.aliyun_region
        self._local = threading.local()
        self._transports = []
        self._transport_lock = threading.Lock()

    def credentials(self, job, prefix):
        policy = {"Version": "1", "Statement": [{
            "Effect": "Allow", "Action": ["oss:PutObject", "oss:GetObject", "oss:ListParts", "oss:AbortMultipartUpload"],
            "Resource": [f"acs:oss:*:*:{self.bucket}/{prefix}*"],
        }]}
        config = open_api_models.Config(
            access_key_id=self.ak,
            access_key_secret=self.sk,
            region_id=self.region,
            endpoint=f"sts.{self.region}.aliyuncs.com",
        )
        response = StsClient(config).assume_role(sts_models.AssumeRoleRequest(
            role_arn=self.role,
            role_session_name=f"trace-dock-{job}",
            duration_seconds=3600,
            policy=json.dumps(policy, separators=(",", ":")),
        ))
        c = response.body.credentials
        return {"endpoint": self.endpoint, "bucket": self.bucket, "prefix": prefix, "region": self.region,
                "access_key_id": c.access_key_id, "access_key_secret": c.access_key_secret,
                "security_token": c.security_token, "expiration": c.expiration, "path_style": False}

    def _client(self, access_key_id, access_key_secret, security_token):
        cfg = oss.config.load_default()
        cfg.region = self.region
        cfg.endpoint = self.read_endpoint
        cfg.credentials_provider = oss.credentials.StaticCredentialsProvider(
            access_key_id, access_key_secret, security_token)
        # One session per worker: reuse connections without sharing a mutable
        # requests.Session between threads. Close sessions after worker shutdown.
        cfg.http_client = RequestsHttpClient()
        with self._transport_lock:
            self._transports.append(cfg.http_client)
        return oss.Client(cfg)

    def _read_client(self):
        if not hasattr(self._local, "client"):
            self._local.client = self._client(self.ak, self.sk, None)
        return self._local.client

    def close(self):
        with self._transport_lock:
            transports, self._transports = self._transports, []
        for transport in transports:
            transport.close()

    def _read(self, key, limit=None):
        # Server credentials are long-lived RAM credentials and are only used by SDK v2.
        client = self._read_client()
        result = client.get_object(oss.GetObjectRequest(bucket=self.bucket, key=key))
        try:
            data = bytearray()
            for chunk in result.body.iter_bytes(block_size=READ_BLOCK_SIZE):
                if limit is not None and len(data) + len(chunk) > limit:
                    raise ValueError("object_size")
                data.extend(chunk)
        finally:
            result.body.close()
        return data

    def read_manifest(self, key, size, digest):
        if not 0 < size <= 32 * 1024 * 1024:
            raise ValueError("manifest_size")
        content_length, remote_crc64 = self.head(key)
        if content_length != size or remote_crc64 != digest:
            raise ValueError("manifest_checksum")
        data = self._read(key, size + 1)
        if len(data) != size:
            raise ValueError("manifest_checksum")
        return json.loads(data)

    def head(self, key):
        client = self._read_client()
        result = client.head_object(oss.HeadObjectRequest(bucket=self.bucket, key=key))
        return result.content_length, result.hash_crc64

    def verify(self, key, size, crc64):
        content_length, remote_crc64 = self.head(key)
        if content_length != size or remote_crc64 != crc64:
            raise ValueError("object_checksum")
