Configuration
=============

Setting up a slidge component
-----------------------------

Refer to the `slidge admin docs <https://slidge.im/docs/slidge/main/admin>`_ for generic
instructions on how to set up a slidge component, and for slidge core
configuration options.

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

.. config-obj:: slidge_whatsapp.config
