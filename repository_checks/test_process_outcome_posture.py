"""Flag fixed terminal errors that erase observed child-process outcomes.

This local AST heuristic covers status guards and assertion helpers. It does
not prove transitive cleanup ordering; lifecycle contracts exercise that path.
"""

import ast
import unittest

from repository_checks.python_sources import ROOT, production_sources
from repository_checks.test_error_transparency_posture import _raises_in_blocks


def _state_field(node):
    if isinstance(node, ast.Attribute) and node.attr == "returncode":
        return "exit"
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        if node.slice.value == "Running":
            return "running"
        if node.slice.value == "ExitCode":
            return "exit"
    return None


def _outcome_predicates(node):
    """Distinguish a successful-operation demand from confirmed-stop admission."""
    field = _state_field(node)
    if field is not None:
        return {"success" if field == "running" else "failure"}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return {"failure" if value == "success" else "success"
                for value in _outcome_predicates(node.operand)}
    if isinstance(node, ast.BoolOp):
        return set().union(*(_outcome_predicates(value) for value in node.values))
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        field = _state_field(node.left)
        expected = node.comparators[0]
        if field is None or not isinstance(expected, ast.Constant):
            return set()
        if field == "running" and type(expected.value) is bool:
            success = expected.value
        elif field == "exit" and type(expected.value) is int and expected.value == 0:
            success = True
        else:
            return set()
        if isinstance(node.ops[0], (ast.NotEq, ast.IsNot)):
            success = not success
        elif not isinstance(node.ops[0], (ast.Eq, ast.Is)):
            return set()
        return {"success" if success else "failure"}
    return set()


def _has_evidence(call):
    def projection(value):
        if isinstance(value, (ast.Name, ast.Attribute, ast.Subscript)):
            return True
        return isinstance(value, ast.Call) and any(projection(argument) for argument in value.args)

    return any(keyword.arg in {"diagnostic", "exit_code", "returncode"}
               and projection(keyword.value) for keyword in call.keywords)


def _fixed_text(node):
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return all(not isinstance(part, ast.FormattedValue) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _fixed_text(node.left) and _fixed_text(node.right)
    return False


def opaque_process_outcomes(source):
    findings = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.If) and "failure" in _outcome_predicates(node.test):
            for statement in _raises_in_blocks(node.body):
                call = statement.exc
                if (isinstance(call, ast.Call) and call.args and _fixed_text(call.args[0])
                        and not _has_evidence(call)):
                    findings.add(statement.lineno)
        elif isinstance(node, ast.Call) and len(node.args) >= 2:
            function = node.func
            assertion = (isinstance(function, ast.Name) and function.id == "require"
                         or isinstance(function, ast.Attribute) and function.attr == "require")
            if assertion and "success" in _outcome_predicates(node.args[0]) and _fixed_text(node.args[1]):
                findings.add(node.lineno)
        elif isinstance(node, ast.Assert) and "success" in _outcome_predicates(node.test) and node.msg is not None:
            if _fixed_text(node.msg):
                findings.add(node.lineno)
    return sorted(findings)


class ProcessOutcomePostureTests(unittest.TestCase):
    def test_fixed_errors_cannot_replace_child_exit_or_liveness_evidence(self):
        for source in (
            'if child.returncode:\n    raise WorkerError("command failed")',
            'if renamed["State"]["ExitCode"] != 0:\n    raise OtherError("failed")',
            'if not record["State"]["Running"]:\n    raise RuntimeError("process stopped")',
            'require(record["State"]["Running"] is True, "identity or liveness changed")',
            'self.require(owned and observed["State"]["Running"], "worker exited")',
            'assert record["State"]["Running"], "worker exited"',
            'require(state["ExitCode"] == 0, "unclean exit")',
            'if child.returncode: raise Error("failed", diagnostic=None)',
            'if child.returncode: raise Error("failed", exit_code=0)',
            'if child.returncode: raise Error("failed", diagnostic=dict())',
            'if child.returncode: raise Error("failed", exit_code=int(0))',
        ):
            with self.subTest(source=source):
                self.assertTrue(opaque_process_outcomes(source))

    def test_explicit_outcome_projections_and_nonprocess_admission_are_distinct(self):
        for source in (
            'if child.returncode:\n    raise WorkerError("failed", exit_code=child.returncode)',
            'if not state["Running"]:\n    raise WorkerError("failed", diagnostic=record)',
            'if not state["Running"]:\n    raise failure_from_owned_child(state)',
            'require(record["Id"] == identity, "ownership changed")',
            'if not state["Running"]:\n    return False',
            'require(state["Running"] is False and valid_exit_range, "retirement unconfirmed")',
            'if child.returncode == 0: raise Error("unrelated validation")',
        ):
            with self.subTest(source=source):
                self.assertEqual(opaque_process_outcomes(source), [])

    def test_production_state_guards_preserve_observed_child_outcomes(self):
        findings = [f"{path.relative_to(ROOT)}:{line}: process state becomes a fixed error; "
                    "preserve owned exit/diagnostic facts before teardown"
                    for path in production_sources()
                    for line in opaque_process_outcomes(path.read_text(encoding="utf-8"))]
        self.assertEqual(findings, [], msg="\n".join(findings))
