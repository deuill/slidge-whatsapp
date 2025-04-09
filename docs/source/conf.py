# DO NOT EDIT
# Generated from .copier-answers.yml

import os
from datetime import datetime
from pathlib import Path

from slidge import __version__ as slidge_version

project = "slidge-whatsapp"
copyright = f"{datetime.today().year}, the {project} contributors"
author = f"the {project} contributors"
branch = os.getenv("CI_COMMIT_BRANCH", "main")

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.extlinks",
    "sphinx.ext.viewcode",
    "sphinx.ext.autodoc.typehints",
    "autoapi.extension",
    "slidge_sphinx_extensions.doap",
    "slidge_sphinx_extensions.config_obj",
    "sphinx_mdinclude",
]

autodoc_typehints = "description"

# Include __init__ docstrings
autoclass_content = "both"
autoapi_python_class_content = "both"

autoapi_type = "python"
autoapi_dirs = ["../../slidge_whatsapp"]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "slixmpp": ("https://slixmpp.readthedocs.io/en/latest/", None),
    "slidge": (f"https://slidge.im/docs/slidge/{slidge_version}/", None),
}

extlinks = {"xep": ("https://xmpp.org/extensions/xep-%s.html", "XEP-%s")}

html_theme = "furo"
html_theme_options = {
    "source_edit_link": f"https://codeberg.org/slidge/{project}/_edit/{branch}/docs/source/{{filename}}",
    "source_view_link": f"https://codeberg.org/slidge/{project}/src/branch/{branch}/docs/source/{{filename}}",
    "footer_icons": [
        {
            "name": "Codeberg",
            "url": f"https://codeberg.org/slidge/{project}",
            "html": Path("codeberg.svg").read_text(),
        },
    ],
}
