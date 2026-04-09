"""Shared fixtures for factory tests."""
import sys
from pathlib import Path

# Add factory dir to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent))
