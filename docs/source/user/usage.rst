Usage
=====

.. note::
   Slidge uses WhatsApp's `Linked Devices <https://faq.whatsapp.com/378279804439436/>`_ feature,
   which may require periodic re-linking against the official client. However, you can still not
   use or even uninstall the official client between re-linking.

Roster
******

Contact JIDs are of the form ``+<phone-number>@slidge-whatsapp.example.org``, where
``<phone-number>`` is the contact's phone number in international format (e.g. ``+442087599036``.
Contacts will be added to the roster as they engage with your account, and may not all appear at
once as they exist in the official client.

Presences
*********

Your contacts' presence will appear as either "online" when the contact is currently using the
WhatsApp client, or "away" otherwise; their last interaction time will also be noted if you've
chosen to share this in the privacy settings of the official client.

Broadcast Messages
******************

Broadcasts are only partially supported; specifically, only Broadcasts sent by other users will be
conveyed via XMPP, and displayed inline with other messages from the sending user, as in the
official WhatsApp clients. Broadcast messages cannot currently be sent via XMPP, and Broadcasts
sent via the official WhatsApp client on the primary device will not be copied to XMPP.
