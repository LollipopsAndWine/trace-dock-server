from contextlib import redirect_stderr, redirect_stdout
import io
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import SecretStr
from sqlalchemy import func, select

from trace_dock_server.db import Database
from trace_dock_server.models import Participant
from trace_dock_server import participants
from trace_dock_server.service import record
from trace_dock_server.service import Service
from trace_dock_server.token_security import recover_email


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
            issued_json = output.getvalue()
            with db.session() as session:
                if action == "issue":
                    participant_id = json.loads(issued_json)["participant_id"]
                    row = session.get(Participant, participant_id)
                else:
                    row = session.scalar(select(Participant).where(
                        Participant.email == "test@example.com"
                    ).order_by(Participant.created_at.desc(), Participant.participant_id.desc()))
                row = record(row)
            return row, issued_json

        first, issued = run("issue")
        first_token = json.loads(issued)["token"]
        self.assertEqual(recover_email(first_token, "p" * 32), "test@example.com")
        self.assertEqual(Service(db, object(), "p" * 32).authenticate("Bearer " + first_token)["participant_id"], first["participant_id"])
        self.assertEqual(first["updated_at"], first["created_at"])
        reissued, issued_again = run("issue")
        self.assertNotEqual(first["participant_id"], reissued["participant_id"])
        self.assertEqual(first["email"], reissued["email"])
        self.assertNotEqual(first["token_hash"], reissued["token_hash"])
        self.assertNotEqual(json.loads(issued)["token"], json.loads(issued_again)["token"])
        with db.session() as session:
            records = [record(row) for row in session.scalars(
                select(Participant).where(Participant.email == "test@example.com")
            )]
        self.assertEqual(len(records), 2)
        self.assertEqual({row["status"] for row in records}, {"active"})
        self.assertEqual(len({row["participant_id"] for row in records}), 2)
        run("revoke")
        with db.session() as session:
            active_count = session.scalar(select(func.count()).select_from(Participant).where(
                Participant.email == "test@example.com", Participant.status == "active"
            ))
            revoked_rows = [record(row) for row in session.scalars(
                select(Participant).where(Participant.email == "test@example.com")
            )]
        self.assertEqual(active_count, 0)
        self.assertEqual(len(revoked_rows), 2)
        self.assertTrue(all(row["status"] == "revoked" and row["revoked_at"] for row in revoked_rows))
        activated, _ = run("issue")
        self.assertEqual(activated["status"], "active")
        self.assertIsNone(activated["revoked_at"])
        self.assertNotIn(activated["participant_id"], {first["participant_id"], reissued["participant_id"]})


if __name__ == "__main__":
    unittest.main()
