package whatsapp

import (
	// Standard library.
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"image/jpeg"
	"math/rand"
	"slices"
	"sync"
	"time"

	// Internal packages.
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/media"

	// Third-party libraries.
	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/util/jsontime"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

const (
	// The number of times keep-alive checks can fail before attempting to re-connect the session.
	keepAliveFailureThreshold = 3

	// The minimum and maximum wait interval between connection retries after keep-alive check failure.
	keepAliveMinRetryInterval = 5 * time.Second
	keepAliveMaxRetryInterval = 5 * time.Minute

	// The amount of time to wait before re-requesting contact presences WhatsApp. This is required
	// since otherwise WhatsApp will assume that you're inactive, and will stop sending presence
	// updates for contacts and groups. By default, this interval has a jitter of ± half its value
	// (e.g. for an initial interval of 2 hours, the final value will range from 1 to 3 hours) in
	// order to provide a more natural interaction with remote WhatsApp servers.
	presenceRefreshInterval = 12 * time.Hour

	// Similarly, a sleep interval between making avatar-related calls to WhatsApp.
	requestAvatarInterval = 100 * time.Millisecond

	// The default amount of time a status message propagated to WhatsApp will remain in place before
	// being reset.
	statusMessageDuration = 24 * time.Hour

	// The maximum number of messages to request at a time when performing on-demand history
	// synchronization.
	maxHistorySyncMessages = 50
)

// A Session represents a connection (active or not) between a linked device and WhatsApp. Active
// sessions need to be established by logging in, after which incoming events will be forwarded to
// the adapter event handler, and outgoing events will be forwarded to WhatsApp.
type Session struct {
	device  LinkedDevice      // The linked device this session corresponds to.
	client  *whatsmeow.Client // The concrete client connection to WhatsApp for this session.
	gateway *Gateway          // The Gateway this Session is attached to.

	ctx       context.Context         // A shared context for all top-level [Session] functions.
	ctxCancel context.CancelCauseFunc // The function to call when cancelling the [Session] context.

	eventHandler HandleEventFunc   // The handler function to use for propagating events to the adapter.
	presenceChan chan PresenceKind // A channel used for periodically refreshing contact presences.

	lastAvatarCall time.Time  // We keep try of the last time we called GetAvatar here
	avatarMutex    sync.Mutex // A mutex to safely
}

// Login attempts to authenticate the given [Session], either by re-using the [LinkedDevice] attached
// or by initiating a pairing session for a new linked device. Callers are expected to have set an
// event handler in order to receive any incoming events from the underlying WhatsApp session.
func (s *Session) Login() error {
	var store *store.Device
	var err error

	// Try to fetch existing device from given device JID.
	s.ctx, s.ctxCancel = context.WithCancelCause(context.Background())
	if s.device.ID != "" {
		store, err = s.gateway.container.GetDevice(s.ctx, s.device.JID())
		if err != nil {
			return err
		}
	}

	if store == nil {
		store = s.gateway.container.NewDevice()
	}

	s.client = whatsmeow.NewClient(store, s.gateway.logger)
	s.client.AddEventHandler(s.handleEvent)
	s.client.AutomaticMessageRerequestFromPhone = true

	// Refresh contact presences on a set interval, to avoid issues with WhatsApp dropping them
	// entirely. Contact presences are refreshed only if our current status is set to "available";
	// otherwise, a refresh is queued up for whenever our status changes back to "available".
	s.presenceChan = make(chan PresenceKind, 1)
	go func() {
		var newTimer = func(d time.Duration) *time.Timer {
			return time.NewTimer(d + time.Duration(rand.Int63n(int64(d))-int64(d/2)))
		}
		var timer, timerStopped = newTimer(presenceRefreshInterval), false
		var presence = PresenceAvailable
		for {
			select {
			case <-timer.C:
				if presence == PresenceAvailable {
					_ = s.SubscribeToPresences()
					timer, timerStopped = newTimer(presenceRefreshInterval), false
				} else {
					timerStopped = true
				}
			case p, ok := <-s.presenceChan:
				if !ok {
					timer.Stop()
					return
				} else if timerStopped && p == PresenceAvailable {
					_ = s.SubscribeToPresences()
					timer, timerStopped = newTimer(presenceRefreshInterval), false
				}
				presence = p
			}
		}
	}()

	// Simply connect our client if already registered.
	if s.client.Store.ID != nil {
		return s.client.ConnectContext(s.ctx)
	}

	// Attempt out-of-band registration of client via QR code.
	qrChan, _ := s.client.GetQRChannel(s.ctx)
	if err = s.client.ConnectContext(s.ctx); err != nil {
		return err
	}

	go func() {
		for e := range qrChan {
			switch e.Event {
			case whatsmeow.QRChannelEventCode:
				s.propagateEvent(EventLogin, &EventPayload{Login: Login{QRCode: e.Code}})
			case whatsmeow.QRChannelEventError:
				s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Error: e.Error.Error()}})
			case whatsmeow.QRChannelTimeout.Event:
				s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Error: "You did not flash the QR code in time. Use re-login when you are ready."}})
			case whatsmeow.QRChannelSuccess.Event:
				return
			default:
				s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Error: e.Event}})
			}
		}
	}()

	return nil
}

