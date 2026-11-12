"""Contracts for the acceptance observer, separate from the indexer itself."""

import unittest
from unittest.mock import Mock, patch

import deployment_probe as probe


class DeploymentProbeContracts(unittest.TestCase):
    """Submission and credential-file formatting must not distort observations."""

    def test_submitted_transaction_is_observed_without_resubmission(self):
        """Anvil may acknowledge submission before its receipt is available."""
        transaction = "0x" + "ab" * 32
        receipt = {"status": "0x1", "logs": [{}], "blockNumber": "0x4"}
        receipts = iter((None, receipt))

        def rpc(method, params):
            if method == "eth_accounts":
                return ["0x" + "22" * 20]
            if method == "eth_sendTransaction":
                return transaction
            if method == "eth_getTransactionReceipt":
                return next(receipts)
            return None

        with patch.object(probe, "rpc", side_effect=rpc) as calls, patch.object(probe.time, "sleep"):
            result = probe.produce(0)
        self.assertEqual(result, {"height": 4, "transaction": transaction})
        self.assertEqual(sum(call.args[0] == "eth_sendTransaction" for call in calls.call_args_list), 1)
        self.assertEqual(sum(call.args[0] == "eth_getTransactionReceipt" for call in calls.call_args_list), 2)

    def test_newline_terminated_dsn_is_used_in_a_read_only_transaction(self):
        """Normal credential files end in a newline, outside the connection URI."""
        dsn = "postgresql://fixture@postgres/conix_rpc_test"
        db = Mock()
        db.execute.side_effect = [
            Mock(fetchone=Mock(return_value=(4, 4, "generation", "source", None))),
            Mock(fetchall=Mock(return_value=[])), Mock(fetchall=Mock(return_value=[])),
        ]
        with patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value = db
            result = probe.snapshot(dsn + "\n")
        self.assertEqual(connect.call_args.args, (dsn,))
        self.assertEqual(connect.call_args.kwargs["options"], "-c default_transaction_read_only=on")
        self.assertEqual(result["height"], 4)
