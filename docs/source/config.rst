Configuration
=============

Minimal setup
-------------

For a minimal ``prosody <https://prosody.im>``_-based setup, add these lines at the bottom of
``/etc/prosody/prosody.cfg.lua``:

.. code-block:: lua

  Component "slidge-whatsapp.example.org"
      component_secret = "secret"

  Component "upload.example.org" "http_file_share"
     http_file_share_access = { "slidge-whatsapp.example.org" }

And start slidge-whatsapp with:

.. code-block:: bash

  slidge-whatsapp \
    --jid slidge-whatsapp.example.org \
    --secret secret \
    --home-dir /somewhere/writable

Advanced usage
--------------

Refer to the `slidge admin docs <https://slidge.im/docs/slidge/main/admin>`_ for more
advanced setups and examples of configuration for other XMPP servers.

You will probably want to add support for `attachments <https://slidge.im/docs/slidge/main/admin/attachments.html>`_
received from WhatsApp, and set up slidge-whatsapp as a `privileged component <https://slidge.im/docs/slidge/main/admin/privilege.html>`_
for better UX.

slidge-whatsapp-specific config
-------------------------------

All `generic slidge configuration options <https://slidge.im/docs/slidge/main/admin/config/#common-config>`_
apply.
slidge-whatsapp provides these additional component-wide options:

.. config-obj:: slidge_whatsapp.config
