"""The standalone ticket rule is the shared rule's complete, drift-checked closure."""

import ast
import io
import tokenize
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
ROOTS = {
    "decide", "RULE_EXPLICIT", "_read_object", "CONFIG_PATH", "OVERRIDE_PATH",
    "require_runnable", "LedgerCallError", "probe",
}
BEGIN = "# --- BEGIN inline ledger rule (skills/_lib/ledger_cli.py) ---"
END = "# --- END inline ledger rule ---"


def definitions(tree):
    result = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        else:
            continue
        for name in names:
            assert name not in result, f"duplicate definition: {name}"
            result[name] = node
    return result


def test_inline_ledger_rule_matches_its_shared_dependency_closure():
    source = (SKILL_DIR.parent / "_lib" / "ledger_cli.py").read_text(encoding="utf-8")
    ticket_source = (SKILL_DIR / "ticket.py").read_text(encoding="utf-8")
    shared = definitions(ast.parse(source))
    installed = definitions(ast.parse(ticket_source))
    forbidden = {
        "spec_from_file_location", "module_from_spec", "exec_module",
        "import_module", "__import__",
    }
    identifiers = {
        token.string for token in tokenize.generate_tokens(io.StringIO(ticket_source).readline)
        if token.type == tokenize.NAME
    }
    assert not (identifiers & forbidden), f"import-system entry points: {sorted(identifiers & forbidden)}"

    closure = set()
    pending = list(ROOTS)
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        closure.add(name)
        pending.extend(
            node.id for node in ast.walk(shared[name])
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            and node.id in shared and node.id not in closure
        )

    assert ticket_source.count(BEGIN) == ticket_source.count(END) == 1
    block = ticket_source.split(BEGIN)[1].split(END)[0]
    assert set(definitions(ast.parse(block))) == closure, "inline block must contain the complete closure only"
    for name in sorted(closure - {"LIB_DIR"}):
        assert ast.dump(installed[name]) == ast.dump(shared[name]), f"inline ledger definition drifted: {name}"
    assert ast.dump(installed["LIB_DIR"]) == ast.dump(ast.parse(
        'LIB_DIR = Path(__file__).resolve().parent.parent / "_lib"'
    ).body[0]), "LIB_DIR must name skills/_lib from ticket's location"
