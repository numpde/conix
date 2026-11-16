# Port source: numpde/pay-to-play@8875439e2f05bb0e3ea262a6bfd190d846d5b8db
# repository_checks/quality/test_error_transparency_posture.py. Preserve donor
# fixtures; adapt source roots and the owned host exception names for Conix.
"""Flag filesystem failures reduced to fixed default-CLI messages."""

import ast
import unittest

from repository_checks.python_sources import ROOT as REPOSITORY_ROOT, production_sources


def _message_without_cause(node: ast.expr, caught_name: str | None) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _message_without_cause(node.left, caught_name) and _message_without_cause(node.right, caught_name)
    if isinstance(node, ast.JoinedStr):
        return not any(isinstance(item, ast.Name) and item.id == caught_name for item in ast.walk(node))
    return False


def _opaque_oserror_translations(source: str) -> list[int]:
    findings = []
    for handler in ast.walk(ast.parse(source)):
        if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
            continue
        caught = list(ast.walk(handler.type))
        if not any(isinstance(node, ast.Name) and node.id == "OSError" for node in caught):
            continue
        if len(handler.body) != 1 or not isinstance(handler.body[0], ast.Raise):
            continue
        replacement = handler.body[0].exc
        if not isinstance(replacement, ast.Call):
            continue
        if not isinstance(replacement.func, ast.Name) or replacement.func.id not in {"CommandError", "OperationError", "CampaignError"}:
            continue
        if (
            len(replacement.args) == 1
            and all(keyword.arg in {"returncode", "exit_code"} for keyword in replacement.keywords)
            and _message_without_cause(replacement.args[0], handler.name)
        ):
            findings.append(handler.lineno)
    return findings


def _raises_in_blocks(statements):
    for statement in statements:
        if isinstance(statement, ast.Raise):
            yield statement
        elif isinstance(statement, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
            for field in ("body", "orelse", "finalbody"):
                yield from _raises_in_blocks(getattr(statement, field, ()))


def _raw_cli_exception_text(source: str) -> list[int]:
    """Report direct raw cause projections in broadly caught CommandError messages.

    Supported projections are formatted values and one-argument str/repr calls;
    other AST nodes do not supply a candidate cause value.
    """
    findings = []
    for handler in ast.walk(ast.parse(source)):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name or handler.type is None:
            continue
        if not any(
            isinstance(node, ast.Name) and node.id in {"OSError", "Exception", "BaseException"} for node in ast.walk(handler.type)
        ):
            continue
        for statement in _raises_in_blocks(handler.body):
            if not isinstance(statement.exc, ast.Call):
                continue
            call = statement.exc
            if not isinstance(call.func, ast.Name) or call.func.id not in {"CommandError", "OperationError", "CampaignError"} or not call.args:
                continue
            for node in (node for arg in call.args for node in ast.walk(arg)):
                if isinstance(node, ast.FormattedValue):
                    value = node.value
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in {"str", "repr"}
                    and len(node.args) == 1
                ):
                    value = node.args[0]
                else:
                    continue
                if isinstance(value, ast.Name) and value.id == handler.name:
                    findings.append(statement.lineno)
                    break
    return findings


