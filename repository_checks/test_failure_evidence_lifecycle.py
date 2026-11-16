"""Run the real operator CLI against an Engine that erases removed child logs.

A real child supplies failing process output. Only the external Docker CLI is
replaced; diagnostic admission, retention, teardown and rendering stay real.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

IDENTITY = "a" * 64
IMAGE = "sha256:" + "b" * 64
DIAGNOSTIC = (b'CONIX-FAILURE-V1 {"phase":"advance","cause":"StorageUnavailable",'
              b'"effect":"unconfirmed"}\n')
PRIVATE = b"postgresql://owner:PRIVATE_RETIREMENT_SECRET@postgres/database"
ROOT = Path(__file__).resolve().parents[1]

ENGINE = r'''#!/usr/bin/env python3
import json
from pathlib import Path
import sys
root=Path(__file__).parent
path=root/'engine.json'
state=json.loads(path.read_text())
args=sys.argv[1:]
if 'context' in args and 'ls' in args:
    print('selected\tunix:///selected')
    sys.exit(0)
if 'container' in args:
    action=args[args.index('container')+1]
elif 'ps' in args:
    action='ps'
else:
    raise SystemExit(97)
with (root/'effects').open('a') as effects:
    effects.write(action+'\n')
if action=='ps':
    if state['present']:print('a'*64)
elif action=='inspect':
    print(json.dumps({'Id':'a'*64,'Image':'sha256:'+'b'*64,'RestartCount':0,
      'Config':{'Labels':{'com.docker.compose.project':'foreign' if state['foreign'] else 'conix-pilot',
                          'com.docker.compose.service':'indexer'}},
      'State':{'Running':False,'ExitCode':17,'OOMKilled':False,
               'StartedAt':'2026-11-16T12:00:00Z','FinishedAt':'2026-11-16T12:00:01Z'}}))
elif action=='logs':
    if state['log_failure'] or not state['present']:raise SystemExit(9)
    sys.stdout.buffer.write((root/'child.stdout').read_bytes())
    sys.stderr.buffer.write((root/'child.stderr').read_bytes())
elif action=='stop':
    pass
elif action=='rm':
    if state['remove_failure']:raise SystemExit(13)
    state['present']=False
    for name in ('child.stdout','child.stderr'):(root/name).unlink()
    path.write_text(json.dumps(state))
else:
    raise SystemExit(97)
'''


class FailureEvidenceLifecycleContracts(unittest.TestCase):
    def run_lane(self, root, *, producer=DIAGNOSTIC, **changed):
        """Execute the public recovery command with a stateful external Engine."""
        state=dict(present=True, foreign=False, log_failure=False, remove_failure=False)
        state.update(changed)
        (root/'engine.json').write_text(json.dumps(state))
        fake=root/'docker'
        fake.write_text(ENGINE)
        fake.chmod(0o755)
        child=subprocess.run(
            [sys.executable,'-c','import sys; sys.stderr.buffer.write(sys.stdin.buffer.read()); sys.exit(17)'],
            input=PRIVATE+b'\n'+producer,capture_output=True,timeout=5,
        )
        self.assertEqual(child.returncode,17)
        (root/'child.stdout').write_bytes(child.stdout)
        (root/'child.stderr').write_bytes(child.stderr)
        launcher=(
            'from pathlib import Path; from tools.containers import operator,named; '
            f'operator.ROOT=Path({str(root)!r}); named.ROOT=operator.ROOT; '
            'named.DEPLOYMENTS=operator.ROOT/"config/deployments"; operator.main()'
        )
        environment=dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'])
        result=subprocess.run(
            [sys.executable,'-c',launcher,'--context','selected','--origin','command line',
             '--deployment','pilot','deployment/down'],
            cwd=ROOT,env=environment,capture_output=True,text=True,timeout=10,
        )
        effects=(root/'effects').read_text().splitlines()
        return result,effects,json.loads((root/'engine.json').read_text())

    def test_failed_child_diagnostic_is_retained_before_removal_and_reaches_operator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            result,effects,state=self.run_lane(root)
            self.assertNotEqual(result.returncode,0)
            for fact in ('StorageUnavailable','advance','unconfirmed','17'):
                self.assertIn(fact,result.stderr)
            self.assertLess(effects.index('logs'),effects.index('rm'))
            self.assertFalse(state['present'])
            receipts=list(root.rglob(IDENTITY+'.json'))
            self.assertEqual(len(receipts),1)
            retained=json.loads(receipts[0].read_text())
            self.assertEqual(retained['exit_code'],17)
            self.assertEqual(retained['diagnostic']['cause'],'StorageUnavailable')
            self.assertNotIn(PRIVATE.decode(),result.stdout+result.stderr)

    def test_collection_failure_preserves_child_exit_and_withholds_destructive_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            result,effects,state=self.run_lane(root,log_failure=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('17',result.stderr)
            self.assertTrue(state['present'])
            self.assertNotIn('rm',effects)
            self.assertNotIn(PRIVATE.decode(),result.stdout+result.stderr)

    def test_removal_failure_cannot_replace_the_childs_admitted_cause(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            result,effects,state=self.run_lane(root,remove_failure=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('StorageUnavailable',result.stderr)
            self.assertIn('17',result.stderr)
            lines = result.stderr.splitlines()
            cleanup = [i for i, line in enumerate(lines)
                       if 'cleanup' in line.lower() and 'unconfirmed' in line.lower()]
            self.assertTrue(cleanup, 'Cleanup needs its own unsuccessful outcome.')
            self.assertLess(max(cleanup), len(lines) - 1)
            self.assertIn('StorageUnavailable', lines[-1])
            self.assertIn('17', lines[-1])
            self.assertTrue(state['present'])
            self.assertNotIn(PRIVATE.decode(),result.stdout+result.stderr)

    def test_receipt_publication_failure_keeps_the_known_child_cause_and_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "out/process-evidence"
            folder.mkdir(parents=True, mode=0o700)
            receipt = folder / (IDENTITY + ".json")
            receipt.write_text('{"older_evidence":true}')
            receipt.chmod(0o600)
            result, effects, state = self.run_lane(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("StorageUnavailable", result.stderr)
            self.assertIn("retention", result.stderr)
            self.assertTrue(state["present"])
            self.assertNotIn("rm", effects)
            self.assertEqual(receipt.read_text(), '{"older_evidence":true}')

    def test_oversized_log_collection_is_bounded_and_never_authorizes_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, effects, state = self.run_lane(Path(temporary), producer=b"A" * 131072 + DIAGNOSTIC)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("17", result.stderr)
            self.assertTrue(state["present"])
            self.assertNotIn("rm", effects)
            self.assertNotIn(PRIVATE.decode(), result.stderr)

    def test_foreign_ownership_prevents_both_log_access_and_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            result,effects,state=self.run_lane(Path(temporary),foreign=True)
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn('logs',effects)
            self.assertNotIn('rm',effects)
