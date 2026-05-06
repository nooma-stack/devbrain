"""factory.graph — Phase 5 graph layer for DevBrain.

Provides bounded multi-hop traversal over the memory_dependencies table
using recursive CTEs (no Apache AGE). See docs/plans/2026-05-05-phase-5-graph-layer-design.md.
"""
from graph.walker import GraphWalkResult, MemoryRef, EdgeRef, walk

__all__ = ["GraphWalkResult", "MemoryRef", "EdgeRef", "walk"]