class ErrorTransparencyPostureTests(unittest.TestCase):
    def test_adapted_host_error_names_keep_both_donor_obligations(self):
        for name in ("OperationError", "CampaignError"):
            with self.subTest(name=name):
                fixed = f'try:\n    work()\nexcept OSError as exc:\n    raise {name}("failed") from exc\n'
                raw = f'try:\n    work()\nexcept Exception as exc:\n    raise {name}(str(exc))\n'
                self.assertEqual([3], _opaque_oserror_translations(fixed))
                self.assertEqual([4], _raw_cli_exception_text(raw))

    def test_raw_exception_text_is_not_a_safe_cause_projection(self) -> None:
        for message, expected in (
            ('f"failed: {exc}"', [4]),
            ('f"{exc!r}"', [4]),
            ('"failed: " + str(exc)', [4]),
            ("repr(exc)", [4]),
            ('"failed", str(exc)', [4]),
            ('f"filesystem code {exc.errno}"', []),
            ("safe_reason(exc)", []),
        ):
            with self.subTest(message=message):
                source = f"try:\n    work()\nexcept OSError as exc:\n    raise CommandError({message})\n"
                self.assertEqual(expected, _raw_cli_exception_text(source))

    def test_raw_exception_detector_follows_blocks_but_not_new_scopes(self) -> None:
        for block, expected in (("if ready", [5]), ("with resource", [5]), ("def deferred(exc)", []), ("class Deferred", [])):
            with self.subTest(block=block):
                source = f"try:\n    work()\nexcept OSError as exc:\n    {block}:\n        raise CommandError(str(exc))\n"
                self.assertEqual(expected, _raw_cli_exception_text(source))

    def test_raw_projection_dispatch_preserves_nested_and_unsupported_message_shapes(self) -> None:
        """Node dispatch retains raw disclosure without treating safe fields as causes."""
        for caught in ("OSError", "Exception", "BaseException"):
            for message, expected in (
                ('f"{exc!s:>20}"', [4]),
                ('f"{exc}{exc}"', [4]),
                ('{"reason": str(repr(exc))}', [4]),
                ('str(exc, encoding="utf8")', [4]),
                ('str(exc, "utf8")', []),
                ("errors.str(exc)", []),
                ('f"{safe_reason(exc)}"', []),
                ('f"{exc.errno}"', []),
                ("repr(other_error)", []),
            ):
                with self.subTest(caught=caught, message=message):
                    source = f"try:\n    work()\nexcept {caught} as exc:\n    raise CommandError({message})\n"
                    self.assertEqual(expected, _raw_cli_exception_text(source))

    def test_cli_projects_broad_failure_facts_instead_of_dumping_exception_text(self) -> None:
        findings = []
        for path in production_sources():
            for line in _raw_cli_exception_text(path.read_text(encoding="utf-8")):
                findings.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}:{line}: host error "
                    "formats a broad exception's raw text; project safe cause "
                    "and context fields rather than losing the cause or exposing values."
                )
        self.assertEqual([], findings, msg="\n".join(findings))

    def test_context_and_formatting_do_not_substitute_for_a_filesystem_cause(self) -> None:
        for message in ('"failed " + "again"', 'f"failed at {path}"', '"failed: " + f"{path}"', 'f"failed"'):
            with self.subTest(message=message):
                source = f"try:\n    work()\nexcept OSError as exc:\n    raise CommandError({message}, returncode=2) from exc\n"
                self.assertEqual([3], _opaque_oserror_translations(source))

    def test_explicit_reason_projection_is_not_a_cause_free_message(self) -> None:
        source = 'try:\n    work()\nexcept OSError as exc:\n    raise CommandError(f"filesystem code {exc.errno}") from exc\n'
        self.assertEqual([], _opaque_oserror_translations(source))

    def test_detector_flags_fixed_cli_translations_even_with_exception_chaining(self) -> None:
        for caught, statement in (
            ("OSError", 'raise CommandError("failed")'),
            ("OSError as exc", 'raise CommandError("failed") from exc'),
            ("(OSError, ValueError) as exc", 'raise CommandError("invalid") from exc'),
            ("OSError as exc", 'raise CommandError("failed") from None'),
        ):
            with self.subTest(caught=caught, statement=statement):
                source = f"try:\n    work()\nexcept {caught}:\n    {statement}\n"
                self.assertEqual([3], _opaque_oserror_translations(source))

    def test_detector_leaves_other_handler_shapes_for_separate_review(self) -> None:
        for caught, statement in (
            ("ValueError as exc", 'raise CommandError("invalid input") from exc'),
            ("OSError as exc", "raise"),
            ("OSError as exc", 'raise OtherError("failed") from exc'),
            ("OSError as exc", "raise CommandError(safe_reason(exc)) from exc"),
            ("OSError as exc", 'report(exc)\n    raise CommandError("failed") from exc'),
            ("errors.OSError as exc", 'raise CommandError("failed") from exc'),
            ("OSError as exc", 'raise errors.CommandError("failed") from exc'),
        ):
            with self.subTest(caught=caught, statement=statement):
                source = f"try:\n    work()\nexcept {caught}:\n    {statement}\n"
                self.assertEqual([], _opaque_oserror_translations(source))

    def test_filesystem_command_failures_do_not_hide_their_cause(self) -> None:
        findings = []
        for path in production_sources():
            for line in _opaque_oserror_translations(path.read_text(encoding="utf-8")):
                findings.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}:{line}: OSError "
                    "becomes a fixed host error; default CLI output hides "
                    "its chained cause. Project the safe filesystem reason "
                    "or report it before raising; do not dump exception text."
                )
        self.assertEqual([], findings, msg="\n".join(findings))
