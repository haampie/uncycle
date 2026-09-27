import os
import pathlib

import pytest

from uncycle import Graph, build_graph


def edges(graph):
    return set(graph.names(graph.edges))


def test_from_import_submodule(tree):
    d = tree({"pkg/__init__.py": "", "pkg/y.py": "", "pkg/m.py": "from . import y"})
    assert edges(build_graph(d)) == {("pkg.m", "pkg.y")}


def test_from_import_attribute(tree):
    d = tree({"pkg/__init__.py": "thing = 1", "pkg/m.py": "from . import thing"})
    assert edges(build_graph(d)) == {("pkg.m", "pkg")}


def test_from_import_attribute_named_like_a_submodule(tree):
    """``Config`` is the class, not ``config.py``, also on a case-insensitive filesystem."""
    d = tree(
        {
            "pkg/__init__.py": "from .config import Config",
            "pkg/config.py": "class Config: pass",
            "pkg/m.py": "from . import Config",
        }
    )
    assert edges(build_graph(d)) == {("pkg", "pkg.config"), ("pkg.m", "pkg")}


def test_from_import_attribute_named_like_a_subpackage(tree):
    d = tree(
        {
            "pkg/__init__.py": "from .sub import Sub",
            "pkg/sub/__init__.py": "class Sub: pass",
            "pkg/m.py": "from pkg import Sub",
        }
    )
    assert edges(build_graph(d)) == {("pkg", "pkg.sub"), ("pkg.m", "pkg")}


def test_from_subpackage_import_submodule(tree):
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/y.py": "",
            "pkg/m.py": "from .sub import y",
        }
    )
    assert edges(build_graph(d)) == {("pkg.m", "pkg.sub"), ("pkg.m", "pkg.sub.y")}


def test_from_subpackage_import_attribute(tree):
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "def thing(): pass",
            "pkg/m.py": "from .sub import thing",
        }
    )
    assert edges(build_graph(d)) == {("pkg.m", "pkg.sub")}


def test_from_package_import_a_module_the_tree_does_not_have(tree):
    """``_foo`` is neither a source file nor a name pkg/__init__.py binds, so it is a module
    the tree cannot see, typically a compiled one, and not a dependency on the package."""
    d = tree(
        {
            "pkg/__init__.py": "from ._basic import *\nfrom . import _foo",
            "pkg/_basic.py": "from . import _foo\nfrom pkg import _foo as impl",
            "pkg/m.py": "from pkg import _foo\nimport pkg._foo",
        }
    )
    graph = build_graph(d)
    assert graph.nodes == ["pkg", "pkg._basic", "pkg._foo", "pkg.m"]
    assert edges(graph) == {
        ("pkg", "pkg._basic"),
        ("pkg", "pkg._foo"),
        ("pkg._basic", "pkg._foo"),
        ("pkg.m", "pkg._foo"),
    }


def test_from_plain_module_import_anything_is_an_attribute(tree):
    """Only packages have submodules, so a name a plain module does not visibly bind is
    still an attribute of it."""
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/impl.py": "globals()['x'] = 1",
            "pkg/m.py": "from .impl import x",
        }
    )
    assert edges(build_graph(d)) == {("pkg.m", "pkg.impl")}


@pytest.mark.parametrize(
    "binding",
    [
        "solve = 1",
        "solve: int = 1",
        "solve += 1",
        "solve, (other, *rest) = 1, (2, 3)",
        "def solve(): pass",
        "async def solve(): pass",
        "class solve: pass",
        "import solve",
        "import solve.deep",
        "import deep as solve",
        "from other import solve",
        "from other import x as solve",
        "for solve in (): pass",
        "with open('f') as solve: pass",
        "try:\n    pass\nexcept Exception as solve:\n    pass",
        "if x:\n    pass\nelse:\n    solve = 1",
        "while x:\n    solve = 1",
        "__all__ = ['solve']",
        "__all__ += ('solve',)",
    ],
)
def test_the_names_a_package_binds_are_its_attributes(tree, binding):
    d = tree({"pkg/__init__.py": binding, "pkg/m.py": "from pkg import solve"})
    assert edges(build_graph(d)) == {("pkg.m", "pkg")}


