from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import SecretStr

from trace_dock_server.db import Database
from trace_dock_server import participants


class ParticipantTimestampTests(unittest.TestCase):
    def test_issue_reissue_and_revoke_refresh_updated_at(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        settings = SimpleNamespace(
            database_url=SecretStr("sqlite:///" + temp.name + "/test.db"),
            token_pepper=SecretStr("p" * 32), trace_dock_dev=True,
        )
        db = Database(settings.database_url.get_secret_value(), allow_sqlite=True)

        def run(action):
            output = io.StringIO()
            with patch.object(participants, "get_settings", return_value=settings), \
                    patch("sys.argv", ["trace-dock-participants", action, "test@example.com"]), \
                    patch.object(participants, "now", return_value="2000-01-01T00:00:00+00:00"), \
                    redirect_stdout(output), redirect_stderr(io.StringIO()):
                participants.main()
            with db.connect() as conn:
                row = conn.one("SELECT * FROM participants WHERE email=?", ("test@example.com",))
            return row, output.getvalue()

        first, issued = run("issue")
        self.assertEqual(first["updated_at"], first["created_at"])
        reissued, issued_again = run("issue")
        self.assertEqual(first["participant_id"], reissued["participant_id"])
        self.assertEqual(first["created_at"], reissued["created_at"])
        self.assertNotEqual(first["token_hash"], reissued["token_hash"])
        self.assertNotEqual(json.loads(issued)["token"], json.loads(issued_again)["token"])
        self.assertGreater(datetime.fromisoformat(reissued["updated_at"].replace("Z", "+00:00")), datetime.fromisoformat(first["updated_at"]))
        revoked, _ = run("revoke")
        self.assertEqual(revoked["status"], "revoked")
        self.assertIsNotNone(revoked["revoked_at"])
        self.assertEqual(revoked["created_at"], first["created_at"])
        self.assertGreaterEqual(datetime.fromisoformat(revoked["updated_at"].replace("Z", "+00:00")), datetime.fromisoformat(reissued["updated_at"].replace("Z", "+00:00")))
        activated, _ = run("issue")
        self.assertEqual(activated["status"], "active")
        self.assertIsNone(activated["revoked_at"])
        self.assertEqual(activated["created_at"], first["created_at"])


if __name__ == "__main__":
    unittest.main()
