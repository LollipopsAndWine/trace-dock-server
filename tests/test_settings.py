import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from trace_dock_server.settings import Settings


ENV_CONTENT = """\
TRACE_DOCK_DEV=0
TOKEN_PEPPER=abcdefghijklmnopqrstuvwxyz1234567890
DATABASE_URL=postgresql://user:password@db.example/trace_dock
HTTP_HOST=127.0.0.1
HTTP_PORT=8080
OSS_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
OSS_BUCKET=private-bucket
OSS_ROLE_ARN=acs:ram::123456789012:role/trace-dock
ALIYUN_REGION=cn-hangzhou
ALIYUN_ACCESS_KEY_ID=access-key-id
ALIYUN_ACCESS_KEY_SECRET=access-key-secret
OSS_READ_ENDPOINT=https://oss-cn-hangzhou-internal.aliyuncs.com
"""


class SettingsTests(unittest.TestCase):
    def test_env_file_loads_and_process_environment_overrides_it(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / "server.env"
            env_file.write_text(ENV_CONTENT, encoding="utf-8")
            with patch.dict(os.environ, {
                "DATABASE_URL": "sqlite:////tmp/test-server.db",
                "TRACE_DOCK_DEV": "1",
                "HTTP_PORT": "9090",
            }, clear=True):
                settings = Settings(_env_file=env_file)
        self.assertTrue(settings.trace_dock_dev)
        self.assertEqual(settings.database_url.get_secret_value(), "sqlite:////tmp/test-server.db")
        self.assertEqual(settings.http_port, 9090)
        self.assertEqual(settings.oss_read_endpoint, "https://oss-cn-hangzhou-internal.aliyuncs.com")
        self.assertNotIn("access-key-secret", repr(settings))

    def test_sqlite_requires_explicit_development_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / "server.env"
            env_file.write_text(ENV_CONTENT.replace(
                "DATABASE_URL=postgresql://user:password@db.example/trace_dock",
                "DATABASE_URL=sqlite:////tmp/test-server.db",
            ), encoding="utf-8")
            with patch.dict(os.environ, {"TRACE_DOCK_DEV": "0"}, clear=True):
                with self.assertRaises(ValidationError):
                    Settings(_env_file=env_file)

    def test_oss_read_endpoint_requires_https(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / "server.env"
            env_file.write_text(ENV_CONTENT.replace(
                "OSS_READ_ENDPOINT=https://oss-cn-hangzhou-internal.aliyuncs.com",
                "OSS_READ_ENDPOINT=http://oss-cn-hangzhou-internal.aliyuncs.com",
            ), encoding="utf-8")
            with self.assertRaises(ValidationError):
                Settings(_env_file=env_file)


if __name__ == "__main__":
    unittest.main()