@pytest.mark.parametrize(
    "binding",
    [
        "def f():\n    solve = 1",
        "class C:\n    solve = 1",
        "if TYPE_CHECKING:\n    solve = 1",
        "from . import solve",
    ],
)
def test_the_names_a_package_does_not_bind_are_unseen_submodules(tree, binding):
    d = tree({"pkg/__init__.py": binding, "pkg/m.py": "from pkg import solve"})
    found = edges(build_graph(d))
    assert ("pkg.m", "pkg.solve") in found
    assert ("pkg.m", "pkg") not in found


def test_a_star_import_of_an_unseen_module_can_bind_any_public_name(tree):
    """numpy style: pkg/__init__.py star-imports a compiled module, so any public name may
    come from it, but a star import never brings in an underscore name."""
    d = tree(
        {
            "pkg/__init__.py": "from ._ufuncs import *",
            "pkg/m.py": "from pkg import gammaln\nfrom pkg import _specfun",
            "pkg/sub/__init__.py": "from .. import *",
            "pkg/sub/n.py": "from pkg.sub import gammaln",
        }
    )
    assert edges(build_graph(d)) == {
        ("pkg", "pkg._ufuncs"),
        ("pkg.m", "pkg"),
        ("pkg.m", "pkg._specfun"),
        ("pkg.sub", "pkg"),
        ("pkg.sub.n", "pkg.sub"),
    }


def test_names_from_star_imports_are_attributes_of_the_package(tree):
    """scipy style: pkg/__init__.py star-imports its implementation modules, and the rest of
    the world does ``from pkg import solve``, which needs pkg to have run."""
    d = tree(
        {
            "pkg/__init__.py": "from ._basic import *",
            "pkg/_basic.py": "from ._misc import *\nimport numpy as np\ndef solve(): pass",
            "pkg/_misc.py": "class LinAlgError(Exception): pass\n_private = 1",
            "pkg/other/__init__.py": "",
            "pkg/other/m.py": "from pkg import solve, LinAlgError",
            "pkg/other/n.py": "from pkg import _private",
        }
    )
    assert edges(build_graph(d)) == {
        ("pkg", "pkg._basic"),
        ("pkg._basic", "pkg._misc"),
        ("pkg.other.m", "pkg"),
        ("pkg.other.n", "pkg._private"),  # a star import does not bring in _private
    }


def test_a_star_import_brings_in_what_a_literal_all_lists(tree):
    """scipy.fftpack: _pseudo_diffs.py imports the compiled convolve submodule, but leaves
    it out of __all__, so the package does not bind it."""
    d = tree(
        {
            "pkg/__init__.py": "from ._impl import *",
            "pkg/_impl.py": "from . import convolve\n__all__ = ['diff']\ndef diff(): pass",
            "pkg/m.py": "from pkg import diff",
        }
    )
    assert edges(build_graph(d)) == {
        ("pkg", "pkg._impl"),
        ("pkg._impl", "pkg.convolve"),
        ("pkg.m", "pkg"),
    }


@pytest.mark.parametrize(
    "exports",
    [
        "__all__ = ['_private']",
        "__all__ = ('_private',)",
        "__all__: list[str] = ['_private']",
        "__all__ = []\n__all__ += ['_private']",
        "if x:\n    __all__ = ['_private']\nelse:\n    __all__ = ['other']",
    ],
)
def test_a_literal_all_can_export_a_private_name(tree, exports):
    d = tree(
        {
            "pkg/__init__.py": "from ._impl import *",
            "pkg/_impl.py": f"{exports}\n_private = 1",
            "pkg/m.py": "from pkg import _private",
        }
    )
    assert ("pkg.m", "pkg") in edges(build_graph(d))


@pytest.mark.parametrize(
    "exports",
    [
        "__all__ = [n for n in dir() if n.islower()]",
        "__all__ = ['diff']\n__all__ += other.__all__",
        "__all__ = ['diff']\n__all__.extend(other.__all__)",
    ],
)
def test_a_computed_all_brings_in_the_public_names(tree, exports):
    d = tree(
        {
            "pkg/__init__.py": "from ._impl import *",
            "pkg/_impl.py": f"import other\n{exports}\ndef convolve(): pass",
            "pkg/m.py": "from pkg import convolve",
        }
    )
    assert ("pkg.m", "pkg") in edges(build_graph(d))


