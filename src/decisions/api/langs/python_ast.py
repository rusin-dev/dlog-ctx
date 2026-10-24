"""Python backend: exact extraction using :mod:`ast`."""

from __future__ import annotations

import ast

from ...core.models import ApiSymbol, decision_refs, strip_decision_refs
from . import Backend


def extract(
    text: str,
    *,
    module: str,
    topic: str,
    doc_path: str,
    rel_path: str,
) -> list[ApiSymbol]:
    """Parse one module and return its public symbols."""
    tree = ast.parse(text, filename=rel_path)
    symbols: list[ApiSymbol] = [
        ApiSymbol(
            qualified_name=module,
            kind="module",
            module=module,
            topic=topic,
            doc_path=doc_path,
            signature=f"module {module}",
            doc=strip_decision_refs(ast.get_docstring(tree)),
            source_file=rel_path,
            lineno=1,
            decision_refs=decision_refs(ast.get_docstring(tree)),
        )
    ]
    for node in tree.body:
        symbols.extend(_walk_node(node, module, topic, doc_path, rel_path))
    return symbols


def _walk_node(
    node: ast.AST,
    module: str,
    topic: str,
    doc_path: str,
    rel_path: str,
    *,
    prefix: str = "",
) -> list[ApiSymbol]:
    symbols: list[ApiSymbol] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if _is_public(node.name):
            symbols.append(_function_symbol(node, module, topic, doc_path, rel_path, prefix))
    elif isinstance(node, ast.ClassDef):
        if _is_public(node.name):
            qualified = f"{prefix}{node.name}"
            raw_doc = ast.get_docstring(node)
            symbols.append(
                ApiSymbol(
                    qualified_name=f"{module}.{qualified}",
                    kind="class",
                    module=module,
                    topic=topic,
                    doc_path=doc_path,
                    signature=_class_signature(node),
                    doc=strip_decision_refs(raw_doc),
                    source_file=rel_path,
                    lineno=node.lineno,
                    decision_refs=decision_refs(raw_doc),
                )
            )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    symbols.extend(
                        _walk_node(
                            child,
                            module,
                            topic,
                            doc_path,
                            rel_path,
                            prefix=f"{qualified}.",
                        )
                    )
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        name = _target_name(node)
        if name and name.isupper() and not name.startswith("_"):
            symbols.append(
                ApiSymbol(
                    qualified_name=f"{module}.{prefix}{name}",
                    kind="constant",
                    module=module,
                    topic=topic,
                    doc_path=doc_path,
                    signature=_constant_signature(node, name),
                    source_file=rel_path,
                    lineno=node.lineno,
                )
            )
    return symbols


def _function_symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    module: str,
    topic: str,
    doc_path: str,
    rel_path: str,
    prefix: str,
) -> ApiSymbol:
    raw_doc = ast.get_docstring(node)
    return ApiSymbol(
        qualified_name=f"{module}.{prefix}{node.name}",
        kind="method" if prefix else "function",
        module=module,
        topic=topic,
        doc_path=doc_path,
        signature=_function_signature(node, is_method=bool(prefix)),
        returns=_annotation(node.returns),
        doc=strip_decision_refs(raw_doc),
        source_file=rel_path,
        lineno=node.lineno,
        decision_refs=decision_refs(raw_doc),
    )


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _annotation(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - ast.unparse is robust on parsed trees
        return None


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_method: bool) -> str:
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults += list(args.defaults)
    pairs = list(zip(positional, defaults, strict=False))
    posonly = len(args.posonlyargs)
    if is_method and pairs:
        pairs = pairs[1:]
        posonly = max(0, posonly - 1)

    parts: list[str] = []
    for index, (arg, default) in enumerate(pairs):
        if posonly and index == posonly:
            parts.append("/")
        parts.append(_arg_text(arg, default))
    if posonly and posonly >= len(pairs):
        parts.append("/")
    if args.vararg is not None:
        parts.append("*" + _arg_text(args.vararg, None))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
        parts.append(_arg_text(arg, default))
    if args.kwarg is not None:
        parts.append("**" + _arg_text(args.kwarg, None))

    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = _annotation(node.returns)
    suffix = f" -> {returns}" if returns else ""
    return f"{prefix} {node.name}({', '.join(parts)}){suffix}"


def _arg_text(arg: ast.arg, default: ast.expr | None) -> str:
    text = arg.arg
    annotation = _annotation(arg.annotation)
    if annotation:
        text += f": {annotation}"
    if default is not None:
        text += f" = {ast.unparse(default)}"
    return text


def _class_signature(node: ast.ClassDef) -> str:
    bases = [ast.unparse(base) for base in node.bases]
    bases += [
        f"{keyword.arg}={ast.unparse(keyword.value)}"
        if keyword.arg
        else f"**{ast.unparse(keyword.value)}"
        for keyword in node.keywords
    ]
    suffix = f"({', '.join(bases)})" if bases else ""
    return f"class {node.name}{suffix}"


def _constant_signature(node: ast.Assign | ast.AnnAssign, name: str) -> str:
    if isinstance(node, ast.AnnAssign) and node.annotation is not None:
        return f"{name}: {ast.unparse(node.annotation)}"
    return name


def _target_name(node: ast.Assign | ast.AnnAssign) -> str | None:
    target = (
        node.target
        if isinstance(node, ast.AnnAssign)
        else (node.targets[0] if node.targets else None)
    )
    if isinstance(target, ast.Name):
        return target.id
    return None


BACKEND = Backend(name="python", suffixes=(".py", ".pyi"), extract=extract)
