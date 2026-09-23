"""List the fewest import statements to remove to break all circular imports in a Python package."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Collection, Iterable

from .fas import minimum_feedback_arc_set
from .graph import Edge, Graph, build_graph
from .io import FORMATS, read_graph, write_graph

BOLD, RED, GREEN, GREY = "1", "31", "32", "90"


def colorize(text: str, *codes: str) -> str:
    if "NO_COLOR" in os.environ:
        return text
    if not (sys.stdout.isatty() or "GITHUB_ACTIONS" in os.environ):
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def display(path: str) -> str:
    """A path relative to the working directory when that is inside it, else as is."""
    try:
        relative = os.path.relpath(path)
    except ValueError:  # another drive on Windows
        return path
    return path if relative.startswith("..") else relative


def keyed_lines(graph: Graph, edges: Iterable[Edge]) -> list[tuple[str, Edge]]:
    """One ``path:line: imports module`` line per import statement behind the given edges,
    in file order, with its edge; ``module: imports module`` for a graph that has no
    locations."""
    keyed: list[tuple[tuple[str, int, str], str, Edge]] = []
    for edge in edges:
        src, dst = graph.names([edge])[0]
        where = graph.locations.get(edge)
        if not where:
            keyed.append(((src, 0, dst), f"{src}: imports {dst}", edge))
        for path, line in where or ():
            shown = display(path)
            keyed.append(((shown, line, dst), f"{shown}:{line}: imports {dst}", edge))
    return [(text, edge) for _, text, edge in sorted(keyed)]


def lines(graph: Graph, edges: Iterable[Edge]) -> list[str]:
    return [text for text, _ in keyed_lines(graph, edges)]


def print_lines(
    graph: Graph, edges: Iterable[Edge], blamed: Collection[Edge] = ()
) -> None:
    """Print the import statements behind the edges, those behind ``blamed`` in red."""
    for line, edge in keyed_lines(graph, edges):
        print(colorize(line, RED) if edge in blamed else line)


def dependencies(n: int) -> str:
    return f"{n} {'dependency' if n == 1 else 'dependencies'}"


def summary(graph: Graph, fas: Iterable[Edge]) -> str:
    """``2 dependencies to remove``, and when they span more import statements than that,
    ``11 dependencies (14 import statements) to remove``."""
    fas = list(fas)
    statements = len(lines(graph, fas))
    if statements == len(fas):
        return f"{dependencies(len(fas))} to remove"
    return f"{dependencies(len(fas))} ({statements} import statements) to remove"


def compare(old: Graph, new: Graph) -> int:
    """Print the import statements that still have to go, those this change added in red,
    and how the count changed."""
    old_fas = minimum_feedback_arc_set(old)
    new_fas = minimum_feedback_arc_set(new)
    before, after = len(old_fas), len(new_fas)
    difference = after - before

    if difference <= 0:
        print_lines(new, new_fas)
        if difference == 0:
            change = f"dependencies to remove unchanged at {after}"
        else:
            change = f"dependencies to remove decreased from {before} to {after}"
        print(colorize(change, GREEN, BOLD))
        return 0

    # Solve the new graph again without the edges the old solution already blamed, so what is
    # left to blame is what this change introduced. A heuristic: the old solution is not
    # necessarily a subset of the new graph's edges.
    kept = set(new.indices(old.names(old_fas)))
    blamed = set(
        minimum_feedback_arc_set(
            Graph(new.nodes, [e for e in new.edges if e not in kept], new.locations)
        )
    )

    if len(blamed) > difference:
        # Breaking exactly those is not the cheapest way back to the old count.
        print_lines(new, blamed, blamed)
        print(f"removing any {difference} of the following would undo the increase:")
        print_lines(new, new_fas)
    else:
        # At most before + difference = after edges that break every cycle: a minimum
        # solution, with what this change added in red.
        print_lines(new, blamed | kept, blamed)
    change = f"dependencies to remove increased from {before} to {after}"
    print(colorize(change, RED, BOLD))
    return 1


def load(
    path: str, exclude: str | None, inline: bool, parser: argparse.ArgumentParser
) -> Graph:
    """A package directory to analyze, or a graph file dumped earlier."""
    if os.path.isdir(path):
        return build_graph(path, exclude, inline, warn)
    if path.endswith(".py"):
        raise ValueError(f"{path}: is a module; pass its package directory instead")
    if exclude is not None or inline:
        parser.error(f"--exclude and --inline do not apply to a graph file: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            return read_graph(f)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None


def warn(message: str) -> None:
    print(colorize(f"uncycle: warning: {message}", GREY), file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(prog="uncycle", description=__doc__)
    parser.add_argument(
        "package",
        metavar="PACKAGE",
        help="a package directory, or a graph file written by --dump-graph",
    )
    parser.add_argument(
        "--exclude",
        metavar="REGEX",
        help="regex searched in module names to leave out of the graph; a package that "
        "matches is pruned along with everything under it",
    )
    parser.add_argument(
        "--inline",
        action="store_true",
        help="include imports inside functions and classes",
    )
    parser.add_argument(
        "--baseline",
        metavar="OLD",
        help="an older version of the package: list the import statements that still have "
        "to go, highlight the ones this version added, and exit 1 if more dependencies "
        "have to go than before",
    )
    parser.add_argument(
        "--dump-graph",
        metavar="FILE",
        help="write the import graph to FILE (- for stdout) instead of solving it",
    )
    parser.add_argument(
        "--format",
        choices=FORMATS,
        help="format of the dumped graph; default: text if FILE ends in .txt, else json",
    )
    args = parser.parse_args()

    try:
        graph = load(args.package, args.exclude, args.inline, parser)

        if args.dump_graph is not None:
            file = args.dump_graph
            format = args.format or ("text" if file.endswith(".txt") else "json")
            if file == "-":
                write_graph(graph, sys.stdout, format)
            else:
                with open(file, "w", encoding="utf-8") as f:
                    write_graph(graph, f, format)
            return 0

        if args.baseline is not None:
            return compare(
                load(args.baseline, args.exclude, args.inline, parser), graph
            )

        fas = minimum_feedback_arc_set(graph)
        print_lines(graph, fas)
        print(colorize(summary(graph, fas), BOLD))
        return 0
    except SyntaxError as e:
        # str(e) names only the file's basename, which is ambiguous in a package
        where = f"{display(e.filename)}:{e.lineno}" if e.filename else "?"
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        print(
            f"uncycle: {where}: {e.msg} (parsed with Python {version})", file=sys.stderr
        )
        return 2
    except (OSError, ValueError) as e:
        print(f"uncycle: {e}", file=sys.stderr)
        return 2