// Logout disconnects and removes the current linked device locally and initiates a logout remotely.
func (s *Session) Logout() error {
	if s.client == nil || s.client.Store.ID == nil {
		return nil
	}

	err := s.client.Logout(s.ctx)
	s.client = nil
	s.ctxCancel(nil)
	close(s.presenceChan)

	return err
}

// Disconnects detaches the current connection to WhatsApp without removing any linked device state.
func (s *Session) Disconnect() error {
	if s.client == nil {
		return nil
	}

	s.client.Disconnect()
	s.ctxCancel(nil)
	s.client = nil
	close(s.presenceChan)

	return nil
}

// PairPhone returns a one-time code from WhatsApp, used for pairing this [Session] against the
// user's primary device, as identified by the given phone number. This will return an error if the
// [Session] is already paired, or if the phone number given is empty or invalid.
func (s *Session) PairPhone(phone string) (string, error) {
	if s.client == nil {
		return "", fmt.Errorf("cannot pair for uninitialized session")
	} else if s.client.Store.ID != nil {
		return "", fmt.Errorf("refusing to pair for connected session")
	} else if phone == "" {
		return "", fmt.Errorf("cannot pair for empty phone number")
	}

	code, err := s.client.PairPhone(s.ctx, phone, true, whatsmeow.PairClientChrome, "Chrome (Linux)")
	if err != nil {
		return "", fmt.Errorf("failed to pair with phone number: %s", err)
	}

	return code, nil
}

// SendMessage processes the given Message and sends a WhatsApp message for the kind and contact JID
// specified within. In general, different message kinds require different fields to be set; see the
// documentation for the [Message] type for more information.
func (s *Session) SendMessage(message Message) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot send message for unauthenticated session")
	}

	var payload *waE2E.Message
	var extra whatsmeow.SendRequestExtra

	switch message.Kind {
	case MessageAttachment:
		// Handle message with attachment, if any.
		if len(message.Attachments) == 0 {
			return nil
		}

		// Upload attachment into WhatsApp before sending message.
		var err error
		if payload, err = uploadAttachment(s.ctx, s.client, &message.Attachments[0]); err != nil {
			return fmt.Errorf("failed uploading attachment: %s", err)
		}
		extra.ID = message.ID
	case MessageEdit:
		// Edit existing message by ID.
		// TODO: Remember why s.device.JID is used here (rather than the JID given in the message).
		payload = s.client.BuildEdit(s.device.JID().ToNonAD(), message.ID, s.getMessagePayload(s.ctx, message))
	case MessageRevoke:
		// Don't send message, but revoke existing message by ID.
		// TODO: Test whether this can be simplified.
		var jid types.JID
		if message.Chat.Kind() == AddressGroup && !message.IsCarbon {
			// A message moderation.
			jid = message.Origin.toJID()
		} else {
			// A message retraction by the person who sent it.
			jid = types.EmptyJID
		}
		payload = s.client.BuildRevoke(message.Chat.toJID(), jid, message.ID)
	case MessageReaction:
		// Send message as emoji reaction to a given message.
		payload = &waE2E.Message{
			ReactionMessage: &waE2E.ReactionMessage{
				Key: &waCommon.MessageKey{
					RemoteJID:   new(message.Chat.toJID().String()),
					FromMe:      &message.IsCarbon,
					ID:          &message.ID,
					Participant: new(message.Origin.toJID().String()),
				},
				Text:              &message.Body,
				SenderTimestampMS: new(time.Now().UnixMilli()),
			},
		}
	default:
		payload = s.getMessagePayload(s.ctx, message)
		extra.ID = message.ID
	}

	s.gateway.logger.Debugf("Sending message to JID '%s': %+v", message.Chat.toJID(), payload)
	_, err := s.client.SendMessage(s.ctx, message.Chat.toJID(), payload, extra)

	return err
}

