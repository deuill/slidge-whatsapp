from datetime import datetime
from pathlib import Path

project = "slidge-whatsapp"
copyright = f"{datetime.today().year}, the {project} contributors"
author = f"the {project} contributors"

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
autoapi_ignore = ["generated/*"]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "slixmpp": ("https://slixmpp.readthedocs.io/en/latest/", None),
    "slidge": ("https://slidge.im/docs/slidge/main/", None),
}

extlinks = {"xep": ("https://xmpp.org/extensions/xep-%s.html", "XEP-%s")}

html_theme = "furo"
html_theme_options = {
    "source_edit_link": f"https://codeberg.org/slidge/{project}/_edit/main/docs/source/{{filename}}",
    "source_view_link": f"https://codeberg.org/slidge/{project}/src/branch/main/docs/source/{{filename}}",
    "footer_icons": [
        {
            "name": "Codeberg",
            "url": f"https://codeberg.org/slidge/{project}",
            "html": Path("codeberg.svg").read_text(),
        },
    ],
}
