"""Durable journal observations remain independent and read-only."""

import unittest
from unittest.mock import Mock, patch

import history_observations


class RetainedHistoryObservationContracts(unittest.TestCase):
    def test_snapshot_reads_retained_source_and_log_identities_without_write(self):
        dsn = "postgresql://runtime@postgres/conix"
        state = (11, 11, "generation-1", "source-1", None)
        blocks = [(1, "hash1", "parent1"), (2, "hash2", "hash1")]
        events = [(1, "transaction1", 0), (2, "transaction2", 0)]
        connection = Mock()
        connection.execute.side_effect = [
            Mock(fetchone=Mock(return_value=state)),
            Mock(fetchall=Mock(return_value=blocks)),
            Mock(fetchall=Mock(return_value=events)),
        ]
        with patch.object(history_observations.psycopg, "connect") as connect:
            connect.return_value.__enter__.return_value = connection
            actual = history_observations.snapshot(dsn + "\n")
        self.assertEqual(connect.call_args.args, (dsn,))
        self.assertEqual(connect.call_args.kwargs["options"], "-c default_transaction_read_only=on")
        self.assertEqual(actual, {
            "height": 11, "revision": 11, "generation": "generation-1",
            "source_id": "source-1", "blocked": None,
            "blocks": blocks, "events": events,
        })
        self.assertEqual(len(connection.execute.call_args_list), 3)
        for call in connection.execute.call_args_list:
            self.assertTrue(call.args[0].lstrip().startswith("SELECT"))
        self.assertTrue(any("source_progress" in call.args[0] for call in connection.execute.call_args_list))
        self.assertTrue(any("conix.blocks" in call.args[0] for call in connection.execute.call_args_list))
        self.assertTrue(any("conix.events" in call.args[0] for call in connection.execute.call_args_list))


if __name__ == "__main__":
    unittest.main()
