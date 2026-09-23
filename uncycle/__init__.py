"""Find the fewest import statements to remove to break all circular imports."""

from .fas import minimum_feedback_arc_set
from .graph import Edge, Graph, build_graph
from .io import FORMATS, read_graph, write_graph

__version__ = "0.1.3"

__all__ = [
    "FORMATS",
    "Edge",
    "Graph",
    "build_graph",
    "minimum_feedback_arc_set",
    "read_graph",
    "write_graph",
]
