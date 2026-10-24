"""Best-effort regex backend for non-Python languages.

Python gets an exact ``ast`` parser; other languages are covered by a
line-oriented scanner. It understands the shape that matters for API docs:

* container declarations (``class``/``interface``/``struct``/``trait``/``enum``/
  ``impl``/``module``) establish a scope;
* function/method declarations become symbols, qualified by their container;
* ALL_CAPS declarations become constants;
* the doc comment directly above a declaration is attached to it;
* ``@decision:<slug>`` markers are extracted as back-references.

Brace scope tracking is heuristic: it follows ``{``/``}`` depth, which is enough
for C-like languages. Ruby is handled with indentation instead. Multi-line
signatures are recorded by their first line. Anything more precise should ship
as a plugin backend (see :mod:`decisions.api.langs`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...core.models import ApiSymbol, decision_refs, strip_decision_refs
from .base import Backend, module_symbol

CONTROL_KEYWORDS = {
    "if",
    "for",
    "foreach",
    "while",
    "switch",
    "catch",
    "else",
    "do",
    "return",
    "throw",
    "delete",
    "sizeof",
    "typeof",
    "using",
    "lock",
    "fixed",
    "match",
    "when",
    "yield",
    "await",
    "assert",
    "unless",
    "until",
    "case",
    "default",
    "def",
    "end",
    "begin",
    "rescue",
    "ensure",
}


@dataclass(frozen=True)
class LanguageSpec:
    """Declarative description of one regex-scanned language.

    ``containers`` establish scopes (classes/modules), ``functions`` produce
    method/function symbols and ``constants`` produce constant symbols. When no
    pattern matches, the line is simply ignored.
    """

    name: str
    suffixes: tuple[str, ...]
    containers: tuple[re.Pattern[str], ...]
    functions: tuple[re.Pattern[str], ...]
    constants: tuple[re.Pattern[str], ...] = ()
    #: container keywords that only establish a scope (no symbol of their own),
    #: e.g. Rust ``impl`` blocks
    scope_only: tuple[str, ...] = ()
    indent_scope: bool = False
    doc_prefixes: tuple[str, ...] = ("///", "/**", "/*", "//", "*", "#", "--")


def _patterns(*raw: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(item) for item in raw)


SPECS: tuple[LanguageSpec, ...] = (
    LanguageSpec(
        name="javascript",
        suffixes=(".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"),
        containers=_patterns(
            r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?class\s+"
            r"(?P<name>[A-Za-z_$][\w$]*)",
            r"^(?:export\s+)?(?:declare\s+)?interface\s+(?P<name>[A-Za-z_$][\w$]*)",
            r"^(?:export\s+)?(?:declare\s+)?enum\s+(?P<name>[A-Za-z_$][\w$]*)",
            r"^(?:export\s+)?(?:declare\s+)?type\s+(?P<name>[A-Za-z_$][\w$]*)\s*"
            r"(?:<[^>]*>)?\s*=",
        ),
        functions=_patterns(
            r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?function\s*\*?\s*"
            r"(?P<name>[A-Za-z_$][\w$]*)\s*[(<]",
            r"^(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*"
            r"(?::[^=]+)?=\s*(?:async\s*)?"
            r"(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)",
            r"^(?:(?:public|private|protected|static|async|readonly|override|get|set"
            r"|declare|abstract)\s+)*(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*"
            r"\([^;{]*\)\s*(?::[^{]+)?\{",
        ),
        constants=_patterns(
            r"^(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=",
        ),
    ),
    LanguageSpec(
        name="go",
        suffixes=(".go",),
        containers=_patterns(
            r"^type\s+(?P<name>[A-Za-z_]\w*)\s+(?:struct|interface)\b",
        ),
        functions=_patterns(
            r"^func\s+\((?P<recv>[^)]*)\)\s*(?P<name>[A-Za-z_]\w*)\s*\(",
            r"^func\s+(?P<name>[A-Za-z_]\w*)\s*[\[(]",
        ),
        constants=_patterns(
            r"^const\s+(?P<name>[A-Z][A-Z0-9_]*)\b",
            r"^\s+(?P<name>[A-Z][A-Z0-9_]*)\s*(?:[A-Za-z_\[\]*.]+)?\s*=",
        ),
        doc_prefixes=("///", "//", "/*", "*"),
    ),
    LanguageSpec(
        name="rust",
        suffixes=(".rs",),
        containers=_patterns(
            r"^(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+(?P<name>[A-Za-z_]\w*)",
            r"^impl(?:<[^>]*>)?\s+(?:[A-Za-z_][\w:<>, ]*\s+for\s+)?(?P<name>[A-Za-z_]\w*)",
        ),
        functions=_patterns(
            r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?"
            r'(?:extern\s+"[^"]*"\s+)?fn\s+(?P<name>[A-Za-z_]\w*)',
        ),
        constants=_patterns(
            r"^(?:pub(?:\([^)]*\))?\s+)?const\s+(?P<name>[A-Z][A-Z0-9_]*)\s*:",
        ),
        scope_only=("impl",),
        doc_prefixes=("///", "//!", "//", "/*", "*"),
    ),
    LanguageSpec(
        name="java",
        suffixes=(".java",),
        containers=_patterns(
            r"^(?:(?:public|private|protected|static|final|abstract|sealed|non-sealed|strictfp)"
            r"\s+)*(?:class|interface|enum|record|@interface)\s+(?P<name>[A-Za-z_]\w*)",
        ),
        functions=_patterns(
            r"^(?:(?:public|private|protected|static|final|abstract|synchronized|native"
            r"|default|strictfp)\s+)*(?:<[^>]+>\s*)?(?:[\w$.<>\[\],?]+\s+)+"
            r"(?P<name>[A-Za-z_]\w*)\s*\([^;]*\)\s*(?:throws\s+[\w$.,\s]+)?\{",
            r"^(?:(?:public|private|protected)\s+)*(?P<name>[A-Z]\w*)\s*\([^;]*\)\s*"
            r"(?:throws\s+[\w$.,\s]+)?\{",
        ),
        constants=_patterns(
            r"^(?:(?:public|private|protected|static|final)\s+)+[\w$.<>\[\],?]+\s+"
            r"(?P<name>[A-Z][A-Z0-9_]*)\s*=",
        ),
        doc_prefixes=("///", "/**", "/*", "//", "*"),
    ),
    LanguageSpec(
        name="kotlin",
        suffixes=(".kt", ".kts"),
        containers=_patterns(
            r"^(?:(?:public|private|protected|internal|open|abstract|sealed|data|enum"
            r"|annotation|inner|value)\s+)*(?:class|interface|object)\s+"
            r"(?P<name>[A-Za-z_]\w*)",
        ),
        functions=_patterns(
            r"^(?:(?:public|private|protected|internal|open|abstract|override|suspend"
            r"|inline|operator|infix|tailrec|external|actual|expect)\s+)*fun\s+"
            r"(?:<[^>]+>\s*)?(?:[\w.<>?,\s]+\.)?(?P<name>[A-Za-z_]\w*)\s*\(",
        ),
        constants=_patterns(
            r"^(?:(?:public|private|internal|const)\s+)*const\s+val\s+(?P<name>[A-Z][A-Z0-9_]*)\b",
        ),
        doc_prefixes=("///", "/**", "/*", "//", "*"),
    ),
    LanguageSpec(
        name="csharp",
        suffixes=(".cs",),
        containers=_patterns(
            r"^(?:(?:public|private|protected|internal|static|abstract|sealed|partial"
            r"|readonly|unsafe|new)\s+)*(?:class|interface|struct|enum|record)\s+"
            r"(?P<name>[A-Za-z_]\w*)",
        ),
        functions=_patterns(
            r"^(?:(?:public|private|protected|internal|static|virtual|override|abstract"
            r"|async|sealed|extern|partial|new|unsafe)\s+)*(?:[\w.<>\[\],?]+\s+)+"
            r"(?P<name>[A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\([^;]*\)\s*"
            r"(?:where\s[^{]+)?\{",
        ),
        constants=_patterns(
            r"^(?:(?:public|private|protected|internal|static|const|readonly)\s+)+"
            r"[\w.<>\[\],?]+\s+(?P<name>[A-Z][A-Z0-9_]*)\s*=",
        ),
        doc_prefixes=("///", "/**", "/*", "//", "*"),
    ),
    LanguageSpec(
        name="ruby",
        suffixes=(".rb",),
        containers=_patterns(r"^(?:class|module)\s+(?P<name>[A-Z]\w*(?:::[A-Z]\w*)*)"),
        functions=_patterns(r"^\s*def\s+(?:self\.)?(?P<name>[A-Za-z_]\w*[?!=]?)"),
        constants=_patterns(r"^\s{0,2}(?P<name>[A-Z][A-Z0-9_]*)\s*="),
        indent_scope=True,
        doc_prefixes=("#",),
    ),
    LanguageSpec(
        name="php",
        suffixes=(".php",),
        containers=_patterns(
            r"^(?:(?:final|abstract|readonly)\s+)*(?:class|interface|trait|enum)\s+"
            r"(?P<name>[A-Za-z_]\w*)",
        ),
        functions=_patterns(
            r"^(?:(?:public|private|protected|static|final|abstract)\s+)*function\s+"
            r"&?\s*(?P<name>[A-Za-z_]\w*)\s*\(",
        ),
        constants=_patterns(
            r"^(?:(?:public|private|protected)\s+)?const\s+(?P<name>[A-Z][A-Z0-9_]*)\b",
        ),
        doc_prefixes=("///", "/**", "/*", "//", "*", "#"),
    ),
)


def extract(
    spec: LanguageSpec,
    text: str,
    *,
    module: str,
    topic: str,
    doc_path: str,
    rel_path: str,
) -> list[ApiSymbol]:
    """Scan ``text`` with ``spec`` and return every public symbol found."""
    lines = text.splitlines()
    symbols: list[ApiSymbol] = [
        module_symbol(module=module, topic=topic, doc_path=doc_path, rel_path=rel_path)
    ]
    scopes: list[tuple[int, str]] = []  # (depth or indent, name)
    depth = 0

    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        depth_before = depth
        scope_depth = _indent_of(raw) if spec.indent_scope else depth_before

        if spec.indent_scope and _closes_scope(line):
            while scopes and scope_depth <= scopes[-1][0]:
                scopes.pop()

        container = _first(spec.containers, line)
        if container is not None:
            name = _clean_name(container.group("name"))
            if _is_public(name):
                if not any(line.startswith(prefix) for prefix in spec.scope_only):
                    prefix = _scope_names(scopes, scope_depth)
                    symbols.append(
                        _symbol(
                            spec,
                            lines,
                            index,
                            name=".".join([*prefix, name]),
                            module=module,
                            topic=topic,
                            doc_path=doc_path,
                            rel_path=rel_path,
                            kind="class",
                        )
                    )
                scopes.append((scope_depth, name))
        else:
            function = _first(spec.functions, line)
            if function is not None:
                name = _clean_name(function.group("name"))
                receiver = function.groupdict().get("recv")
                if receiver and not spec.indent_scope:
                    owner = _receiver_type(receiver)
                    qualified = f"{owner}.{name}" if owner else name
                    kind = "method" if owner else "function"
                else:
                    prefix = _scope_names(scopes, scope_depth)
                    qualified = ".".join([*prefix, name])
                    kind = "method" if prefix else "function"
                if _is_public(name) and name.lower() not in CONTROL_KEYWORDS:
                    symbols.append(
                        _symbol(
                            spec,
                            lines,
                            index,
                            name=qualified,
                            module=module,
                            topic=topic,
                            doc_path=doc_path,
                            rel_path=rel_path,
                            kind=kind,
                        )
                    )
            elif spec.constants:
                constant = _first(spec.constants, line)
                if constant is not None:
                    name = _clean_name(constant.group("name"))
                    if _is_public(name):
                        prefix = _scope_names(scopes, scope_depth)
                        symbols.append(
                            _symbol(
                                spec,
                                lines,
                                index,
                                name=".".join([*prefix, name]),
                                module=module,
                                topic=topic,
                                doc_path=doc_path,
                                rel_path=rel_path,
                                kind="constant",
                            )
                        )

        depth += line.count("{") - line.count("}")
        if not spec.indent_scope:
            while scopes and "}" in line and scopes[-1][0] >= depth:
                scopes.pop()

    return symbols


def _symbol(
    spec: LanguageSpec,
    lines: list[str],
    index: int,
    *,
    name: str,
    module: str,
    topic: str,
    doc_path: str,
    rel_path: str,
    kind: str,
) -> ApiSymbol:
    raw_doc = _leading_doc(lines, index, spec.doc_prefixes)
    return ApiSymbol(
        qualified_name=f"{module}.{name}",
        kind=kind,
        module=module,
        topic=topic,
        doc_path=doc_path,
        signature=_signature(lines[index]),
        doc=strip_decision_refs(raw_doc),
        source_file=rel_path,
        lineno=index + 1,
        decision_refs=decision_refs(raw_doc),
    )


def _first(patterns: tuple[re.Pattern[str], ...], line: str) -> re.Match[str] | None:
    for pattern in patterns:
        match = pattern.match(line)
        if match is not None:
            return match
    return None


def _scope_names(scopes: list[tuple[int, str]], current: int) -> list[str]:
    if not scopes:
        return []
    # For indentation scopes every enclosing scope qualifies; for brace scopes a
    # declaration is nested when the scope opened at a shallower depth.
    return [name for depth, name in scopes if depth < current]


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _closes_scope(line: str) -> bool:
    return line.split()[0] in ("end", "end;", "end,")


def _clean_name(name: str) -> str:
    return name.replace("::", ".")


def _is_public(name: str) -> bool:
    return bool(name) and not name.startswith("_")


def _receiver_type(receiver: str) -> str | None:
    tokens = re.findall(r"[A-Za-z_]\w*", receiver.replace("*", " "))
    return tokens[-1] if tokens else None


def _signature(line: str) -> str:
    text = " ".join(line.split())
    return text.rstrip("{").rstrip(":").strip()


def _leading_doc(lines: list[str], index: int, prefixes: tuple[str, ...]) -> str | None:
    block: list[str] = []
    cursor = index - 1
    in_block = False
    while cursor >= 0:
        text = lines[cursor].strip()
        if not text:
            if block:
                break
            cursor -= 1
            continue
        if text.endswith("*/"):
            block.append(text)
            if text.startswith("/*"):
                break  # single-line block comment
            in_block = True
            cursor -= 1
            continue
        if in_block:
            block.append(text)
            if text.startswith("/*"):
                in_block = False
                break
            cursor -= 1
            continue
        if text.startswith(prefixes):
            block.append(text)
            cursor -= 1
            continue
        break
    if not block:
        return None

    block.reverse()
    cleaned: list[str] = []
    for text in block:
        text = text.strip()
        for prefix in ("///", "/**", "/*", "*/", "//", "#", "*", "--"):
            if text.startswith(prefix):
                text = text[len(prefix) :]
        text = text.rstrip("*/").strip()
        if text:
            cleaned.append(text)
    return "\n".join(cleaned) or None


def _build_backend(spec: LanguageSpec) -> Backend:
    """Wrap one :class:`LanguageSpec` as a registrable backend."""

    def run(
        text: str,
        *,
        module: str,
        topic: str,
        doc_path: str,
        rel_path: str,
    ) -> list[ApiSymbol]:
        """Extract symbols for this spec (backend protocol adapter)."""
        return extract(
            spec,
            text,
            module=module,
            topic=topic,
            doc_path=doc_path,
            rel_path=rel_path,
        )

    return Backend(name=spec.name, suffixes=spec.suffixes, extract=run)


BACKENDS: tuple[Backend, ...] = tuple(_build_backend(spec) for spec in SPECS)
