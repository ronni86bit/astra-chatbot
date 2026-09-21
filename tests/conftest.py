"""conftest.py - adds src directory to path for tests and shares the engine"""

import sys
import os

import pytest

# Add the src directory to the path so rf_catalogue can be imported
src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

# Also add the project root
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


@pytest.fixture(scope="session")
def engine():
    """Session-scoped CatalogueEngine.

    The workbook load takes ~13s; the engine is read-only during tests,
    so one shared instance keeps the suite fast without weakening checks.
    """
    from rf_catalogue.engine import CatalogueEngine

    return CatalogueEngine()