const (
	// The maximum size thumbnail image we'll send in outgoing URL preview messages.
	maxPreviewThumbnailSize = 1024 * 500 // 500KiB
)

// GetMessagePayload returns a concrete WhatsApp protocol message for the given Message representation.
// The specific fields set within the protocol message, as well as its type, can depend on specific
// fields set in the Message type, and may be nested recursively (e.g. when replying to a reply).
func (s *Session) getMessagePayload(ctx context.Context, message Message) *waE2E.Message {
	var payload *waE2E.Message

	// Compose extended message when made as a reply to a different message.
	if message.ReplyID != "" && !message.Origin.IsEmpty() {
		payload = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: &message.Body}}
		if payload.ExtendedTextMessage.ContextInfo == nil {
			payload.ExtendedTextMessage.ContextInfo = &waE2E.ContextInfo{}
		}
		payload.ExtendedTextMessage.ContextInfo.StanzaID = &message.ReplyID
		payload.ExtendedTextMessage.ContextInfo.QuotedMessage = &waE2E.Message{Conversation: new(message.ReplyBody)}
		payload.ExtendedTextMessage.ContextInfo.Participant = new(message.Origin.toJID().String())
	}

	// Add URL preview, if any was given in message.
	if message.Preview.URL != "" {
		if payload == nil {
			payload = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: &message.Body}}
		}

		switch message.Preview.Kind {
		case PreviewPlain:
			payload.ExtendedTextMessage.PreviewType = new(waE2E.ExtendedTextMessage_NONE)
		case PreviewVideo:
			payload.ExtendedTextMessage.PreviewType = new(waE2E.ExtendedTextMessage_VIDEO)
		}

		payload.ExtendedTextMessage.MatchedText = &message.Preview.URL
		payload.ExtendedTextMessage.Title = &message.Preview.Title
		payload.ExtendedTextMessage.Description = &message.Preview.Description

		if len(message.Preview.Thumbnail) > 0 && len(message.Preview.Thumbnail) < maxPreviewThumbnailSize {
			data, err := media.Convert(ctx, message.Preview.Thumbnail, &previewThumbnailSpec)
			if err == nil {
				payload.ExtendedTextMessage.JPEGThumbnail = data
				if info, err := jpeg.DecodeConfig(bytes.NewReader(data)); err == nil {
					payload.ExtendedTextMessage.ThumbnailWidth = new(uint32(info.Width))
					payload.ExtendedTextMessage.ThumbnailHeight = new(uint32(info.Height))
				}
			}
		}
	}

	// Attach any inline mentions extended metadata.
	if len(message.Mentions) > 0 {
		if payload == nil {
			payload = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: &message.Body}}
		}
		if payload.ExtendedTextMessage.ContextInfo == nil {
			payload.ExtendedTextMessage.ContextInfo = &waE2E.ContextInfo{}
		}
		for _, m := range message.Mentions {
			payload.ExtendedTextMessage.ContextInfo.MentionedJID = append(
				payload.ExtendedTextMessage.ContextInfo.MentionedJID,
				m.Address.String(),
			)
		}
	}

	// Process any location information in message, if possible.
	if message.Location.Latitude > 0 || message.Location.Longitude > 0 {
		if payload == nil {
			payload = &waE2E.Message{LocationMessage: &waE2E.LocationMessage{}}
		}
		payload.LocationMessage.DegreesLatitude = &message.Location.Latitude
		payload.LocationMessage.DegreesLongitude = &message.Location.Longitude
		payload.LocationMessage.AccuracyInMeters = new(uint32(message.Location.Accuracy))
	}

	if payload == nil {
		payload = &waE2E.Message{Conversation: &message.Body}
	}

	return payload
}

