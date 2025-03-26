# slidge-whatsapp

A
[feature-rich](https://slidge.im/docs/slidge-whatsapp/main/features.html)
[WhatsApp](https://whatsapp.com) to
[XMPP](https://xmpp.org/) puppeteering
[gateway](https://xmpp.org/extensions/xep-0100.html), based on
[slidge](https://slidge.im) and
[whatsmeow](https://github.com/tulir/whatsmeow).

[![PyPI package version](https://badge.fury.io/py/slidge-whatsapp.svg)](https://pypi.org/project/slidge-whatsapp/)
[![CI pipeline status](https://ci.codeberg.org/api/badges/14066/status.svg)](https://ci.codeberg.org/repos/14066)
[![Chat](https://conference.nicoco.fr:5281/muc_badge/slidge@conference.nicoco.fr)](https://slidge.im/xmpp-web/#/guest?join=slidge@conference.nicoco.fr)


## Installation

Refer to the [slidge admin documentation](https://slidge.im/docs/slidge/main/admin/)
for general info on how to set up an XMPP server component.

### Containers

From [the codeberg package registry](https://codeberg.org/slidge/-/packages/container/slidge-whatsapp/latest)

```sh
docker run codeberg.org/slidge/slidge-whatsapp  # works with podman too
```

Use the `:latest-amd64` tag for the latest release, `:vX.X.X-amd64` for release X.X.X, and `:main-amd64`
for the bleeding edge. You can substitute change `-amd64` to `-arm64` if necessary.

### Python package

With [pipx](https://pypa.github.io/pipx/):

```sh

# for the latest stable release (if any)
pipx install slidge-whatsapp

# for the bleeding edge
pipx install slidge-whatsapp==0.0.0.dev0 \
    --pip-args='--extra-index-url https://codeberg.org/api/packages/slidge/pypi/simple/'

# to update bleeding edge installs
pipx install slidge-whatsapp==0.0.0.dev0 \
    --pip-args='--extra-index-url https://codeberg.org/api/packages/slidge/pypi/simple/' --force

slidge-whatsapp --help
```

## Documentation

Hosted on [codeberg pages](https://slidge.im/docs/slidge-whatsapp/main/).

## Dev

```sh
git clone https://codeberg.org/slidge/slidge-whatsapp
cd slidge-whatsapp
docker-compose up  # works with podman-compose too
```
