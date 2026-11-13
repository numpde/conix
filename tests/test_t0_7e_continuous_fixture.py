"""Acceptance-only source projection for the real continuous dapp32 phase."""

from contextlib import asynccontextmanager
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from conix.catalog import prepare_catalog
import generic_acceptance
from generic_acceptance import catalog_path, stage_continuous_inputs
from fixture_protocol import parse_phase_failure, phase_failure_record


class ContinuousDapp32SourceContracts(unittest.IsolatedAsyncioTestCase):
    def test_stage_three_real_abi_selections_without_schema_owner(self):
        descriptor = {
            "chainId": 31337,
            "rpcUrl": "http://anvil:8545",
            "contracts": {"PayToPlay": {
                "address": "0x" + "11" * 20, "deploymentBlock": 1,
            }},
        }
        tokens = {
            "token20": {"address": "0x" + "22" * 20},
            "token721": {"address": "0x" + "33" * 20},
        }
        original = catalog_path(descriptor, "0x" + "44" * 32, tokens)
        runtime_dsn = "postgresql://runtime:disposable@postgres/conix_test"

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            source = stage_continuous_inputs(original, runtime_dsn, target)
            projected = prepare_catalog(target / "config.toml")

            self.assertEqual(projected.source_id, source.source_id)
            self.assertEqual(
                {selected.alias for selected in projected.contracts},
                {"payment", "token20", "token721"},
            )
            self.assertEqual(
                {path.name for path in (target / "abi").iterdir()},
                {"payment.json", "token20.json", "token721.json"},
            )
            self.assertEqual(
                (target / "connection.dsn").read_text(), runtime_dsn + "\n",
            )
            self.assertEqual(
                stat.S_IMODE((target / "connection.dsn").stat().st_mode), 0o600,
            )
            self.assertFalse((target / "owner.dsn").exists())
            self.assertNotIn("disposable", (target / "config.toml").read_text())
            self.assertEqual(set(path.name for path in target.iterdir()), {
                "abi", "config.toml", "connection.dsn",
            })


    async def test_primary_reader_failure_survives_independent_cleanup_failures(self):
        """The real continuous entrypoint must preserve both observed outcomes."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            abi = target / "abi"
            abi.mkdir()
            member = abi / "token.json"
            member.write_bytes(b"[]")
            (target / "config.toml").write_text("temporary configuration")
            (target / "connection.dsn").write_text("disposable runtime credential")

            primary = RuntimeError("postgresql://private:do-not-report@db/secret")
            attempted = []
            unlink = Path.unlink

            def unlink_with_failure(path, *args, **kwargs):
                attempted.append(path.name)
                if path == member:
                    raise PermissionError("cleanup contains secret credentials")
                return unlink(path, *args, **kwargs)

            @asynccontextmanager
            async def rejected_reader(**_kwargs):
                raise primary
                yield

            with (
                patch.object(generic_acceptance, "Path",
                             side_effect=lambda text: target if text == "/run/conix" else Path(text)),
                patch.object(generic_acceptance, "stage_continuous_inputs",
                             return_value=SimpleNamespace()),
                patch("conix.reader.open_reader", side_effect=rejected_reader),
                patch.object(Path, "unlink", autospec=True, side_effect=unlink_with_failure),
            ):
                with self.assertRaises(RuntimeError) as caught:
                    await generic_acceptance.continuous(
                        {"rpcUrl": "http://unreachable.invalid"}, Path("/tmp/source.toml"),
                        AsyncMock(), tokens={}, writer_dsn="test-runtime", reader_dsn="test-reader",
                    )

            self.assertIs(caught.exception, primary)
            self.assertIn("token.json", attempted)
            self.assertIn("config.toml", attempted)
            self.assertIn("connection.dsn", attempted)
            self.assertFalse((target / "config.toml").exists())
            self.assertFalse((target / "connection.dsn").exists())
            note = " ".join(getattr(primary, "__notes__", []))
            self.assertIn("cleanup unconfirmed", note.lower())
            self.assertIn("ABI", note)
            public = parse_phase_failure(phase_failure_record("continuous", primary), phase="continuous")
            self.assertIn("RuntimeError", public)
            self.assertIn("cleanup unconfirmed", public.lower())
            self.assertNotIn("do-not-report", public)
            self.assertNotIn("secret credentials", public)

    async def test_process_retirement_failure_does_not_replace_primary_fault(self):
        """Even child retirement refusal cannot hide an RPC failure."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "abi").mkdir()
            (target / "abi" / "payment.json").write_bytes(b"[]")
            (target / "config.toml").write_text("temporary configuration")
            (target / "connection.dsn").write_text("runtime credential")

            class Reader:
                async def checkpoint(self):
                    return SimpleNamespace(block=SimpleNamespace(number=1), generation="generation")

                async def events(self, **_kwargs):
                    return SimpleNamespace(
                        complete=True,
                        events=[SimpleNamespace(transaction_hash=b"x" * 32)],
                    )

            @asynccontextmanager
            async def selected_reader(**_kwargs):
                yield Reader()

            class Child:
                returncode = None
                killed = False
                reaped = False

                def kill(self):
                    self.killed = True
                    raise PermissionError("sensitive process retirement output")

                async def communicate(self):
                    self.reaped = True
                    return b"", b""

            child = Child()
            primary = RuntimeError("source RPC private detail")
            injected_rpc = AsyncMock(side_effect=primary)

            with (
                patch.object(generic_acceptance, "Path",
                             side_effect=lambda text: target if text == "/run/conix" else Path(text)),
                patch.object(generic_acceptance, "stage_continuous_inputs",
                             return_value=SimpleNamespace(limits=SimpleNamespace(max_page_size=10))),
                patch("conix.reader.open_reader", side_effect=selected_reader),
                patch.object(generic_acceptance.asyncio, "create_subprocess_exec",
                             return_value=child),
            ):
                with self.assertRaises(RuntimeError) as caught:
                    await generic_acceptance.continuous(
                        {"rpcUrl": "http://unreachable.invalid"}, Path("/tmp/source.toml"),
                        injected_rpc, tokens={}, writer_dsn="test-runtime", reader_dsn="test-reader",
                    )

            self.assertIs(caught.exception, primary)
            self.assertTrue(child.killed)
            self.assertTrue(child.reaped)
            self.assertFalse((target / "abi").exists())
            self.assertFalse((target / "config.toml").exists())
            self.assertFalse((target / "connection.dsn").exists())
            public = parse_phase_failure(phase_failure_record("continuous", primary), phase="continuous")
            self.assertIn("RuntimeError", public)
            self.assertIn("process retirement", public)
            self.assertNotIn("sensitive", public)


if __name__ == "__main__":
    unittest.main()
