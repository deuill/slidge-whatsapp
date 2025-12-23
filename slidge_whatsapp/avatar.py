from typing import TYPE_CHECKING

from slidge.core.mixins import AvatarMixin as BaseAvatarMixin

if TYPE_CHECKING:
    from .session import Session


class AvatarMixin(BaseAvatarMixin):
    legacy_id: str
    session: "Session"

    async def update_whatsapp_avatar(self):
        unique_id = ""
        if self.avatar is not None:
            # assert=workaround for poor type annotations in slidge core
            assert not isinstance(self.avatar.unique_id, int)
            unique_id = self.avatar.unique_id or ""
        self.session.whatsapp.RequestAvatar(self.legacy_id, unique_id)
