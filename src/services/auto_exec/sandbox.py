"""Restricted-Python sandbox for AI-generated tool-orchestration code.

Only a whitelisted subset of the Python AST is allowed, and the only
callable exposed to the executed code is `call_tool` — a single async hook
bound by the caller (see tool_bridge.py) to the real tool dispatcher. There
is no import, eval, exec, file, network, or loop primitive available inside
the sandbox: every side effect the generated code can produce goes through
that one function, which itself only ever calls the existing,
already-trusted tool executors (axios_work / call_gtwy_agent /
call_mcp_tool / RAG).

Deliberately no loop support (no `for`, no `range`, no comprehensions):
generated plans are a flat sequence of `await call_tool(...)` calls with
values threaded between them, plus optional `if`/`try`/`except` for
conditional branching and failure recovery — never an actual loop. No
wall-clock timeout here either: each individual tool call already has its
own timeout at the dispatch level (e.g. axios_work's SERVICE_TIMEOUTS), so
a hung call fails there, not by running the whole script forever.
"""

import ast
import builtins as _builtins_module


class SandboxRejected(Exception):
    """Generated code failed the whitelist check before ever running."""


_ALLOWED_NODES = (
    ast.Module, ast.Expr, ast.AsyncFunctionDef, ast.arguments, ast.arg,
    ast.Return, ast.Assign, ast.AugAssign, ast.AnnAssign,
    ast.If, ast.Pass,
    ast.Try, ast.ExceptHandler, ast.Raise,
    ast.Await, ast.Call, ast.Name, ast.Load, ast.Store, ast.Del,
    ast.Attribute, ast.Subscript, ast.Slice,
    ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.Constant, ast.JoinedStr, ast.FormattedValue,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.And, ast.Or, ast.Not, ast.UAdd, ast.USub, ast.Invert,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Is, ast.IsNot,
    ast.Starred,
)

# Only these bare names may be *called*. Anything calling through an
# Attribute (e.g. os.system) is validated separately in visit_Attribute /
# visit_Call below. No `range` — there are no loops to use it in.
_ALLOWED_CALL_NAMES = {
    "call_tool", "len", "str", "int", "float", "bool", "dict", "list",
    "tuple", "set", "min", "max", "sum", "sorted", "isinstance", "abs",
    "round", "any", "all", "zip",
}

_SAFE_BUILTIN_NAMES = (
    "len", "str", "int", "float", "bool", "dict", "list", "tuple", "set",
    "min", "max", "sum", "sorted", "isinstance", "abs",
    "round", "any", "all", "zip",
)

_SAFE_EXCEPTION_NAMES = (
    "Exception", "ValueError", "KeyError", "TypeError", "IndexError",
    "StopIteration", "AttributeError", "ZeroDivisionError", "RuntimeError",
)


class _Validator(ast.NodeVisitor):
    def __init__(self):
        self.errors = []

    def generic_visit(self, node):
        if not isinstance(node, _ALLOWED_NODES):
            self.errors.append(f"Disallowed syntax: {type(node).__name__}")
            return
        super().generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr.startswith("__"):
            self.errors.append(f"Disallowed attribute access: {node.attr}")
            return
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Name):
            if func.id not in _ALLOWED_CALL_NAMES:
                self.errors.append(f"Call to disallowed function: {func.id}")
        elif isinstance(func, ast.Attribute):
            if func.attr.startswith("__"):
                self.errors.append(f"Disallowed method call: {func.attr}")
        else:
            self.errors.append("Disallowed call target")
        self.generic_visit(node)

    def visit_Import(self, node):
        self.errors.append("import is not allowed")

    def visit_ImportFrom(self, node):
        self.errors.append("import is not allowed")


def validate_code(code: str) -> ast.Module:
    """Parse + whitelist-check `code`. Raises SandboxRejected on any violation."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxRejected(f"Syntax error: {exc}") from exc

    top_level_fns = [
        n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run"
    ]
    if len(tree.body) != 1 or not top_level_fns:
        raise SandboxRejected(
            "Code must contain exactly one top-level statement: async def run(call_tool):"
        )

    run_fn = top_level_fns[0]
    arg_names = [a.arg for a in run_fn.args.args]
    if arg_names != ["call_tool"] or run_fn.args.vararg or run_fn.args.kwarg or run_fn.args.defaults:
        raise SandboxRejected("run() must take exactly one parameter named call_tool, no defaults/*args/**kwargs")

    validator = _Validator()
    validator.visit(tree)
    if validator.errors:
        raise SandboxRejected("; ".join(validator.errors))

    return tree


def _build_safe_globals():
    safe_builtins = {name: getattr(_builtins_module, name) for name in _SAFE_BUILTIN_NAMES}
    safe_builtins.update({name: getattr(_builtins_module, name) for name in _SAFE_EXCEPTION_NAMES})
    return {"__builtins__": safe_builtins}


async def run_sandboxed(code: str, call_tool_fn):
    """Validate and execute `code`'s `run(call_tool)` inside a restricted
    namespace. Returns whatever `run()` returns.

    Raises SandboxRejected (invalid code, never executed). Any exception
    raised by `call_tool` itself (a failed tool call) or by the generated
    code's own logic propagates as-is, so an uncaught tool failure
    naturally aborts the rest of the script — mirroring "a failed step
    blocks its dependents" without extra bookkeeping.
    """
    tree = validate_code(code)
    safe_globals = _build_safe_globals()

    compiled = compile(tree, filename="<auto_exec_plan>", mode="exec")
    exec(compiled, safe_globals)  # noqa: S102 - restricted namespace, whitelisted AST only

    run_fn = safe_globals.get("run")
    if run_fn is None:
        raise SandboxRejected("run() was not defined")

    return await run_fn(call_tool_fn)
