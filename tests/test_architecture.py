"""Keep shared infrastructure independent of HTTP feature routers."""

import ast
from pathlib import Path


def test_shared_layers_do_not_import_http_routers():
    root = Path(__file__).resolve().parents[1] / "app"
    violations = []
    for package in ("core", "permissions", "middleware", "persistence", "shared", "tenancy"):
        for path in (root / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                elif isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                else:
                    continue
                for module in modules:
                    if module.startswith("app.api") or (
                        module.startswith("app.features.") and module.endswith(".router")
                    ):
                        violations.append(f"{path.relative_to(root)}:{node.lineno}: {module}")
    assert not violations, "Shared layers must not depend on HTTP routers:\n" + "\n".join(
        violations
    )
