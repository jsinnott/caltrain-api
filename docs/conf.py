"""Sphinx configuration for caltrain-api.

Published by Read the Docs (see .readthedocs.yaml). Build locally with the
same strictness -- any warning, including a broken cross-reference, fails:

    uv sync --extra docs
    .venv/bin/sphinx-build -W --keep-going -b html docs docs/_build/html
"""

from importlib.metadata import version as _version

project = "caltrain-api"
author = "Joe Sinnott"
copyright = "2026, Joe Sinnott"
release = _version("caltrain-api")
version = release

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",        # Google-style Args:/Returns:/Raises: sections
    "sphinx.ext.intersphinx",     # links to Python / pydantic / httpx docs
    "sphinx.ext.viewcode",        # "[source]" links
    "myst_parser",                # Markdown pages (README, data findings)
    "sphinxcontrib.autodoc_pydantic",
]

exclude_patterns = ["_build"]
nitpicky = True  # warn on every unresolvable reference (local and Read the Docs alike)
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}

# -- autodoc -------------------------------------------------------------------
autodoc_member_order = "bysource"
autodoc_typehints = "signature"
autodoc_class_signature = "separated"
autodoc_default_options = {"members": True, "show-inheritance": True}
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_rtype = False

# Pydantic models: list fields with their descriptions; hide JSON schema,
# config and validator noise that callers don't need.
autodoc_pydantic_model_show_json = False
autodoc_pydantic_model_show_config_summary = False
autodoc_pydantic_model_show_validator_summary = False
autodoc_pydantic_model_show_validator_members = False
autodoc_pydantic_model_show_field_summary = False
autodoc_pydantic_model_member_order = "bysource"
autodoc_pydantic_field_list_validators = False
autodoc_pydantic_field_show_constraints = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pydantic": ("https://docs.pydantic.dev/latest", None),
    # httpx's docs are MkDocs, with no Sphinx inventory to link against.
}

# Names in rendered type annotations that have no link target. They still
# display; this just stops the strict (-n) build from failing on them.
nitpick_ignore = [
    ("py:class", "httpx.Client"),
    ("py:class", "AwareDatetime"),   # pydantic.AwareDatetime, rendered unqualified
    ("py:class", "date"),            # datetime.date, rendered unqualified
    ("py:class", "timedelta"),       # datetime.timedelta, rendered unqualified
]


def _hide_private_bases(app, name, obj, options, bases):
    """Show the internal frozen-model base class as pydantic.BaseModel."""
    from pydantic import BaseModel

    bases[:] = [BaseModel if b.__name__ == "_Frozen" else b for b in bases]


def setup(app):
    app.connect("autodoc-process-bases", _hide_private_bases)

# -- MyST ----------------------------------------------------------------------
myst_heading_anchors = 3

# -- HTML ----------------------------------------------------------------------
html_theme = "furo"
html_title = f"caltrain-api {release}"
