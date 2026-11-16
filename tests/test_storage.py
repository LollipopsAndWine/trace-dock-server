import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import SecretStr

from trace_dock_server.storage import OSS


class StorageTests(unittest.TestCase):
    def settings(self):
        return SimpleNamespace(
            oss_endpoint="https://public.example",
            oss_read_endpoint="https://internal.example",
            oss_bucket="bucket",
            aliyun_access_key_id=SecretStr("access-key"),
            aliyun_access_key_secret=SecretStr("secret-key"),
            oss_role_arn="role",
            aliyun_region="cn-hangzhou",
        )

    def test_read_endpoint_is_separate_from_cli_upload_endpoint(self):
        storage = OSS(self.settings())
        with patch("trace_dock_server.storage.oss.Client", side_effect=lambda config: config):
            client_config = storage._client("id", "secret", None)
        credentials = SimpleNamespace(access_key_id="temp-id", access_key_secret="temp-secret", security_token="temp-token", expiration="later")
        response = SimpleNamespace(body=SimpleNamespace(credentials=credentials))
        with patch("trace_dock_server.storage.StsClient") as sts_client:
            sts_client.return_value.assume_role.return_value = response
            cli_credentials = storage.credentials("job", "prefix")
        self.assertEqual(client_config.endpoint, "https://internal.example")
        self.assertEqual(cli_credentials["endpoint"], "https://public.example")
        storage.close()

    def test_verify_uses_head_object_without_get_object(self):
        storage = OSS(self.settings())
        storage.head = lambda key: (123, "456")
        storage.verify("object", 123, "456")
        with self.assertRaises(ValueError):
            storage.verify("object", 124, "456")
        with self.assertRaises(ValueError):
            storage.verify("object", 123, "457")
        storage.close()


if __name__ == "__main__":
    unittest.main()