def test_circular_star_imports_terminate(tree):
    d = tree(
        {
            "pkg/__init__.py": "from .a import *",
            "pkg/a.py": "from .b import *\nx = 1",
            "pkg/b.py": "from .a import *",
            "pkg/m.py": "from pkg import x",
        }
    )
    assert ("pkg.m", "pkg") in edges(build_graph(d))


def test_dunders_are_attributes_of_every_module(tree):
    d = tree({"pkg/__init__.py": "", "pkg/m.py": "from pkg import __file__"})
    assert edges(build_graph(d)) == {("pkg.m", "pkg")}


def test_a_module_level_getattr_can_bind_any_public_name(tree):
    """PEP 562 lazy loaders and deprecation shims: the package may serve any public name,
    but a private one is still taken to be a submodule the tree cannot see."""
    d = tree(
        {
            "pkg/__init__.py": "def __getattr__(name): ...",
            "pkg/m.py": "from pkg import lazy\nfrom pkg import _ext",
            "pkg/sub/__init__.py": "from .. import *",
            "pkg/sub/n.py": "from pkg.sub import lazy",
        }
    )
    assert edges(build_graph(d)) == {
        ("pkg.m", "pkg"),
        ("pkg.m", "pkg._ext"),
        ("pkg.sub", "pkg"),
        ("pkg.sub.n", "pkg.sub"),
    }


def test_from_module_import_star(tree):
    d = tree({"pkg/__init__.py": "", "pkg/d.py": "", "pkg/m.py": "from pkg.d import *"})
    assert edges(build_graph(d)) == {("pkg.m", "pkg.d")}


def test_import_as_ignores_the_alias(tree):
    d = tree({"pkg/__init__.py": "", "pkg/e.py": "", "pkg/m.py": "import pkg.e as x"})
    assert edges(build_graph(d)) == {("pkg.m", "pkg.e")}


@pytest.mark.parametrize("statement", ["import pkg.a.b", "from pkg.a.b import x"])
def test_an_import_depends_on_the_packages_it_runs(tree, statement):
    """``import pkg.a.b`` runs pkg/a/__init__.py too. pkg/__init__.py has run before pkg.m,
    so that is no edge."""
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/a/__init__.py": "",
            "pkg/a/b.py": "",
            "pkg/m.py": statement,
        }
    )
    assert edges(build_graph(d)) == {("pkg.m", "pkg.a"), ("pkg.m", "pkg.a.b")}


def test_a_cycle_through_the_init_of_a_package_in_between(tree):
    """Importing pkg.b first fails: pkg.sub runs before pkg.sub.m, and imports B from the
    partially initialized pkg.b."""
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "from pkg.sub.m import M\nclass B: pass",
            "pkg/sub/__init__.py": "from pkg.b import B",
            "pkg/sub/m.py": "class M: pass",
        }
    )
    assert edges(build_graph(d)) == {
        ("pkg.b", "pkg.sub"),
        ("pkg.b", "pkg.sub.m"),
        ("pkg.sub", "pkg.b"),
    }


def test_importing_a_sibling_through_the_parent_is_no_cycle(tree):
    """``from pkg import a`` in pkg.b runs pkg/__init__.py, which already ran before pkg.b."""
    d = tree(
        {
            "pkg/__init__.py": "from . import b",
            "pkg/a.py": "",
            "pkg/b.py": "from pkg import a",
        }
    )
    assert edges(build_graph(d)) == {("pkg", "pkg.b"), ("pkg.b", "pkg.a")}


def test_relative_import_from_the_parent_package(tree):
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/m.py": "from ..a import thing",
        }
    )
    assert edges(build_graph(d)) == {("pkg.sub.m", "pkg.a")}


def test_imports_outside_the_package_are_dropped(tree):
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "import os\nimport other.thing\nfrom re import X",
        }
    )
    assert edges(build_graph(d)) == set()


