"""Opt-in test boundary, checked before any SQLite filesystem operation."""
import os
from pathlib import Path


def isolated_database(path):
    resolved = Path(path).resolve()
    root = os.environ.get('MIA_ISOLATED_ROOT')
    if root:
        boundary = Path(root).resolve()
        default = Path(__file__).resolve().parents[2] / 'data' / 'market_agent.db'
        if resolved == default.resolve() or not resolved.is_relative_to(boundary) or resolved == boundary:
            raise ValueError('MIA_TEST_DATABASE_OUTSIDE_ISOLATION')
    return resolved
