import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from slidge.db.meta import JSONSerializable
from slidge.group import LegacyBookmarks, LegacyMUC, LegacyParticipant, MucType
from slidge.util.types import Hat, HoleBound, MucAffiliation, XMPPMessage
from slixmpp.exceptions import XMPPError

from .generated import go, whatsapp
from .mixins import AvatarMixin, RecipientMixin, strip_quote_prefix

if TYPE_CHECKING:
    from .contact import Contact
    from .session import Session


class Participant(LegacyParticipant["Contact"]):
    muc: "MUC"
    session: "Session"

    def online(
        self,
        status: str | None = None,
        last_seen: datetime | None = None,
    ) -> None:
        if self.is_user:
            # "user participant" presences are not something we want to bridge
            # on joining a MUC, slidge sends a basic "online" presence for the user,
            # and we have no reason to ever send another one.
            return
        if self.contact is None:
            super().online(status, last_seen)
        else:
            self.contact.online(status, last_seen)

    def update_whatsapp_info(self, data: whatsapp.GroupParticipant) -> None:
        if data.Affiliation == whatsapp.GroupAffiliationAdmin:
            # Only owners can change the group name according to
            # XEP-0045, so we make all "WA admins" "XMPP owners"
            self.affiliation = "owner"
            self.role = "moderator"
        elif data.Affiliation == whatsapp.GroupAffiliationOwner:
            # The WA owner is in fact the person who created the room
            self.set_hats(
                [Hat("https://slidge.im/hats/slidge-whatsapp/owner", "Owner")]
            )
            self.affiliation = "owner"
            self.role = "moderator"
        else:
            self.affiliation = "member"
            self.role = "participant"

    @property
    def lid(self) -> str:
        return self.occupant_id.removesuffix("@lid")

    async def on_set_affiliation(  # type:ignore[override]  # ty:ignore[invalid-method-override]
        self,
        contact: "Contact",
        affiliation: MucAffiliation,
        reason: str | None,
        nickname: str | None,
    ) -> None:
        if affiliation == "member":
            participant = await self.muc.get_participant_by_contact(  # type:ignore[call-overload]  # ty:ignore
                contact, create=False
            )
            if participant is None or participant.affiliation in ("outcast", "none"):
                action = whatsapp.GroupParticipantActionAdd
            elif participant.affiliation == "member":
                return
            else:
                action = whatsapp.GroupParticipantActionDemote
        elif affiliation in ("admin", "owner"):
            action = whatsapp.GroupParticipantActionPromote
        elif affiliation == "outcast" or affiliation == "none":
            action = whatsapp.GroupParticipantActionRemove
        else:
            raise XMPPError(
                "bad-request",
                f"You can't make a participant '{affiliation}' in WhatsApp",
            )
        self.session.whatsapp.UpdateGroupParticipants(
            contact.legacy_id,
            whatsapp.Slice_whatsapp_GroupParticipant(  # type:ignore[no-untyped-call]
                [
                    whatsapp.GroupParticipant(  # type:ignore[no-untyped-call]
                        Actor=whatsapp.Actor(JID=contact.legacy_id),  # type:ignore[no-untyped-call]
                        Action=action,
                    )
                ]
            ),
        )