@pytest.mark.parametrize("test", ["TYPE_CHECKING", "typing.TYPE_CHECKING"])
def test_type_checking_body_is_skipped_but_its_else_is_kept(tree, test):
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "",
            "pkg/c.py": "",
            "pkg/m.py": f"if {test}:\n    import pkg.b\nelse:\n    import pkg.c\n",
        }
    )
    assert edges(build_graph(d)) == {("pkg.m", "pkg.c")}


def test_negated_type_checking_is_not_special(tree):
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "",
            "pkg/m.py": "if not TYPE_CHECKING:\n    import pkg.b\n",
        }
    )
    assert edges(build_graph(d)) == {("pkg.m", "pkg.b")}


def test_both_arms_of_a_try_except_import(tree):
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "",
            "pkg/c.py": "",
            "pkg/m.py": "try:\n    import pkg.b\nexcept ImportError:\n    import pkg.c\n",
        }
    )
    assert edges(build_graph(d)) == {("pkg.m", "pkg.b"), ("pkg.m", "pkg.c")}


@pytest.mark.parametrize("scope", ["def f():", "class C:"])
def test_inline_imports(tree, scope):
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "",
            "pkg/m.py": f"{scope}\n    import pkg.b\n",
        }
    )
    assert edges(build_graph(d)) == set()
    assert edges(build_graph(d, inline=True)) == {("pkg.m", "pkg.b")}


def test_re_exports_are_plain_edges(tree):
    """x re-exports x.y, which re-exports x.y.z, which imports foo: only those three edges.
    That x thereby depends on foo is the solver's business, see test_fas."""
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/foo.py": "",
            "pkg/x/__init__.py": "from . import y",
            "pkg/x/y/__init__.py": "from . import z",
            "pkg/x/y/z.py": "import pkg.foo",
        }
    )
    assert edges(build_graph(d)) == {
        ("pkg.x", "pkg.x.y"),
        ("pkg.x.y", "pkg.x.y.z"),
        ("pkg.x.y.z", "pkg.foo"),
    }


def test_an_edge_records_where_its_import_statement_is(tree):
    """The line of a multi-line ``from`` import is that of the ``from``."""
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "import os\nfrom pkg import (\n    b,\n)\n",
            "pkg/b.py": "",
        }
    )
    graph = build_graph(d)
    (edge,) = graph.edges
    assert graph.names([edge]) == [("pkg.m", "pkg.b")]
    assert graph.locations[edge] == [(os.path.join(d, "m.py"), 2)]


def test_two_statements_for_one_module_are_one_edge_with_two_locations(tree):
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": "import pkg.b\nfrom pkg import b\n",
            "pkg/b.py": "",
        }
    )
    graph = build_graph(d)
    (edge,) = graph.edges
    assert [line for _, line in graph.locations[edge]] == [1, 2]


def test_locations_do_not_take_part_in_equality(tree):
    d = tree({"pkg/__init__.py": "", "pkg/m.py": "import pkg.b", "pkg/b.py": ""})
    graph = build_graph(d)
    assert graph.locations
    assert graph == build_graph(d)
    assert graph == Graph(graph.nodes, graph.edges)


def test_a_package_importing_an_unseen_submodule_of_itself_is_not_a_self_loop(tree):
    """``from . import thing`` in pkg/__init__.py names the submodule pkg.thing, which the
    tree does not have: an edge to a leaf, and in particular not an edge from pkg to pkg."""
    d = tree({"pkg/__init__.py": "from . import thing"})
    assert edges(build_graph(d)) == {("pkg", "pkg.thing")}


def test_exclude_drops_modules_and_prunes_subpackages(tree):
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/keep.py": "import pkg.vendor.inner",
            "pkg/vendor/__init__.py": "",
            "pkg/vendor/inner.py": "import pkg.keep",
        }
    )
    graph = build_graph(d, exclude=r"^pkg\.vendor\b")
    assert edges(graph) == set()
    assert graph.nodes == ["pkg", "pkg.keep"]


def test_modules_without_any_import_are_still_nodes(tree):
    d = tree({"pkg/__init__.py": "", "pkg/lonely.py": ""})
    graph = build_graph(d)
    assert graph.nodes == ["pkg", "pkg.lonely"]
    assert graph.edges == []