// GenerateMessageID returns a valid, pseudo-random message ID for use in outgoing messages.
func (s *Session) GenerateMessageID() string {
	return s.client.GenerateMessageID()
}

// SendChatState sends the given chat state notification (e.g. composing message) to WhatsApp for the
// contact specified within.
func (s *Session) SendChatState(state ChatState) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot send chat state for unauthenticated session")
	}

	var presence types.ChatPresence
	switch state.Kind {
	case ChatStateComposing:
		presence = types.ChatPresenceComposing
	case ChatStatePaused:
		presence = types.ChatPresencePaused
	}

	return s.client.SendChatPresence(s.ctx, state.Chat.toJID(), presence, "")
}

// SendReceipt sends a read receipt to WhatsApp for the message IDs specified within.
func (s *Session) SendReceipt(receipt Receipt) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot send receipt for unauthenticated session")
	}

	participantJID := types.EmptyJID
	if receipt.Chat.Kind() == AddressGroup {
		participantJID = receipt.Sender.toJID()
	}

	var ids = slices.Clone(receipt.MessageIDs)
	return s.client.MarkRead(s.ctx, ids, time.Unix(receipt.Timestamp, 0), receipt.Chat.toJID(), participantJID)
}

// SendPresence sets the activity state and (optional) status message for the current session and
// user. An error is returned if setting availability fails for any reason.
func (s *Session) SendPresence(presence PresenceKind, statusMessage string) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot send presence for unauthenticated session")
	}

	var err error
	s.presenceChan <- presence

	switch presence {
	case PresenceAvailable:
		err = s.client.SendPresence(s.ctx, types.PresenceAvailable)
	case PresenceUnavailable:
		err = s.client.SendPresence(s.ctx, types.PresenceUnavailable)
	}

	if err == nil && statusMessage != "" {
		err = s.client.SetStatusMessage(s.ctx, types.SetStatusInput{
			Text:     new(statusMessage),
			Duration: jsontime.S(statusMessageDuration),
		})
	}

	return err
}

// GetContacts subscribes to the WhatsApp roster currently stored in the Session's internal state.
// If `refresh` is `true`, FetchRoster will pull application state from the remote service and
// synchronize any contacts found with the adapter.
func (s *Session) GetContacts(refresh bool) ([]Contact, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return nil, fmt.Errorf("cannot get contacts for unauthenticated session")
	}

	// Synchronize remote application state with local state if requested.
	if refresh {
		err := s.client.FetchAppState(s.ctx, appstate.WAPatchCriticalUnblockLow, false, false)
		if err != nil {
			s.gateway.logger.Warnf("Could not get app state from server: %s", err)
		}
	}

	// Synchronize local contact state with overarching gateway for all local contacts.
	data, err := s.client.Store.Contacts.GetAllContacts(s.ctx)
	if err != nil {
		return nil, fmt.Errorf("failed getting local contacts: %s", err)
	}

	var contacts []Contact
	for jid, info := range data {
		c, err := newContact(s.ctx, s.client, jid, info)
		if err != nil {
			continue
		}
		contacts = append(contacts, c)
	}

	return contacts, nil
}

