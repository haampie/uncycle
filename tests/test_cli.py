import json
import os
import re
import sys

import pytest

from uncycle import cli

CYCLE = {
    "pkg/__init__.py": "",
    "pkg/a.py": "import pkg.b",
    "pkg/b.py": "import pkg.a",
    "pkg/lonely.py": "",
}


@pytest.fixture
def run(monkeypatch):
    """Run the command line with plain output, even on GitHub Actions."""
    monkeypatch.setenv("NO_COLOR", "1")

    def go(*argv):
        monkeypatch.setattr(sys, "argv", ["uncycle", *argv])
        return cli.main()

    return go


def test_graph_to_stdout(tree, run, capsys):
    assert run(tree(CYCLE), "--dump-graph", "-") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["nodes"] == ["pkg", "pkg.a", "pkg.b", "pkg.lonely"]
    assert data["edges"] == [[1, 2], [2, 1]]


@pytest.mark.parametrize("name,first", [("g.json", "{"), ("g.txt", "4")])
def test_the_format_follows_the_output_name(tree, run, tmp_path, name, first):
    out = tmp_path / name
    assert run(tree(CYCLE), "--dump-graph", str(out)) == 0
    assert out.read_text().startswith(first)


def test_a_dumped_graph_can_be_read_back(tree, run, tmp_path, capsys):
    out = tmp_path / "g.txt"
    assert run(tree(CYCLE), "--dump-graph", str(out)) == 0
    capsys.readouterr()
    assert run(str(out)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert re.fullmatch(r"pkg\.[ab]: imports pkg\.[ab]", lines[0])
    assert lines[-1] == "1 dependency to remove"


def test_extraction_flags_do_not_apply_to_a_graph_file(tree, run, tmp_path):
    out = tmp_path / "g.json"
    run(tree(CYCLE), "--dump-graph", str(out))
    with pytest.raises(SystemExit) as excinfo:
        run(str(out), "--inline")
    assert excinfo.value.code == 2


def test_solve(tree, run, capsys):
    assert run(tree(CYCLE)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert re.search(r"pkg/[ab]\.py:1: imports pkg\.[ab]$", lines[0])
    assert lines[-1] == "1 dependency to remove"


def test_every_statement_behind_an_edge_is_listed(tree, run, capsys):
    """a -> b is on both cycles, so it is the unique answer, and it has two statements."""
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "import pkg.b\nfrom pkg import b\n",
            "pkg/b.py": "import pkg.a\nimport pkg.c\n",
            "pkg/c.py": "import pkg.a",
        }
    )
    assert run(d) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].endswith("pkg/a.py:1: imports pkg.b")
    assert lines[1].endswith("pkg/a.py:2: imports pkg.b")
    assert lines[2] == "1 dependency (2 import statements) to remove"


def test_no_cycles(tree, run, capsys):
    assert run(tree({"pkg/__init__.py": "", "pkg/a.py": "import pkg"})) == 0
    assert capsys.readouterr().out == "0 dependencies to remove\n"


def test_a_syntax_error_in_the_package(tree, run, capsys):
    assert run(tree({"pkg/__init__.py": "", "pkg/bad.py": "def (\n"})) == 2
    assert "bad.py" in capsys.readouterr().err


def test_a_syntax_error_names_the_path_and_line(tree, run, capsys, monkeypatch):
    """Not just ``invalid syntax (utils.py, line 2)``: packages have many utils.py."""
    d = tree(
        {
            "pkg/__init__.py": "",
            "pkg/utils.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/utils.py": "x = 1\ndef (\n",
        }
    )
    monkeypatch.chdir(d)
    assert run(".") == 2
    err = capsys.readouterr().err
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert err.startswith(f"uncycle: {os.path.join('sub', 'utils.py')}:2: ")
    assert err.rstrip().endswith(f"(parsed with Python {version})")


def test_compare_unchanged(tree, run, capsys):
    """What is left is listed, and the count."""
    d = tree(CYCLE)
    assert run(d, "--baseline", d) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert re.search(r"pkg/[ab]\.py:1: imports pkg\.[ab]$", lines[0])
    assert lines[1] == "dependencies to remove unchanged at 1"


