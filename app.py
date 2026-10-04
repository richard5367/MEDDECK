"""WSGI entrypoint for Vercel.

Vercel's Flask preset looks for a Flask instance named ``app`` in a fixed list of
filenames at the project root, and ``app.py`` is the first of them. The
implementation lives in ``meddeck.py``; this module simply re-exports the
instance so the project deploys whether or not the ``tool.vercel.entrypoint``
setting in pyproject.toml is picked up.

Importing ``meddeck`` runs ``create_app()``, which is where the database schema
and the secret key are set up.
"""

from meddeck import app

__all__ = ["app"]