// SubscribeToPresences attempts to subscribe to presence events for all locally known contacts,
// enabling any future events to be handled as per [Session]-wide event handlers.
func (s *Session) SubscribeToPresences() error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot subscribe to presences for unauthenticated session")
	}

	data, err := s.client.Store.Contacts.GetAllContacts(s.ctx)
	if err != nil {
		return fmt.Errorf("failed getting local contacts: %s", err)
	}

	if err := s.SendPresence(PresenceAvailable, ""); err != nil {
		return fmt.Errorf("failed setting presence: %s", err)
	}

	for jid := range data {
		if err = s.client.SubscribePresence(s.ctx, jid); err != nil {
			s.gateway.logger.Debugf("Failed to subscribe to presence for %s", jid)
		}
	}

	return nil
}

// GetGroups returns a list of all group-chats currently joined in WhatsApp, along with additional
// information on present participants.
func (s *Session) GetGroups() ([]Group, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return nil, fmt.Errorf("cannot get groups for unauthenticated session")
	}

	data, err := s.client.GetJoinedGroups(s.ctx)
	if err != nil {
		return nil, fmt.Errorf("failed getting groups: %s", err)
	}

	var groups []Group
	for _, info := range data {
		groups = append(groups, newGroup(s.ctx, s.client, info))
	}

	return groups, nil
}

// CreateGroup attempts to create a new WhatsApp group for the given human-readable name and
// participant JIDs given.
func (s *Session) CreateGroup(group Group) (Group, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return Group{}, fmt.Errorf("cannot create group for unauthenticated session")
	}

	var jids []types.JID
	for _, p := range group.Participants {
		jids = append(jids, p.Address.toJID())
	}

	req := whatsmeow.ReqCreateGroup{Name: group.Name, Participants: jids}
	info, err := s.client.CreateGroup(s.ctx, req)
	if err != nil {
		return Group{}, fmt.Errorf("could not create group: %s", err)
	}

	return newGroup(s.ctx, s.client, info), nil
}

// LeaveGroup attempts to remove our own user from the given WhatsApp group, for the address given.
func (s *Session) LeaveGroup(addr Address) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot leave group for unauthenticated session")
	} else if addr.Kind() != AddressGroup {
		return fmt.Errorf("cannot leave group for non-group address %s", addr)
	}

	return s.client.LeaveGroup(s.ctx, addr.toJID())
}

// TODO
func (s *Session) RequestAvatar(addr Address, avatarID string) {
	if s.client == nil || s.client.Store.ID == nil {
		s.gateway.logger.Errorf("Cannot request avatar for unauthenticated session")
		return
	}

	if addr.IsEmpty() {
		return
	}

	s.avatarMutex.Lock()
	defer s.avatarMutex.Unlock()

	timeSinceLastCall := time.Since(s.lastAvatarCall)
	if timeSinceLastCall < requestAvatarInterval {
		time.Sleep(requestAvatarInterval + time.Duration(rand.Int63n(int64(requestAvatarInterval))-int64(requestAvatarInterval/2)))
	}

	p, err := s.client.GetProfilePictureInfo(s.ctx, addr.toJID(), &whatsmeow.GetProfilePictureParams{ExistingID: avatarID})
	if !errors.Is(err, whatsmeow.ErrProfilePictureNotSet) && !errors.Is(err, whatsmeow.ErrProfilePictureUnauthorized) {
		s.gateway.logger.Errorf("Error fetching avatar for %s: %s", addr, err)
		return
	} else if p == nil {
		return
	}

	s.lastAvatarCall = time.Now()

	if p.ID == avatarID {
		s.gateway.logger.Debugf("Fetching avatar for %s skipped, cached avatar up-to-date", addr)
		return
	}

	s.propagateEvent(EventAvatar, &EventPayload{Avatar: Avatar{Address: addr, ID: p.ID, URL: p.URL}})
}