def test_compare_improved_with_cycles_left(tmp_path, run, capsys):
    """Statements that still have to go are listed."""
    for name, source in {
        "old/pkg/__init__.py": "",
        "old/pkg/a.py": "import pkg.b",
        "old/pkg/b.py": "import pkg.a",
        "old/pkg/c.py": "import pkg.d",
        "old/pkg/d.py": "import pkg.c",
        "new/pkg/__init__.py": "",
        "new/pkg/a.py": "import pkg.b",
        "new/pkg/b.py": "",
        "new/pkg/c.py": "import pkg.d",
        "new/pkg/d.py": "import pkg.c",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    assert (
        run(str(tmp_path / "new" / "pkg"), "--baseline", str(tmp_path / "old" / "pkg"))
        == 0
    )
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert re.search(r"new/pkg/[cd]\.py:1: imports pkg\.[cd]$", lines[0])
    assert lines[1] == "dependencies to remove decreased from 2 to 1"


def test_compare_improved(tmp_path, run, capsys):
    for name, source in {
        "old/pkg/__init__.py": "",
        "old/pkg/a.py": "import pkg.b",
        "old/pkg/b.py": "import pkg.a",
        "new/pkg/__init__.py": "",
        "new/pkg/a.py": "import pkg.b",
        "new/pkg/b.py": "",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    assert (
        run(str(tmp_path / "new" / "pkg"), "--baseline", str(tmp_path / "old" / "pkg"))
        == 0
    )
    assert capsys.readouterr().out == "dependencies to remove decreased from 1 to 0\n"


def test_compare_worse(tmp_path, run, capsys):
    for name, source in {
        "old/pkg/__init__.py": "",
        "old/pkg/a.py": "",
        "old/pkg/b.py": "",
        "new/pkg/__init__.py": "",
        "new/pkg/a.py": "import pkg.b",
        "new/pkg/b.py": "import pkg.a",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    assert (
        run(str(tmp_path / "new" / "pkg"), "--baseline", str(tmp_path / "old" / "pkg"))
        == 1
    )
    lines = capsys.readouterr().out.splitlines()
    assert re.search(r"new/pkg/[ab]\.py:1: imports pkg\.[ab]$", lines[0])
    assert lines[-1] == "dependencies to remove increased from 0 to 1"


def test_compare_worse_lists_what_was_left_too(tmp_path, run, capsys, monkeypatch):
    """The old cycle's statement is listed plain, the new cycle's in red."""
    for name, source in {
        "old/pkg/__init__.py": "",
        "old/pkg/a.py": "import pkg.b",
        "old/pkg/b.py": "import pkg.a",
        "old/pkg/c.py": "",
        "old/pkg/d.py": "",
        "new/pkg/__init__.py": "",
        "new/pkg/a.py": "import pkg.b",
        "new/pkg/b.py": "import pkg.a",
        "new/pkg/c.py": "import pkg.d",
        "new/pkg/d.py": "import pkg.c",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("GITHUB_ACTIONS", "1")
    assert (
        run(str(tmp_path / "new" / "pkg"), "--baseline", str(tmp_path / "old" / "pkg"))
        == 1
    )
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert re.fullmatch(r"\S*new/pkg/[ab]\.py:1: imports pkg\.[ab]", lines[0])
    assert re.fullmatch(
        r"\033\[31m\S*new/pkg/[cd]\.py:1: imports pkg\.[cd]\033\[0m", lines[1]
    )
    assert "dependencies to remove increased from 1 to 2" in lines[2]


def test_compare_when_a_blamed_edge_is_gone(tmp_path, run, capsys):
    """The old solution names pkg.a -> pkg.b, which the new tree does not have at all."""
    for name, source in {
        "old/pkg/__init__.py": "",
        "old/pkg/a.py": "import pkg.b",
        "old/pkg/b.py": "import pkg.a",
        "old/pkg/c.py": "",
        "new/pkg/__init__.py": "",
        "new/pkg/a.py": "",
        "new/pkg/b.py": "import pkg.c",
        "new/pkg/c.py": "import pkg.b",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    assert (
        run(str(tmp_path / "new" / "pkg"), "--baseline", str(tmp_path / "old" / "pkg"))
        == 0
    )
    assert (
        capsys.readouterr().out.splitlines()[-1]
        == "dependencies to remove unchanged at 1"
    )


@pytest.mark.parametrize(
    "env,colored",
    [
        ({}, False),
        ({"GITHUB_ACTIONS": "1"}, True),
        ({"GITHUB_ACTIONS": "1", "NO_COLOR": "1"}, False),
    ],
)
def test_color(monkeypatch, env, colored):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert ("\033[" in cli.colorize("hi", "1")) is colored


def test_a_module_file_instead_of_its_package(tree, run, capsys):
    d = tree(CYCLE)
    assert (
        run(
            d + "/a.py",
        )
        == 2
    )
    assert "is a module; pass its package directory" in capsys.readouterr().err


def test_a_file_that_is_not_a_graph_names_the_file(tmp_path, run, capsys):
    f = tmp_path / "notes.txt"
    f.write_text("hello\n")
    assert run(str(f)) == 2
    assert capsys.readouterr().err.startswith(f"uncycle: {f}: not a graph file")


def test_a_relative_import_above_the_package_warns_on_stderr(tree, run, capsys):
    d = tree({"pkg/__init__.py": "", "pkg/m.py": "from ... import x\n"})
    assert run(d) == 0
    out, err = capsys.readouterr()
    assert out == "0 dependencies to remove\n"
    assert err.startswith("uncycle: warning: ") and "m.py:1" in err
