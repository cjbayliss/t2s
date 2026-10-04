import ast
import io
import tokenize
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SOURCE_ROOTS = (REPO / "t2s", REPO / "tests")
DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def docstring_lines(source: str) -> tuple[int, ...]:
    tree = ast.parse(source)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, DOCSTRING_OWNERS) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            hits.append(first.lineno)
    return tuple(hits)


def comment_lines(source: str) -> tuple[int, ...]:
    return tuple(
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    )


def sources() -> list[Path]:
    return sorted({p for root in SOURCE_ROOTS for p in root.rglob("*.py")})


@pytest.mark.parametrize("path", sources(), ids=lambda p: str(p.relative_to(REPO)))
def test_no_docstrings_or_comments(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    sites = [f"docstring at line {n}" for n in docstring_lines(source)]
    sites += [f"comment at line {n}" for n in comment_lines(source)]
    assert sites == [], f"{path}: {'; '.join(sites)}"


def test_ban_checkers_catch_violations() -> None:
    assert docstring_lines('def f():\n    """no."""\n') == (2,)
    assert docstring_lines('class C:\n    """no."""\n') == (2,)
    assert docstring_lines('"""module."""\n') == (1,)
    assert docstring_lines("x = 1\n") == ()
    assert comment_lines("x = 1  # no\n") == (1,)
    assert comment_lines('y = "# not a comment"\n') == ()
    assert comment_lines("z = 1\n") == ()
