"""Compatibility app entrypoint for gradual refactor.

This module intentionally delegates to the legacy monolith (org.py)
so runtime behavior stays identical while we extract components.
"""

from org import app as legacy_app


def create_app():
    return legacy_app


app = create_app()
