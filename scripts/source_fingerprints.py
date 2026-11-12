"""Fingerprint executable Python structure and Study 1 source text."""

import ast
import hashlib
from pathlib import Path


class ExecutableStructure(ast.NodeTransformer):
    def visit(self, node):
        node = super().visit(node)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
        return node

    def visit_Call(self, node):
        node = self.generic_visit(node)
        # These append calls emit standalone comments in generated FaLang programs.
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "lines"
                and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.startswith("//") and "\n" not in node.args[0].value):
            node.args[0].value = "//"
        return node


def executable_sha256(source: str) -> str:
    tree = ExecutableStructure().visit(ast.parse(source))
    payload = ast.dump(tree, include_attributes=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def study1_source_digests(runtime: Path) -> dict[str, str]:
    grammar = "\n".join((runtime / name).read_text(encoding="utf-8")
                        for name in ("compiler/lexer.py", "compiler/parser.py"))
    names = sorted(path.name for path in (runtime / "compiler").glob("*.py")
                   if path.name != "__init__.py")
    core = "\n".join(f"{name}:{(runtime / 'compiler' / name).read_text(encoding='utf-8')}"
                     for name in names)
    return {key: hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
            for key, value in (("grammar_digest", grammar), ("compiler_core_digest", core))}
