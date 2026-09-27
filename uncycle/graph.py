"""Build the import graph of a Python package."""

from __future__ import annotations

import ast
import dataclasses
import os
import re
import warnings
from collections.abc import Callable, Iterable, Iterator
from importlib.util import resolve_name

#: an edge as a pair of indices into :attr:`Graph.nodes`
Edge = tuple[int, int]
#: where an import statement is: absolute file path and 1-based line number
Location = tuple[str, int]


@dataclasses.dataclass
class Graph:
    """A module import graph: sorted module names, edges as index pairs into them, and per
    edge the import statements behind it, which a graph read from a file does not have."""

    nodes: list[str]
    edges: list[Edge]
    locations: dict[Edge, list[Location]] = dataclasses.field(
        default_factory=dict, compare=False
    )

    def names(self, edges: Iterable[Edge]) -> list[tuple[str, str]]:
        """The given edges as pairs of module names."""
        return [(self.nodes[i], self.nodes[j]) for i, j in edges]

    def indices(self, named: Iterable[tuple[str, str]]) -> list[Edge]:
        """Edges by name in this graph's index space; edges it does not have are dropped."""
        index = {name: i for i, name in enumerate(self.nodes)}
        edges = set(self.edges)
        pairs = ((index[a], index[b]) for a, b in named if a in index and b in index)
        return sorted(edge for edge in pairs if edge in edges)


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _is_main(test: ast.expr) -> bool:
    """``__name__ == "__main__"``, in either order."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    sides = (test.left, test.comparators[0])
    name = any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
    main = any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides)
    return name and main


def _runs_on_import(test: ast.expr) -> bool:
    """Whether the body of ``if test:`` can run while the module is being imported."""
    return not _is_type_checking(test) and not _is_main(test)


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
#: in a namespace: any public name may be there, see :func:`build_graph`
_ANY = "*"


def _statements(tree: ast.AST, inline: bool) -> Iterator[tuple[ast.AST, bool]]:
    """The statements that run when the module is imported, and whether each is at module
    level rather than in a function or class body. Iterative rather than a NodeVisitor, since
    a generated file with a very long expression nests deeper than the recursion limit;
    expressions are never entered at all."""
    stack: list[tuple[ast.AST, bool]] = [(tree, True)]
    while stack:
        node, top = stack.pop()
        yield node, top
        if isinstance(node, _SCOPES):
            if inline:
                stack.extend((child, False) for child in node.body)
        elif isinstance(node, ast.If) and not _runs_on_import(node.test):
            # the body of if TYPE_CHECKING and of if __name__ == "__main__" does not run
            # on import, but the else branch of either does
            stack.extend((child, top) for child in node.orelse)
        else:
            for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                stack.extend((child, top) for child in getattr(node, field, ()))


def _literal_names(value: ast.expr | None) -> list[str] | None:
    """The strings in a list or tuple literal like ``["a", "b"]``, else ``None``."""
    if not isinstance(value, (ast.List, ast.Tuple)):
        return None
    names = [
        e.value
        for e in value.elts
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    ]
    return names if len(names) == len(value.elts) else None


def _all_update(node: ast.AST) -> tuple[bool, list[str] | None]:
    """Whether a statement changes ``__all__``, and if so the literal names it adds, or
    ``None`` when it computes them."""
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            return True, _literal_names(node.value)
    elif (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "__all__"
    ):
        return True, None  # __all__.extend(...), __all__.append(...) and the like
    return False, None


def _stored(target: ast.expr) -> Iterator[str]:
    """The names an assignment target binds: ``a`` and ``b`` in ``a, (b, c.d) = ...``."""
    for node in ast.walk(target):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            yield node.id


def _bound(node: ast.AST) -> Iterator[str]:
    """The names a statement binds in the module namespace, except by ``from`` imports."""
    if isinstance(node, _SCOPES):
        yield node.name
    elif isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.asname or alias.name.partition(".")[0]
    elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            yield from _stored(target)
        # what a literal __all__ lists exists, however it got there
        if isinstance(node.value, (ast.List, ast.Tuple)) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in targets
        ):
            for elt in node.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    yield elt.value
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        yield from _stored(node.target)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                yield from _stored(item.optional_vars)
    elif isinstance(node, ast.ExceptHandler) and node.name:
        yield node.name


@dataclasses.dataclass
class _Module:
    """What one source file tells us."""

    path: str
    is_package: bool
    #: the imports that run: ``("a.b", None, line)`` for ``import a.b`` and
    #: ``("a", "b", line)`` for ``from a import b``
    imports: list[tuple[str, str | None, int]] = dataclasses.field(default_factory=list)
    #: names bound at module level, so attributes the module has once it has run
    bound: set[str] = dataclasses.field(default_factory=set)
    #: modules star-imported at module level
    stars: list[str] = dataclasses.field(default_factory=list)
    #: what a star import of this module brings in: the names of a literal ``__all__``, or
    #: ``None`` if there is none or it is computed
    exports: set[str] | None = None


def scan_module(
    path: str, name: str, is_package: bool, inline: bool, warn: Callable[[str], None]
) -> _Module:
    """Parse one source file for its imports and the names it binds."""
    # bytes, so that ast honors a PEP 263 coding cookie
    with open(path, "rb") as f:
        tree = ast.parse(f.read(), filename=path)
    module = _Module(path, is_package)
    package = name if is_package else name.rpartition(".")[0]
    # the union of every literal __all__ assignment, as either branch of an if may run
    exports: set[str] | None = set()
    has_all = False

    for node, top in _statements(tree, inline):
        if top:
            module.bound.update(_bound(node))
            changes_all, names = _all_update(node)
            if changes_all:
                has_all = True
                exports = (
                    None if names is None or exports is None else exports | set(names)
                )
        if isinstance(node, ast.Import):
            # import statements are always absolute
            for alias in node.names:
                module.imports.append((alias.name, None, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            # from imports can be relative, and the alias can be a submodule or attribute
            try:
                src = resolve_name("." * node.level + (node.module or ""), package)
            except ImportError:
                # such a statement fails at runtime too; it is in a template or dead code
                warn(
                    f"{path}:{node.lineno}: relative import beyond the package, skipped"
                )
                continue
            for alias in node.names:
                module.imports.append((src, alias.name, node.lineno))
                if not top:
                    continue
                if alias.name == "*":
                    module.stars.append(src)
                # from . import x in a package's __init__ names the submodule x, not an
                # attribute the package defines
                elif alias.asname or src != name:
                    module.bound.add(alias.asname or alias.name)
    if has_all:
        module.exports = exports
    return module


def build_graph(
    package_dir: str,
    exclude: str | None = None,
    inline: bool = False,
    warn: Callable[[str], None] = warnings.warn,
) -> Graph:
    """The import graph of a package. Modules whose name matches the ``exclude`` regex are left
    out; ``inline`` includes imports inside functions and classes. Statements that cannot be
    imports of anything, such as a relative import above the package, go to ``warn``."""
    package_dir = os.path.abspath(package_dir)
    root, pkg = os.path.split(package_dir)
    if not pkg.isidentifier():
        raise ValueError(f"{package_dir}: {pkg!r} is not a valid package name")
    if not os.path.isfile(os.path.join(package_dir, "__init__.py")):
        raise ValueError(f"{package_dir}: not a package, it has no __init__.py")

    excluded = re.compile(exclude).search if exclude else lambda _: None

    modules: dict[str, _Module] = {}
    for dirpath, dirnames, filenames in os.walk(package_dir):
        subpkg = os.path.relpath(dirpath, root).replace(os.sep, ".")
        dirnames[:] = [
            d
            for d in dirnames
            if d.isidentifier()
            and os.path.isfile(os.path.join(dirpath, d, "__init__.py"))
            and not excluded(f"{subpkg}.{d}")
        ]
        for filename in filenames:
            stem, ext = os.path.splitext(filename)
            is_package = stem == "__init__"
            name = subpkg if is_package else f"{subpkg}.{stem}"
            if ext == ".py" and not excluded(name):
                path = os.path.join(dirpath, filename)
                modules[name] = scan_module(path, name, is_package, inline, warn)

    namespaces: dict[str, set[str]] = {}

    def namespace(name: str) -> set[str]:
        """The attributes a module has once it has run, as far as the tree shows. It holds
        ``_ANY`` if any public name may be there: the module star-imports a module the tree
        cannot see, or defines a module-level ``__getattr__``. That marker is passed on by
        star imports of this module like any other public name."""
        if name not in namespaces:
            # registered before it is filled: star imports can be circular
            namespaces[name] = names = set()
            names |= modules[name].bound
            if "__getattr__" in names:
                names.add(_ANY)
            for star in modules[name].stars:
                if star not in modules:
                    names.add(_ANY)
                elif (exports := modules[star].exports) is not None:
                    names |= exports
                else:
                    # without a literal __all__, a star import brings in the public names
                    names |= {n for n in namespace(star) if not n.startswith("_")}
        return namespaces[name]

    def target(module: str, attr: str | None) -> str:
        """The module that ``from module import attr`` runs, or ``module`` for ``import``."""
        if attr is None or attr == "*":
            return module
        submodule = f"{module}.{attr}"
        if submodule in modules:
            return submodule
        if module in modules and modules[module].is_package:
            if attr.startswith("__") and attr.endswith("__"):
                return module  # __file__ and friends: every module has them
            names = namespace(module)
            if attr in names or (_ANY in names and not attr.startswith("_")):
                return module
            # a submodule the tree does not have, typically a compiled one: a leaf
            return submodule
        return module

    inside = re.compile(rf"{re.escape(pkg)}(\.|$)").match

    def keep(module: str) -> bool:
        """The graph holds this package's own modules, minus the excluded ones."""
        return bool(inside(module)) and not excluded(module)

    lines: dict[tuple[str, str], set[int]] = {}
    for name, module in modules.items():
        for imported, attr, line in module.imports:
            dst = target(imported, attr)
            # a self-loop is a cycle no reshuffling of imports can break
            if dst != name and keep(dst):
                lines.setdefault((name, dst), set()).add(line)
            # importing a.b.c runs a/b/__init__.py too, unless that is the importer or one
            # of its own packages, which ran before it
            parts = dst.split(".")
            for i in range(1, len(parts)):
                above = ".".join(parts[:i])
                if (
                    above in modules
                    and keep(above)
                    and not f"{name}.".startswith(f"{above}.")
                ):
                    lines.setdefault((name, above), set()).add(line)

    nodes = sorted(set(modules) | {dst for _, dst in lines})
    index = {node: i for i, node in enumerate(nodes)}
    locations = {
        (index[src], index[dst]): [(modules[src].path, line) for line in sorted(where)]
        for (src, dst), where in lines.items()
    }
    return Graph(nodes, sorted(locations), locations)
