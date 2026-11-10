"""Best-effort source-expression notes for internal Python tracebacks.

A validation helper can record which expression supplied its first argument
without accepting a handwritten label. The expression is source code, never
the evaluated value. It appears in a traceback note, not in the exception's
public message. Source-less execution still raises the original exception.
"""

import ast
import inspect
import linecache
from types import FrameType


def _argument_expression(caller: FrameType) -> str | None:
    """Use the executing instruction's exact AST call, even through aliases."""
    source = "".join(linecache.getlines(caller.f_code.co_filename, caller.f_globals))
    if not source:
        return None

    position = inspect.getframeinfo(caller, context=0).positions
    if position is None or position.col_offset is None:
        return None

    location = (
        position.lineno,
        position.col_offset,
        position.end_lineno,
        position.end_col_offset,
    )
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue

        if (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset) != location:
            continue

        argument = node.args[0]
        if isinstance(argument, ast.Constant):
            # Never reproduce a literal supplied directly by the caller.
            return None

        expression = ast.get_source_segment(source, argument)
        if expression is not None and len(expression) <= 200:
            return " ".join(expression.split())

    return None


def note_argument(error: Exception) -> Exception:
    """Attach the failing helper's first argument expression, when available.

    Called only while raising from a validation helper: this frame's parent
    is the helper and its grandparent is the call whose argument we want.
    Failure to inspect the source must never replace the original error.
    """
    current = inspect.currentframe()
    try:
        if current is None or current.f_back is None or current.f_back.f_back is None:
            return error

        helper = current.f_back
        expression = _argument_expression(helper.f_back)
        if expression:
            error.add_note(f"argument expression: {expression}")
    except Exception:
        # Diagnostics cannot acquire authority over the validation failure.
        pass
    finally:
        del current

    return error
