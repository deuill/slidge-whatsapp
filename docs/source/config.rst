Configuration
=============

Minimal setup
-------------

For a minimal ``prosody <https://prosody.im>`` based setup, add these lines at the bottom of
``/etc/prosody/prosody.cfg.lua``:

.. code-block:: lua

  Component "slidge-whatsapp.example.org"
      component_secret = "secret"

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
received from WhatsApp, and setup slidge-whatsapp as a `privileged component <https://slidge.im/docs/slidge/main/admin/privilege.html>`_
for better UX.

Optional dependencies
---------------------

WhatsApp requires that image, audio, and video attachments are sent in
specific formats; these formats are generaly incompatible with prevailing
standards across XMPP clients.

Thus, sending attachments with full client compatibility requires that we
convert these on-the-fly; this requires that FFmpeg is installed. If a
valid FFmpeg installation is not found, attachments will still be sent in
their original formats, which may cause these to appear as "document"
attachments in official WhatsApp clients.

FFmpeg is widely used and packaged -- please refer to your distribution's
documentation on how to install the FFmpeg package.

slidge-whatsapp-specific config
-------------------------------

All `generic slidge configuration options <https://slidge.im/docs/slidge/main/admin/config/#common-config>`_
apply.
slidge-whatsapp provides these additional component-wide options:

.. config-obj:: slidge_whatsapp.config
