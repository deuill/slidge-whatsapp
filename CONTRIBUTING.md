# Contributing

We are really happy to welcome new contributors to slidge-whatsapp!
Before starting anything, please join [our chat room](xmpp:slidge@conference.nicoco.fr?join)
to say hi and see if anyone is already working on the bug you want to fix or
the feature you want to implement.

## Quickstart

To start hacking on slidge-whatsapp, you need to:

1. Clone the repository: `git clone https://codeberg.org/slidge/slidge-whatsapp`.
2. Install the required dependencies. (see below)
3. Spin up a development XMPP server for slidge-whatsapp to connect to. (see below)

### Easy mode: docker-compose

The easiest way to achieve 2 and 3 at the same time is to use the provided
`docker-compose.yml` file. Run ``docker compose up`` (or ``podman-compose up``)
in the directory of the repository you just cloned. It will:

- spin up a [prosody](https://prosody.im) instance configured for development;
- launch slidge-whatsapp in a dedicated container, with hot code reload.

You will then be able to connect to that prosody using any XMPP client, using
"test@localhost" as JID and "password" as password. We recommended
[gajim](https://gajim.org) which lets you start a different profile than your
main profile: `gajim -p slidge -c ~/.local/share/gajim-slidge`.

To avoid having to accept self-signed certificates, you can add prosody's
certificate to your local store. In debian, you can do that with:

```bash
# download the certificate
curl https://codeberg.org/slidge/prosody-dev-container/raw/branch/main/localhost.crt | sudo tee /usr/local/share/ca-certificates/localhost.crt
# set the right perms
chmod 600 /usr/local/share/ca-certificates/localhost.crt
# import it
sudo update-ca-certificates
```

### Slightly harder mode: setting up a virtualenv

In some situations, developing in containers is not optimal, for instance if
you want to attach an interactive debugger to the slidge-whatsapp process.

slidge-whatsapp defines its dependencies in a [PEP 517](https://peps.python.org/pep-0517/)-compliant
`pyproject.toml` file. This means that you can use different tools to set up a
virtualenv with the appropriate dependencies.

We recommend using [uv](https://docs.astral.sh/uv/) which is fast and has some
nice features. By running ``uv sync --frozen --all-groups --all-extras``, a
standard virtualenv will be installed in `./.venv`. You can then activate it
with `source .venv/bin/activate` and launch `slidge-whatsapp --help`.

NB: you will need to set up a local XMPP server. An easy way to do that is to
use the `slidge-prosody-dev` container: `docker run --network host codeberg.org/slidge/prosody-slidge-dev`.
With it you will be able to launch slidge-whatsapp with
``slidge-whatsapp --jid slidge.localhost --secret secret --debug --home-dir ./persistent``.

### Hacking on slidge core simultaneously

Maybe you will discover that what you want to change, add or fix is not part
of slidge-whatsapp but part of slidge core. To modify slidge core, first you
need to clone [the slidge repo](https://codeberg.org/slidge/slidge) somewhere
on your computer.

Let's assume you cloned `slidge` in the same root dir as `slidge-whatsapp`,
e.g., `~/src/slidge-whatsapp` and `~/src/slidge`.
With the `docker-compose`-based dev setup, you will need to add an additional
mount for the slidge dir, e.g., `../slidge:/build/slidge`. If you opted for
the virtualenv solution, you can install slidge in your virtualenv in editable
mode with `[uv] pip install -e ../slidge`.

## Guidelines

slidge-whatsapp uses these tools to ensure some level of code quality:

- [mypy](https://www.mypy-lang.org/)
  for static type checking,
- [ruff](https://docs.astral.sh/ruff/)
  to detect common python mistakes and enforce a consistent style,
- [pytest](https://docs.pytest.org/en/stable/)
  for automated tests.

Commit messages should be in the form of
[conventional commits](https://www.conventionalcommits.org/en/v1.0.0/), with
some additional types defined in [./commitlinx.config.js](https://codeberg.org/slidge/slidge-whatsapp/src/branch/main/commitlint.config.js).
This makes it possible to automatically generate changelogs on releases, it
is worth it! We use [git-cliff](https://git-cliff.org/) for this. You don't have
to install git-cliff locally, the magic happens in CI.

We recommended setting up [pre-commit](https://pre-commit.com/) to ensure that
these pass for each commit: `pre-commit install && pre-commit install --hook-type commit-msg`.

Make a new git branch, commit your changes, push it to your fork and open a
pull request!

NB: we also accept contributions without pull requests. Just push your changes
somewhere and tell us where to pull via the group chat.
