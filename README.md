# slidge-whatsapp

A
[feature-rich](https://slidge.im/docs/slidge-whatsapp/main/user/features.html)
[WhatsApp](https://whatsapp.com) to
[XMPP](https://xmpp.org/) puppeteering
[gateway](https://xmpp.org/extensions/xep-0100.html), based on
[slidge](https://slidge.im) and
[whatsmeow](https://github.com/tulir/whatsmeow).

[![CI pipeline status](https://ci.codeberg.org/api/badges/14066/status.svg)](https://ci.codeberg.org/repos/14066)
[![Chat](https://conference.nicoco.fr:5281/muc_badge/slidge@conference.nicoco.fr)](https://slidge.im/xmpp-web/#/guest?join=slidge@conference.nicoco.fr)
[![PyPI package version](https://badge.fury.io/py/slidge-whatsapp.svg)](https://pypi.org/project/slidge-whatsapp/)




slidge-whatsapp lets you chat with users of WhatsApp without leaving your favorite XMPP client.

## Quickstart

```sh
docker run codeberg.org/slidge/slidge-whatsapp \  # works with podman too
    --jid whatsapp.example.org \  # can be whatever you want it to be
    --secret some-secret \  # must match your XMPP server config
    --home-dir /somewhere/writeable  # for data persistence
```

Use the `:latest-amd64` tag for the latest release, `:vX.X.X-amd64` for release X.X.X, and `:main-amd64`
for the bleeding edge.`-arm64` images are also available.

If you do not like containers, other installation methods are detailed
[in the docs](https://slidge.im/docs/slidge-whatsapp/main/admin/install.html).

## Documentation

Hosted on [codeberg pages](https://slidge.im/docs/slidge-whatsapp/main/).

## Contributing

Contributions are **very** welcome, and we tried our best to make it easy
to start hacking on slidge-whatsapp. See [CONTRIBUTING.md](./CONTRIBUTING.md).