// SetAvatar updates the profile picture for the Contact or Group [Address] given; it can also
// update the profile picture for our own user by providing an empty address. The unique picture ID
// is returned, typically used as a cache reference or in providing to future calls for
// [Session.RequestAvatar].
func (s *Session) SetAvatar(addr Address, avatar []byte) (string, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return "", fmt.Errorf("cannot set avatar for unauthenticated session")
	}

	jid := addr.toJID()
	if len(avatar) == 0 {
		return s.client.SetGroupPhoto(s.ctx, jid, nil)
	} else {
		// Ensure avatar is in JPEG format, and convert before setting if needed.
		data, err := media.Convert(s.ctx, avatar, &media.Spec{MIME: media.TypeJPEG})
		if err != nil {
			return "", fmt.Errorf("failed converting avatar to JPEG: %s", err)
		}

		return s.client.SetGroupPhoto(s.ctx, jid, data)
	}
}

// SetGroupName updates the name of a WhatsApp group for the address given.
func (s *Session) SetGroupName(addr Address, name string) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot set group name for unauthenticated session")
	} else if addr.Kind() != AddressGroup {
		return fmt.Errorf("cannot set group name for non-group address %s", addr)
	}

	return s.client.SetGroupName(s.ctx, addr.toJID(), name)
}

// SetGroupName updates the topic of a WhatsApp group for the address given.
func (s *Session) SetGroupTopic(addr Address, topic string) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot set group topic for unauthenticated session")
	} else if addr.Kind() != AddressGroup {
		return fmt.Errorf("cannot set group topic for non-group address %s", addr)
	}

	return s.client.SetGroupTopic(s.ctx, addr.toJID(), "", "", topic)

}

// UpdateGroupParticipants processes changes to the given group's participants, including additions,
// removals, and changes to privileges. Participant JIDs given must be part of the authenticated
// session's roster at least, and must also be active group participants for other types of changes.
func (s *Session) UpdateGroupParticipants(addr Address, participants []GroupParticipant) ([]GroupParticipant, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return nil, fmt.Errorf("cannot update group participants for unauthenticated session")
	}

	if addr.Kind() != AddressGroup {
		return nil, fmt.Errorf("cannot update group participants for non-group address %s", addr)
	}

	var changes = make(map[whatsmeow.ParticipantChange][]types.JID)
	for _, p := range participants {
		participantJID := p.Address.toJID()
		if c, err := s.client.Store.Contacts.GetContact(s.ctx, participantJID); err != nil {
			return nil, fmt.Errorf("could not fetch contact for participant: %s", err)
		} else if !c.Found {
			return nil, fmt.Errorf("cannot update group participant for contact '%s' not in roster", participantJID)
		}

		c := p.Action.toParticipantChange()
		changes[c] = append(changes[c], participantJID)
	}

	var result []GroupParticipant
	var groupJID = addr.toJID()

	for change, participantJIDs := range changes {
		participants, err := s.client.UpdateGroupParticipants(s.ctx, groupJID, participantJIDs, change)
		if err != nil {
			return nil, fmt.Errorf("failed setting group affiliation: %s", err)
		}
		for i := range participants {
			p := newGroupParticipant(s.ctx, s.client, participants[i])
			if p.Address.IsEmpty() {
				continue
			}
			result = append(result, p)
		}
	}

	return result, nil
}

// FindContact attempts to check for a registered contact on WhatsApp corresponding to the given
// phone number, returning a concrete instance if found; typically, only the contact JID is set. No
// error is returned if no contact was found, but any unexpected errors will otherwise be returned
// directly.
func (s *Session) FindContact(phone string) (Contact, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return Contact{}, fmt.Errorf("cannot find contact for unauthenticated session")
	}

	jid := types.NewJID(phone, types.DefaultUserServer)
	if info, err := s.client.Store.Contacts.GetContact(s.ctx, jid); err == nil && info.Found {
		return newContact(s.ctx, s.client, jid, info)
	}

	resp, err := s.client.IsOnWhatsApp(s.ctx, []string{phone})
	if err != nil {
		return Contact{}, fmt.Errorf("failed looking up contact '%s': %s", phone, err)
	} else if len(resp) != 1 {
		return Contact{}, fmt.Errorf("failed looking up contact '%s': invalid response", phone)
	} else if !resp[0].IsIn || resp[0].JID.IsEmpty() {
		return Contact{}, nil
	}

	return Contact{
		Address: newAddressFromJID(resp[0].JID),
		Name:    phone,
	}, nil
}