class MUC(RecipientMixin, AvatarMixin, LegacyMUC[Participant]):
    session: "Session"

    HAS_DESCRIPTION = False
    REACTIONS_SINGLE_EMOJI = True
    _ALL_INFO_FILLED_ON_STARTUP = True

    _history_requested: bool = False

    @property
    def history_requested(self) -> bool:
        return self._history_requested

    @history_requested.setter
    def history_requested(self, flag: bool) -> None:
        if self._history_requested == flag:
            return
        self._history_requested = flag
        self.commit()

    def serialize_extra_attributes(self) -> JSONSerializable:
        return {"history_requested": self._history_requested}

    def deserialize_extra_attributes(self, data: JSONSerializable) -> None:
        self._history_requested = bool(data.get("history_requested", False))

    async def update_info(self) -> None:
        # stuff happens in self.update_whatsapp_info()
        pass

    async def backfill(
        self,
        after: HoleBound | None = None,
        before: HoleBound | None = None,
    ) -> None:
        """
        Request history for messages older than the oldest message given by ID and date.
        """

        if before is None:
            return
            # WhatsApp requires a full reference to the last seen message in performing on-demand sync.

        if self.history_requested:
            return
            # With whatsmeow, we don't have to fill holes due to slidge downtime: we receive missed messages
            # on startup, as long as we have not been logged out by WhatsApp

        assert isinstance(before.id, str)
        oldest_message = whatsapp.Message(  # type:ignore[no-untyped-call]
            ID=before.id,
            Actor=await self.get_wa_actor(before.id),
            Timestamp=int(before.timestamp.timestamp()),
        )
        self.session.whatsapp.RequestMessageHistory(self.legacy_id, oldest_message)  # type:ignore[no-untyped-call]
        self.history_requested = True

    def get_sender_lid(self, legacy_msg_id: str) -> str:
        for message in self.get_archived_messages(legacy_msg_id):
            break
        else:
            raise XMPPError(
                "internal-server-error", f"Message {legacy_msg_id} is not in archive"
            )
        occupant_id = message.occupant_id
        if occupant_id == "slidge-user":
            return self.session.contacts.user_legacy_id  # type:ignore
        if occupant_id.endswith("@lid"):
            return occupant_id
        # this part _should_ not be reached, but it is a safeguard against sending
        # bad stuff to whatsapp
        raise XMPPError(
            "internal-server-error",
            f"Stored message sender is not a LID: {occupant_id}",
        )

    async def update_whatsapp_info(self, info: whatsapp.Group) -> None:
        """
        Set MUC information based on WhatsApp group information, which may or may not be partial in
        case of updates to existing MUCs.
        """
        with self.updating_info():
            self.type = MucType.GROUP
            if info.Nickname:
                self.user_nick = info.Nickname
            if info.Name:
                self.name = info.Name
            if info.Subject.Subject:
                self.subject = info.Subject.Subject
                if info.Subject.SetAt:
                    set_at = datetime.fromtimestamp(info.Subject.SetAt, tz=UTC)
                    self.subject_date = set_at
                if info.Subject.SetBy and info.Subject.SetBy.JID:
                    self.subject_setter = await self.get_participant_by_actor(
                        info.Subject.SetBy
                    )

            await self.update_whatsapp_avatar()
            self.n_participants = len(info.Participants)
            for wa_part in info.Participants:
                assert isinstance(wa_part, whatsapp.GroupParticipant)
                participant = await self.get_participant_by_actor(
                    wa_part.Actor,
                    wa_part.Nickname,
                    create=wa_part.Action != whatsapp.GroupParticipantActionRemove,
                )
                if participant is None:
                    continue
                if wa_part.Action == whatsapp.GroupParticipantActionRemove:
                    self.remove_participant(participant)
                else:
                    participant.update_whatsapp_info(wa_part)

    async def replace_mentions(self, text: str) -> str:
        # TODO: ideally, we shouldn't parse the text looking for mentions of any participant
        #       here, but instead rely on the explicit mentions of whatsapp.Message.MentionJIDs
        mapping: dict[str, str] = {}
        async for p in self.get_participants():
            if p.contact is not None:
                mapping[p.contact.phone] = p.nickname
            mapping[p.lid] = p.nickname
        if self.session.user_phone:
            mapping[self.session.user_phone.removeprefix("+")] = self.user_nick

        return replace_whatsapp_mentions(text, mapping=mapping)

    async def on_avatar(self, data: bytes | None, mime: str | None) -> None:
        self.session.whatsapp.SetAvatar(
            self.legacy_id,
            go.Slice_byte.from_bytes(data) if data else go.Slice_byte(),  # type:ignore[no-untyped-call]
        )

    async def on_set_config(
        self,
        name: str | None,
        description: str | None,
    ) -> None:
        # there are no group descriptions in WA, but topics=subjects
        if self.name != name:
            self.session.whatsapp.SetGroupName(self.legacy_id, name)  # type:ignore[no-untyped-call]

    async def on_set_subject(self, subject: str) -> None:
        if self.subject != subject:
            self.session.whatsapp.SetGroupTopic(self.legacy_id, subject)  # type:ignore[no-untyped-call]

    async def on_moderate(self, legacy_msg_id: str, reason: str | None) -> None:
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            Kind=whatsapp.MessageRevoke,
            ID=legacy_msg_id,
            Chat=self.get_wa_chat(),
            OriginActor=await self.get_wa_actor(legacy_msg_id),
        )
        self.session.whatsapp.SendMessage(message)  # type:ignore[no-untyped-call]
        # Apparently, no revoke event is received by whatsmeow after sending
        # the revoke message, so we need to "echo" it here.
        part = await self.get_user_participant()
        part.moderate(legacy_msg_id)

    async def on_leave_group(self, legacy_muc_id: str) -> None:
        """
        Removes own user from given WhatsApp group.
        """
        self.session.whatsapp.LeaveGroup(legacy_muc_id)  # type:ignore[no-untyped-call]

    def get_wa_chat(self) -> whatsapp.Chat:
        return whatsapp.Chat(JID=self.legacy_id, IsGroup=True)  # type:ignore[no-untyped-call]

    async def get_participant_by_actor(
        self, actor: whatsapp.Actor, nickname: str = "", create: bool = True
    ) -> Participant | None:
        if actor.IsMe:
            return await self.get_user_participant(occupant_id=actor.LID)
        elif actor.JID:
            assert isinstance(actor.JID, str)
            assert isinstance(actor.LID, str)
            # call-overload? because https://github.com/python/mypy/issues/14764
            # FIXME?: missing overload in slidge core
            return await self.get_participant_by_legacy_id(  # type:ignore[call-overload,no-any-return]
                actor.JID, occupant_id=actor.LID, create=create
            )
        else:
            if not actor.LID:
                return None
            return await self.get_participant(  # type:ignore[call-overload,no-any-return]
                nickname,
                occupant_id=actor.LID,
                create=create,
            )

    async def get_wa_actor(self, legacy_msg_id: str) -> whatsapp.Actor:
        lid = self.get_sender_lid(legacy_msg_id)
        jid = ""
        # I don't think we need a JID in groups now, but here is how we could get it
        # part = await self.get_participant(occupant_id=lid)
        # if part.contact:
        #     jid = part.contact.legacy_id
        # elif part.is_user:
        #     jid = self.session.contacts.user_legacy_id
        return whatsapp.Actor(  # type:ignore[no-untyped-call]
            JID=jid,
            LID=lid or "",
            IsMe=self.session.message_is_carbon(self, legacy_msg_id),
        )

    def _set_reply_to(self, xmpp_msg: XMPPMessage, wa_msg: whatsapp.Message) -> None:
        wa_msg.OriginActor.GroupJID = self.legacy_id

        if not xmpp_msg.reply:
            return

        wa_msg.ReplyID = xmpp_msg.reply.msg_id

        if xmpp_msg.reply.fallback:
            wa_msg.ReplyBody = strip_quote_prefix(xmpp_msg.reply.fallback)
            wa_msg.Body = wa_msg.Body.lstrip()

        if not xmpp_msg.reply.to:
            wa_msg.OriginActor.IsMe = False
            wa_msg.OriginActor.JID = self.session.contacts.user_legacy_id
            return

        xmpp_msg.reply.to = cast(Participant, xmpp_msg.reply.to)
        wa_msg.OriginActor.IsMe = xmpp_msg.reply.to.is_user
        wa_msg.OriginActor.GroupJID = self.legacy_id
        if xmpp_msg.reply.to.contact:
            wa_msg.OriginActor.JID = xmpp_msg.reply.to.contact.legacy_id
        if xmpp_msg.reply.to.occupant_id and xmpp_msg.reply.to.occupant_id.endswith(
            "@lid"
        ):
            wa_msg.OriginActor.LID = xmpp_msg.reply.to.occupant_id