def test_a_syntax_error_names_the_file(tree):
    d = tree({"pkg/__init__.py": "", "pkg/bad.py": "def (\n"})
    with pytest.raises(SyntaxError) as excinfo:
        build_graph(d)
    assert excinfo.value.filename.endswith("bad.py")


def test_a_coding_cookie_is_honored(tree):
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "",
            "pkg/m.py": b"# -*- coding: latin-1 -*-\nx = '\xe9'\nimport pkg.b\n",
        }
    )
    assert edges(build_graph(d)) == {("pkg.m", "pkg.b")}


def test_the_graph_is_reproducible(tree):
    d = tree(
        {"pkg/__init__.py": "", "pkg/a.py": "import pkg.b", "pkg/b.py": "import pkg.a"}
    )
    assert build_graph(d) == build_graph(d)


def test_a_submodule_wins_over_an_attribute_of_the_same_name(tree):
    d = tree({"pkg/__init__.py": "thing = 1", "pkg/m.py": "from . import thing"})
    assert edges(build_graph(d)) == {("pkg.m", "pkg")}
    pathlib.Path(d, "thing.py").write_text("")
    assert edges(build_graph(d)) == {("pkg.m", "pkg.thing")}


def test_a_directory_without_an_init_is_not_a_package(tree):
    d = tree({"pkg/__init__.py": ""})
    with pytest.raises(ValueError, match="no __init__.py"):
        build_graph(str(pathlib.Path(d).parent))


def test_a_directory_whose_name_is_not_an_identifier(tmp_path):
    (tmp_path / "my-pkg").mkdir()
    (tmp_path / "my-pkg" / "__init__.py").write_text("")
    with pytest.raises(ValueError, match="not a valid package name"):
        build_graph(str(tmp_path / "my-pkg"))


def test_a_missing_directory(tmp_path):
    with pytest.raises(ValueError, match="not a package"):
        build_graph(str(tmp_path / "nope"))


@pytest.mark.parametrize("test", ['__name__ == "__main__"', '"__main__" == __name__'])
def test_main_block_is_skipped_but_its_else_is_kept(tree, test):
    """The demo block at the bottom of a module does not run when it is imported."""
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "",
            "pkg/c.py": "",
            "pkg/m.py": f"if {test}:\n    import pkg.b\nelse:\n    import pkg.c\n",
        }
    )
    assert edges(build_graph(d)) == {("pkg.m", "pkg.c")}


def test_other_name_comparisons_are_not_special(tree):
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "",
            "pkg/c.py": "",
            "pkg/m.py": """if __name__ == "pkg.m":
    import pkg.b
if __name__ != "__main__":
    import pkg.c
""",
        }
    )
    assert edges(build_graph(d)) == {("pkg.m", "pkg.b"), ("pkg.m", "pkg.c")}


def test_a_relative_import_above_the_package_is_skipped_with_a_warning(tree):
    """Such a statement fails at runtime as well; it occurs in template files."""
    d = tree({"pkg/__init__.py": "", "pkg/m.py": "from ... import x\nimport pkg.b\n"})
    warned = []
    graph = build_graph(d, warn=warned.append)
    assert edges(graph) == {("pkg.m", "pkg.b")}
    assert warned == [
        f"{os.path.join(d, 'm.py')}:1: relative import beyond the package, skipped"
    ]


def test_a_very_long_expression_does_not_overflow_the_stack(tree):
    """A generated lookup table nests thousands of BinOp nodes; a recursive visitor dies."""
    source = "x = " + " + ".join(["1"] * 3000) + "\nimport pkg.b\n"
    d = tree({"pkg/__init__.py": "", "pkg/m.py": source})
    assert edges(build_graph(d)) == {("pkg.m", "pkg.b")}


def test_module_file_names_need_not_be_identifiers(tree):
    """Django migrations are called 0001_initial.py and import like any other module."""
    d = tree(
        {"pkg/__init__.py": "", "pkg/db.py": "", "pkg/0001_initial.py": "import pkg.db"}
    )
    graph = build_graph(d)
    assert graph.nodes == ["pkg", "pkg.0001_initial", "pkg.db"]
    assert edges(graph) == {("pkg.0001_initial", "pkg.db")}
