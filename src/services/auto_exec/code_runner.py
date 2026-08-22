"""Structural validation for AI-generated tool-orchestration code.

No AST whitelist and no restricted builtins/namespace: `code` is only
checked for shape (valid Python, exactly one top-level
`async def run(call_tool):` with that exact signature) and then executed
with normal Python semantics. `call_tool` is a single async hook bound by
the caller (see tool_bridge.py) to the real tool dispatcher.
"""

import ast


class CodeRejected(Exception):
    """Generated code failed the structural check before ever running."""


def validate_code(code: str) -> ast.Module:
    """Parse `code` and check its shape. Raises CodeRejected on any violation."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise CodeRejected(f"Syntax error: {exc}") from exc

    top_level_fns = [
        n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run"
    ]
    if len(tree.body) != 1 or not top_level_fns:
        raise CodeRejected(
            "Code must contain exactly one top-level statement: async def run(call_tool):"
        )

    run_fn = top_level_fns[0]
    arg_names = [a.arg for a in run_fn.args.args]
    if arg_names != ["call_tool"] or run_fn.args.vararg or run_fn.args.kwarg or run_fn.args.defaults:
        raise CodeRejected("run() must take exactly one parameter named call_tool, no defaults/*args/**kwargs")

    return tree


async def run_generated_code(code: str, call_tool_fn):
    """Validate and execute `code`'s `run(call_tool)`. Returns whatever `run()` returns.

    Raises CodeRejected (invalid code, never executed). Any exception
    raised by `call_tool` itself (a failed tool call) or by the generated
    code's own logic propagates as-is, so an uncaught tool failure
    naturally aborts the rest of the script — mirroring "a failed step
    blocks its dependents" without extra bookkeeping.
    """
    tree = validate_code(code)

    namespace = {}
    compiled = compile(tree, filename="<auto_exec_plan>", mode="exec")
    exec(compiled, namespace)  # noqa: S102 - AI-generated code runs with full builtins, per explicit decision

    run_fn = namespace.get("run")
    if run_fn is None:
        raise CodeRejected("run() was not defined")

    return await run_fn(call_tool_fn)