class Bookmarks(LegacyBookmarks[MUC]):
    session: "Session"

    async def fill(self) -> None:
        groups = self.session.whatsapp.GetGroups()  # type:ignore[no-untyped-call]
        for group in groups:
            await self.add_whatsapp_group(group)

    async def add_whatsapp_group(self, data: whatsapp.Group) -> None:
        muc = await self.by_legacy_id(data.JID)
        await muc.update_whatsapp_info(data)
        await muc.add_to_bookmarks()

    async def legacy_id_to_jid_local_part(self, legacy_id: str) -> str:
        return "#" + legacy_id[: legacy_id.find("@")]

    async def jid_local_part_to_legacy_id(self, local_part: str) -> str:
        if not local_part.startswith("#"):
            raise XMPPError("bad-request", "Invalid group ID, expected '#' prefix")

        whatsapp_group_id = (
            local_part.removeprefix("#") + "@" + whatsapp.DefaultGroupServer
        )

        if not await self.by_legacy_id(whatsapp_group_id, create=False):
            raise XMPPError("item-not-found", f"No group found for {whatsapp_group_id}")

        return whatsapp_group_id

    async def rename_anonymous_participants(self, contact: whatsapp.Contact) -> None:
        if not contact.Name:
            return
        for muc in self:
            participant = await muc.get_participant(
                occupant_id=contact.Actor.LID, create=False
            )
            if participant is not None:
                participant.nickname = contact.Name


def replace_whatsapp_mentions(text: str, mapping: dict[str, str]) -> str:
    def match(m: re.Match[str]) -> str:
        group = m.group(0)
        return mapping.get(group.removeprefix("@"), group)

    return re.sub(r"@\d+", match, text)