// RequestMessageHistory sends and asynchronous request for message history related to the given
// group address, ending at the oldest message given. Messages returned from history should then be
// handled as a `HistorySync` event of type `ON_DEMAND`, in the session-wide event handler. An error
// will be returned if requesting history fails for any reason.
func (s *Session) RequestMessageHistory(addr Address, oldest Message) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot request history for unauthenticated session")
	}

	if addr.Kind() != AddressGroup {
		return fmt.Errorf("cannot fetch history for non-group address %s", addr)
	}

	req := s.client.BuildHistorySyncRequest(
		&types.MessageInfo{
			ID:            oldest.ID,
			MessageSource: types.MessageSource{Chat: addr.toJID(), IsFromMe: oldest.IsCarbon},
			Timestamp:     time.Unix(oldest.Timestamp, 0).UTC(),
		},
		maxHistorySyncMessages,
	)

	_, err := s.client.SendMessage(s.ctx, s.device.JID().ToNonAD(), req, whatsmeow.SendRequestExtra{Peer: true})
	if err != nil {
		return fmt.Errorf("failed to request history for %s: %s", addr, err)
	}

	return nil
}

// SetEventHandler assigns the given handler function for propagating internal events into the Python
// gateway. Note that the event handler function is not entirely safe to use directly, and all calls
// should instead be sent to the [Gateway] via its internal call channel.
func (s *Session) SetEventHandler(h HandleEventFunc) {
	s.eventHandler = h
}

// PropagateEvent handles the given event kind and payload with the adapter event handler defined in
// [Session.SetEventHandler].
func (s *Session) propagateEvent(kind EventKind, payload *EventPayload) {
	if s.eventHandler == nil {
		s.gateway.logger.Errorf("Event handler not set when propagating event %d with payload %v", kind, payload)
		return
	} else if kind == EventUnknown {
		return
	}

	// Send empty payload instead of a nil pointer, as Python has trouble handling the latter.
	if payload == nil {
		payload = &EventPayload{}
	}

	s.gateway.callChan <- func() { s.eventHandler(kind, payload) }
}

