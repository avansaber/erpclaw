"""Core must remain usable without importing the assistant package."""
import ast
from pathlib import Path


CORE = Path(__file__).resolve().parents[1] / "packages/erpclaw-core/src/erpclaw_core"


def _core_assistant_imports_in_ast(tree):
    violations = []
    dynamic_names = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "importlib":
            dynamic_names.update(alias.asname or alias.name for alias in node.names if alias.name == "import_module")
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            function = node.func
            if ((isinstance(function, ast.Name) and function.id in dynamic_names)
                    or (isinstance(function, ast.Attribute) and function.attr == "import_module")):
                modules = [node.args[0].value]
        if any(isinstance(name, str) and (name == "erpclaw_assistant" or name.startswith("erpclaw_assistant."))
               for name in modules):
            violations.append(node.lineno)
    return violations


def test_core_files_never_import_assistant():
    files = sorted(CORE.rglob("*.py"))
    assert files
    violations = {str(path.relative_to(CORE)): lines for path in files
                  if (lines := _core_assistant_imports_in_ast(ast.parse(path.read_text(encoding="utf-8"))))}
    assert violations == {}


def test_import_boundary_detects_direct_lazy_and_dynamic_imports():
    for source in ("import erpclaw_assistant.resolve_text", "from erpclaw_assistant import resolve_text",
                   "def f():\n    import erpclaw_assistant", "__import__('erpclaw_assistant.graph')",
                   "import importlib\nimportlib.import_module('erpclaw_assistant')",
                   "from importlib import import_module as load\nload('erpclaw_assistant.graph')"):
        assert _core_assistant_imports_in_ast(ast.parse(source)), source