// HandleEvent processes the given incoming WhatsApp event, checking its concrete type and
// propagating it to the adapter event handler. Unknown or unhandled events are ignored, and any
// errors that occur during processing are logged.
func (s *Session) handleEvent(evt any) {
	s.gateway.logger.Debugf("Handling event '%T': %+v", evt, jsonStringer{evt})

	switch evt := evt.(type) {
	case *events.AppStateSyncComplete:
		if len(s.client.Store.PushName) > 0 && evt.Name == appstate.WAPatchCriticalBlock {
			s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Address: newAddressFromJID(s.device.JID())}})
			if err := s.client.SendPresence(s.ctx, types.PresenceAvailable); err != nil {
				s.gateway.logger.Warnf("Failed to send available presence: %s", err)
			}
		}
	case *events.ConnectFailure:
		switch evt.Reason {
		case events.ConnectFailureLoggedOut:
			// These events are handled separately.
		default:
			s.gateway.logger.Errorf("failed to connect: %s", evt.Message)
			s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Error: evt.Message}})
		}
	case *events.Connected, *events.PushNameSetting:
		if len(s.client.Store.PushName) == 0 {
			return
		}
		s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Address: newAddressFromJID(s.device.JID())}})
		if err := s.client.SendPresence(s.ctx, types.PresenceAvailable); err != nil {
			s.gateway.logger.Warnf("Failed to send available presence: %s", err)
		}
	case *events.HistorySync:
		switch evt.Data.GetSyncType() {
		case waHistorySync.HistorySync_PUSH_NAME:
			for _, n := range evt.Data.GetPushnames() {
				jid, _ := types.ParseJID(n.GetID())
				if jid != types.EmptyJID {
					s.propagateEvent(newContactEvent(s.ctx, s.client, jid))
				}
			}
		case waHistorySync.HistorySync_INITIAL_BOOTSTRAP, waHistorySync.HistorySync_RECENT, waHistorySync.HistorySync_ON_DEMAND:
			for _, c := range evt.Data.GetConversations() {
				for _, msg := range c.GetMessages() {
					s.propagateEvent(newEventFromHistory(s.ctx, s.client, msg.GetMessage()))
				}
			}
		}
	case *events.Message:
		s.propagateEvent(newMessageEvent(s.ctx, s.client, evt))
	case *events.Receipt:
		s.propagateEvent(newReceiptEvent(evt))
	case *events.Presence:
		s.propagateEvent(newPresenceEvent(evt))
	case *events.Contact:
		s.propagateEvent(newContactEvent(s.ctx, s.client, evt.JID))
	case *events.PushName:
		s.propagateEvent(newContactEvent(s.ctx, s.client, evt.JID))
	case *events.JoinedGroup:
		s.propagateEvent(EventGroup, &EventPayload{Group: newGroup(s.ctx, s.client, &evt.GroupInfo)})
	case *events.GroupInfo:
		s.propagateEvent(newGroupEvent(evt))
	case *events.ChatPresence:
		s.propagateEvent(newChatStateEvent(evt))
	case *events.CallOffer:
		s.propagateEvent(newCallEvent(CallIncoming, evt.BasicCallMeta))
	case *events.CallTerminate:
		s.propagateEvent(newCallEvent(callStateFromReason(evt.Reason), evt.BasicCallMeta))
	case *events.LoggedOut:
		_ = s.Disconnect()
		s.propagateEvent(EventLogout, &EventPayload{Logout: Logout{Reason: evt.Reason.String()}})
	case *events.PairSuccess:
		if s.client.Store.ID == nil {
			s.gateway.logger.Errorf("Pairing succeeded, but device ID is missing")
			return
		}
		s.device.ID = s.client.Store.ID.String()
		s.propagateEvent(EventLogin, &EventPayload{Login: Login{PairID: s.device.ID}})
		if err := s.gateway.CleanupSession(LinkedDevice{ID: s.device.ID}); err != nil {
			s.gateway.logger.Warnf("Failed to clean up devices after pair: %s", err)
		}
	case *events.KeepAliveTimeout:
		if evt.ErrorCount > keepAliveFailureThreshold {
			s.gateway.logger.Debugf("Forcing reconnection after keep-alive timeouts...")
			go func() {
				var interval = keepAliveMinRetryInterval
				s.client.Disconnect()
				for {
					err := s.client.Connect()
					if err == nil || err == whatsmeow.ErrAlreadyConnected {
						break
					}

					s.gateway.logger.Errorf("Error reconnecting after keep-alive timeouts, retrying in %s: %s", interval, err)
					time.Sleep(interval)

					if interval > keepAliveMaxRetryInterval {
						interval = keepAliveMaxRetryInterval
					} else if interval < keepAliveMaxRetryInterval {
						interval *= 2
					}
				}
			}()
		}
	}
}

// a JSONStringer is a value that returns a JSON-encoded, multi-line version of itself in calls to
// [String].
type jsonStringer struct{ v any }

// String returns a multi-line, indented, JSON representation of the [jsonStringer] value.
func (j jsonStringer) String() string {
	buf, _ := json.MarshalIndent(j.v, "", "    ")
	return string(buf)
}

// Coalesce returns the first non-empty value in the arguments given, or the empty value if none
// were found.
func coalesce[T comparable](v ...T) T {
	var empty T
	for i := range v {
		if v[i] != empty {
			return v[i]
		}
	}
	return empty
}
