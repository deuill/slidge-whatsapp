package whatsapp

import (
	// Standard library.
	"bytes"
	"context"
	"errors"
	"fmt"
	"image/jpeg"
	"math/rand"
	"slices"
	"time"

	// Internal packages.
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/media"

	// Third-party libraries.
	_ "github.com/mattn/go-sqlite3"
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
	// The default host part for user JIDs on WhatsApp.
	DefaultUserServer = types.DefaultUserServer

	// The default host part for group JIDs on WhatsApp.
	DefaultGroupServer = types.GroupServer

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

	// The maximum number of messages to request at a time when performing on-demand history
	// synchronization.
	maxHistorySyncMessages = 50
)

// A Session represents a connection (active or not) between a linked device and WhatsApp. Active
// sessions need to be established by logging in, after which incoming events will be forwarded to
// the adapter event handler, and outgoing events will be forwarded to WhatsApp.
type Session struct {
	device       LinkedDevice      // The linked device this session corresponds to.
	client       *whatsmeow.Client // The concrete client connection to WhatsApp for this session.
	gateway      *Gateway          // The Gateway this Session is attached to.
	eventHandler HandleEventFunc   // The handler function to use for propagating events to the adapter.
	presenceChan chan PresenceKind // A channel used for periodically refreshing contact presences.
}

// Login attempts to authenticate the given [Session], either by re-using the [LinkedDevice] attached
// or by initiating a pairing session for a new linked device. Callers are expected to have set an
// event handler in order to receive any incoming events from the underlying WhatsApp session.
func (s *Session) Login() error {
	var err error
	var store *store.Device
	var ctx = context.Background()

	// Try to fetch existing device from given device JID.
	if s.device.ID != "" {
		store, err = s.gateway.container.GetDevice(ctx, s.device.JID())
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
					_, _ = s.GetContacts(false)
					timer, timerStopped = newTimer(presenceRefreshInterval), false
				} else {
					timerStopped = true
				}
			case p, ok := <-s.presenceChan:
				if !ok && !timerStopped {
					if !timer.Stop() {
						<-timer.C
					}
					return
				} else if timerStopped && p == PresenceAvailable {
					_, _ = s.GetContacts(false)
					timer, timerStopped = newTimer(presenceRefreshInterval), false
				}
				presence = p
			}
		}
	}()

	// Simply connect our client if already registered.
	if s.client.Store.ID != nil {
		return s.client.Connect()
	}

	// Attempt out-of-band registration of client via QR code.
	qrChan, _ := s.client.GetQRChannel(context.Background())
	if err = s.client.Connect(); err != nil {
		return err
	}

	go func() {
		for e := range qrChan {
			if !s.client.IsConnected() {
				return
			}
			switch e.Event {
			case whatsmeow.QRChannelEventCode:
				s.propagateEvent(EventQRCode, &EventPayload{QRCode: e.Code})
			case whatsmeow.QRChannelEventError:
				s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Error: e.Error.Error()}})
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

	err := s.client.Logout(context.Background())
	s.client = nil
	close(s.presenceChan)

	return err
}

// Disconnects detaches the current connection to WhatsApp without removing any linked device state.
func (s *Session) Disconnect() error {
	if s.client != nil {
		s.client.Disconnect()
		s.client = nil
		close(s.presenceChan)
	}

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

	var ctx = context.Background()
	code, err := s.client.PairPhone(ctx, phone, true, whatsmeow.PairClientChrome, "Chrome (Linux)")
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

	jid, err := types.ParseJID(message.JID)
	if err != nil {
		return fmt.Errorf("could not parse sender JID for message: %s", err)
	}

	var ctx = context.Background()
	var payload *waE2E.Message
	var extra whatsmeow.SendRequestExtra

	switch message.Kind {
	case MessageAttachment:
		// Handle message with attachment, if any.
		if len(message.Attachments) == 0 {
			return nil
		}

		// Upload attachment into WhatsApp before sending message.
		if payload, err = uploadAttachment(ctx, s.client, &message.Attachments[0]); err != nil {
			return fmt.Errorf("failed uploading attachment: %s", err)
		}
		extra.ID = message.ID
	case MessageEdit:
		// Edit existing message by ID.
		payload = s.client.BuildEdit(s.device.JID().ToNonAD(), message.ID, s.getMessagePayload(ctx, message))
	case MessageRevoke:
		// Don't send message, but revoke existing message by ID.
		var originJID types.JID
		if message.OriginJID == "" {
			// A message retraction by the person who sent it
			originJID = types.EmptyJID
		} else {
			// A message moderation
			originJID, err = types.ParseJID(message.OriginJID)
			if err != nil {
				return fmt.Errorf("could not parse sender JID for message: %s", err)
			}
		}
		payload = s.client.BuildRevoke(jid, originJID, message.ID)
	case MessageReaction:
		// Send message as emoji reaction to a given message.
		payload = &waE2E.Message{
			ReactionMessage: &waE2E.ReactionMessage{
				Key: &waCommon.MessageKey{
					RemoteJID:   &message.JID,
					FromMe:      &message.IsCarbon,
					ID:          &message.ID,
					Participant: &message.OriginJID,
				},
				Text:              &message.Body,
				SenderTimestampMS: ptrTo(time.Now().UnixMilli()),
			},
		}
	default:
		payload = s.getMessagePayload(ctx, message)
		extra.ID = message.ID
	}

	s.gateway.logger.Debugf("Sending message to JID '%s': %+v", jid, payload)
	_, err = s.client.SendMessage(ctx, jid, payload, extra)
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
	// Compose extended message when made as a reply to a different message.
	var payload *waE2E.Message
	if message.ReplyID != "" {
		// Fall back to our own JID if no origin JID has been specified, in which case we assume
		// we're replying to our own messages.
		if message.OriginJID == "" {
			message.OriginJID = s.device.JID().ToNonAD().String()
		}
		payload = &waE2E.Message{
			ExtendedTextMessage: &waE2E.ExtendedTextMessage{
				Text: &message.Body,
				ContextInfo: &waE2E.ContextInfo{
					StanzaID:      &message.ReplyID,
					QuotedMessage: &waE2E.Message{Conversation: ptrTo(message.ReplyBody)},
					Participant:   &message.OriginJID,
				},
			},
		}
	}

	// Add URL preview, if any was given in message.
	if message.Preview.URL != "" {
		if payload == nil {
			payload = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: &message.Body}}
		}

		switch message.Preview.Kind {
		case PreviewPlain:
			payload.ExtendedTextMessage.PreviewType = ptrTo(waE2E.ExtendedTextMessage_NONE)
		case PreviewVideo:
			payload.ExtendedTextMessage.PreviewType = ptrTo(waE2E.ExtendedTextMessage_VIDEO)
		}

		payload.ExtendedTextMessage.MatchedText = &message.Preview.URL
		payload.ExtendedTextMessage.Title = &message.Preview.Title
		payload.ExtendedTextMessage.Description = &message.Preview.Description

		if len(message.Preview.Thumbnail) > 0 && len(message.Preview.Thumbnail) < maxPreviewThumbnailSize {
			data, err := media.Convert(ctx, message.Preview.Thumbnail, &previewThumbnailSpec)
			if err == nil {
				payload.ExtendedTextMessage.JPEGThumbnail = data
				if info, err := jpeg.DecodeConfig(bytes.NewReader(data)); err == nil {
					payload.ExtendedTextMessage.ThumbnailWidth = ptrTo(uint32(info.Width))
					payload.ExtendedTextMessage.ThumbnailHeight = ptrTo(uint32(info.Height))
				}
			}
		}
	}

	// Attach any inline mentions extended metadata.
	if len(message.MentionJIDs) > 0 {
		if payload == nil {
			payload = &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: &message.Body}}
		}
		if payload.ExtendedTextMessage.ContextInfo == nil {
			payload.ExtendedTextMessage.ContextInfo = &waE2E.ContextInfo{}
		}
		payload.ExtendedTextMessage.ContextInfo.MentionedJID = message.MentionJIDs
	}

	// Process any location information in message, if possible.
	if message.Location.Latitude > 0 || message.Location.Longitude > 0 {
		if payload == nil {
			payload = &waE2E.Message{LocationMessage: &waE2E.LocationMessage{}}
		}
		payload.LocationMessage.DegreesLatitude = &message.Location.Latitude
		payload.LocationMessage.DegreesLongitude = &message.Location.Longitude
		payload.LocationMessage.AccuracyInMeters = ptrTo(uint32(message.Location.Accuracy))
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

	jid, err := types.ParseJID(state.JID)
	if err != nil {
		return fmt.Errorf("could not parse sender JID for chat state: %s", err)
	}

	var presence types.ChatPresence
	switch state.Kind {
	case ChatStateComposing:
		presence = types.ChatPresenceComposing
	case ChatStatePaused:
		presence = types.ChatPresencePaused
	}

	return s.client.SendChatPresence(jid, presence, "")
}

// SendReceipt sends a read receipt to WhatsApp for the message IDs specified within.
func (s *Session) SendReceipt(receipt Receipt) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot send receipt for unauthenticated session")
	}

	var jid, senderJID types.JID
	var err error

	if receipt.GroupJID != "" {
		if senderJID, err = types.ParseJID(receipt.JID); err != nil {
			return fmt.Errorf("could not parse sender JID for receipt: %s", err)
		} else if jid, err = types.ParseJID(receipt.GroupJID); err != nil {
			return fmt.Errorf("could not parse group JID for receipt: %s", err)
		}
	} else {
		if jid, err = types.ParseJID(receipt.JID); err != nil {
			return fmt.Errorf("could not parse sender JID for receipt: %s", err)
		}
	}

	ids := slices.Clone(receipt.MessageIDs)
	return s.client.MarkRead(ids, time.Unix(receipt.Timestamp, 0), jid, senderJID)
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
		err = s.client.SendPresence(types.PresenceAvailable)
	case PresenceUnavailable:
		err = s.client.SendPresence(types.PresenceUnavailable)
	}

	if err == nil && statusMessage != "" {
		err = s.client.SetStatusMessage(statusMessage)
	}

	return err
}

// GetContacts subscribes to the WhatsApp roster currently stored in the Session's internal state.
// If `refresh` is `true`, FetchRoster will pull application state from the remote service and
// synchronize any contacts found with the adapter.
func (s *Session) GetContacts(refresh bool) ([]Contact, error) {
	var ctx = context.Background()
	if s.client == nil || s.client.Store.ID == nil {
		return nil, fmt.Errorf("cannot get contacts for unauthenticated session")
	}

	// Synchronize remote application state with local state if requested.
	if refresh {
		err := s.client.FetchAppState(ctx, appstate.WAPatchCriticalUnblockLow, false, false)
		if err != nil {
			s.gateway.logger.Warnf("Could not get app state from server: %s", err)
		}
	}

	// Synchronize local contact state with overarching gateway for all local contacts.
	data, err := s.client.Store.Contacts.GetAllContacts(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed getting local contacts: %s", err)
	}

	var contacts []Contact
	for jid, info := range data {
		if err = s.client.SubscribePresence(jid); err != nil {
			s.gateway.logger.Warnf("Failed to subscribe to presence for %s", jid)
		}

		_, c := newContactEvent(jid, info)
		contacts = append(contacts, c.Contact)
	}

	return contacts, nil
}

// GetGroups returns a list of all group-chats currently joined in WhatsApp, along with additional
// information on present participants.
func (s *Session) GetGroups() ([]Group, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return nil, fmt.Errorf("cannot get groups for unauthenticated session")
	}

	data, err := s.client.GetJoinedGroups()
	if err != nil {
		return nil, fmt.Errorf("failed getting groups: %s", err)
	}

	var groups []Group
	for _, info := range data {
		groups = append(groups, newGroup(s.client, info))
	}

	return groups, nil
}

// CreateGroup attempts to create a new WhatsApp group for the given human-readable name and
// participant JIDs given.
func (s *Session) CreateGroup(name string, participants []string) (Group, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return Group{}, fmt.Errorf("cannot create group for unauthenticated session")
	}

	var jids []types.JID
	for _, p := range participants {
		jid, err := types.ParseJID(p)
		if err != nil {
			return Group{}, fmt.Errorf("could not parse participant JID: %s", err)
		}

		jids = append(jids, jid)
	}

	req := whatsmeow.ReqCreateGroup{Name: name, Participants: jids}
	info, err := s.client.CreateGroup(req)
	if err != nil {
		return Group{}, fmt.Errorf("could not create group: %s", err)
	}

	return newGroup(s.client, info), nil
}

// LeaveGroup attempts to remove our own user from the given WhatsApp group, for the JID given.
func (s *Session) LeaveGroup(resourceID string) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot leave group for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return fmt.Errorf("could not parse JID for leaving group: %s", err)
	}

	return s.client.LeaveGroup(jid)
}

// GetAvatar fetches a profile picture for the Contact or Group JID given. If a non-empty `avatarID`
// is also given, GetAvatar will return an empty [Avatar] instance with no error if the remote state
// for the given ID has not changed.
func (s *Session) GetAvatar(resourceID, avatarID string) (Avatar, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return Avatar{}, fmt.Errorf("cannot get avatar for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return Avatar{}, fmt.Errorf("could not parse JID for avatar: %s", err)
	}

	p, err := s.client.GetProfilePictureInfo(jid, &whatsmeow.GetProfilePictureParams{ExistingID: avatarID})
	if errors.Is(err, whatsmeow.ErrProfilePictureNotSet) || errors.Is(err, whatsmeow.ErrProfilePictureUnauthorized) {
		return Avatar{}, nil
	} else if err != nil {
		return Avatar{}, fmt.Errorf("could not get avatar: %s", err)
	} else if p != nil {
		return Avatar{ID: p.ID, URL: p.URL}, nil
	}

	return Avatar{ID: avatarID}, nil
}

// SetAvatar updates the profile picture for the Contact or Group JID given; it can also update the
// profile picture for our own user by providing an empty JID. The unique picture ID is returned,
// typically used as a cache reference or in providing to future calls for [Session.GetAvatar].
func (s *Session) SetAvatar(resourceID string, avatar []byte) (string, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return "", fmt.Errorf("cannot set avatar for unauthenticated session")
	}

	var ctx = context.Background()
	var jid types.JID
	var err error

	// Setting the profile picture for the user expects an empty `resourceID`.
	if resourceID == "" {
		jid = types.EmptyJID
	} else if jid, err = types.ParseJID(resourceID); err != nil {
		return "", fmt.Errorf("could not parse JID for avatar: %s", err)
	}

	if len(avatar) == 0 {
		return s.client.SetGroupPhoto(jid, nil)
	} else {
		// Ensure avatar is in JPEG format, and convert before setting if needed.
		data, err := media.Convert(ctx, avatar, &media.Spec{MIME: media.TypeJPEG})
		if err != nil {
			return "", fmt.Errorf("failed converting avatar to JPEG: %s", err)
		}

		return s.client.SetGroupPhoto(jid, data)
	}
}

// SetGroupName updates the name of a WhatsApp group for the Group JID given.
func (s *Session) SetGroupName(resourceID, name string) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot set group name for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return fmt.Errorf("could not parse JID for group name change: %s", err)
	}

	return s.client.SetGroupName(jid, name)
}

// SetGroupName updates the topic of a WhatsApp group for the Group JID given.
func (s *Session) SetGroupTopic(resourceID, topic string) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot set group topic for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return fmt.Errorf("could not parse JID for group topic change: %s", err)
	}

	return s.client.SetGroupTopic(jid, "", "", topic)

}

// UpdateGroupParticipants processes changes to the given group's participants, including additions,
// removals, and changes to privileges. Participant JIDs given must be part of the authenticated
// session's roster at least, and must also be active group participants for other types of changes.
func (s *Session) UpdateGroupParticipants(resourceID string, participants []GroupParticipant) ([]GroupParticipant, error) {
	var ctx = context.Background()
	if s.client == nil || s.client.Store.ID == nil {
		return nil, fmt.Errorf("cannot update group participants for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return nil, fmt.Errorf("could not parse JID for group participant update: %s", err)
	}

	var changes = make(map[whatsmeow.ParticipantChange][]types.JID)
	for _, p := range participants {
		participantJID, err := types.ParseJID(p.JID)
		if err != nil {
			return nil, fmt.Errorf("could not parse participant JID for update: %s", err)
		}

		if c, err := s.client.Store.Contacts.GetContact(ctx, participantJID); err != nil {
			return nil, fmt.Errorf("could not fetch contact for participant: %s", err)
		} else if !c.Found {
			return nil, fmt.Errorf("cannot update group participant for contact '%s' not in roster", participantJID)
		}

		c := p.Action.toParticipantChange()
		changes[c] = append(changes[c], participantJID)
	}

	var result []GroupParticipant
	for change, participantJIDs := range changes {
		participants, err := s.client.UpdateGroupParticipants(jid, participantJIDs, change)
		if err != nil {
			return nil, fmt.Errorf("failed setting group affiliation: %s", err)
		}
		for i := range participants {
			p := newGroupParticipant(participants[i])
			if p.JID == "" {
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
	var ctx = context.Background()
	if s.client == nil || s.client.Store.ID == nil {
		return Contact{}, fmt.Errorf("cannot find contact for unauthenticated session")
	}

	jid := types.NewJID(phone, DefaultUserServer)
	if c, err := s.client.Store.Contacts.GetContact(ctx, jid); err == nil && c.Found {
		if _, e := newContactEvent(jid, c); e != nil {
			return e.Contact, nil
		}
	}

	resp, err := s.client.IsOnWhatsApp([]string{phone})
	if err != nil {
		return Contact{}, fmt.Errorf("failed looking up contact '%s': %s", phone, err)
	} else if len(resp) != 1 {
		return Contact{}, fmt.Errorf("failed looking up contact '%s': invalid response", phone)
	} else if !resp[0].IsIn || resp[0].JID.IsEmpty() {
		return Contact{}, nil
	}

	return Contact{JID: resp[0].JID.ToNonAD().String()}, nil
}

// RequestMessageHistory sends and asynchronous request for message history related to the given
// resource (e.g. Contact or Group JID), ending at the oldest message given. Messages returned from
// history should then be handled as a `HistorySync` event of type `ON_DEMAND`, in the session-wide
// event handler. An error will be returned if requesting history fails for any reason.
func (s *Session) RequestMessageHistory(resourceID string, oldestMessage Message) error {
	var ctx = context.Background()
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot request history for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return fmt.Errorf("could not parse JID for history request: %s", err)
	}

	info := &types.MessageInfo{
		ID:            oldestMessage.ID,
		MessageSource: types.MessageSource{Chat: jid, IsFromMe: oldestMessage.IsCarbon},
		Timestamp:     time.Unix(oldestMessage.Timestamp, 0).UTC(),
	}

	req := s.client.BuildHistorySyncRequest(info, maxHistorySyncMessages)
	_, err = s.client.SendMessage(ctx, s.device.JID().ToNonAD(), req, whatsmeow.SendRequestExtra{Peer: true})
	if err != nil {
		return fmt.Errorf("failed to request history for %s: %s", resourceID, err)
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
	var ctx = context.Background()
	s.gateway.logger.Debugf("Handling event '%T': %+v", evt, evt)

	switch evt := evt.(type) {
	case *events.AppStateSyncComplete:
		if len(s.client.Store.PushName) > 0 && evt.Name == appstate.WAPatchCriticalBlock {
			s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{JID: s.device.JID().ToNonAD().String()}})
			if err := s.client.SendPresence(types.PresenceAvailable); err != nil {
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
		s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{JID: s.device.JID().ToNonAD().String()}})
		if err := s.client.SendPresence(types.PresenceAvailable); err != nil {
			s.gateway.logger.Warnf("Failed to send available presence: %s", err)
		}
	case *events.HistorySync:
		switch evt.Data.GetSyncType() {
		case waHistorySync.HistorySync_PUSH_NAME:
			for _, n := range evt.Data.GetPushnames() {
				jid, err := types.ParseJID(n.GetID())
				if err != nil {
					continue
				}
				s.propagateEvent(newContactEvent(jid, types.ContactInfo{FullName: n.GetPushname()}))
				if err = s.client.SubscribePresence(jid); err != nil {
					s.gateway.logger.Warnf("Failed to subscribe to presence for %s", jid)
				}
			}
		case waHistorySync.HistorySync_INITIAL_BOOTSTRAP, waHistorySync.HistorySync_RECENT, waHistorySync.HistorySync_ON_DEMAND:
			for _, c := range evt.Data.GetConversations() {
				for _, msg := range c.GetMessages() {
					s.propagateEvent(newEventFromHistory(s.client, msg.GetMessage()))
				}
			}
		}
	case *events.Message:
		s.propagateEvent(newMessageEvent(s.client, evt))
	case *events.Receipt:
		s.propagateEvent(newReceiptEvent(evt))
	case *events.Presence:
		s.propagateEvent(newPresenceEvent(evt))
	case *events.PushName:
		s.propagateEvent(newContactEvent(evt.JID, types.ContactInfo{FullName: evt.NewPushName}))
	case *events.JoinedGroup:
		s.propagateEvent(EventGroup, &EventPayload{Group: newGroup(s.client, &evt.GroupInfo)})
	case *events.GroupInfo:
		s.propagateEvent(newGroupEvent(evt))
	case *events.ChatPresence:
		s.propagateEvent(newChatStateEvent(evt))
	case *events.CallOffer:
		s.propagateEvent(newCallEvent(CallIncoming, evt.BasicCallMeta))
	case *events.CallTerminate:
		s.propagateEvent(newCallEvent(callStateFromReason(evt.Reason), evt.BasicCallMeta))
	case *events.LoggedOut:
		s.client.Disconnect()
		if err := s.client.Store.Delete(ctx); err != nil {
			s.gateway.logger.Warnf("Unable to delete local device state on logout: %s", err)
		}
		s.client = nil
		s.propagateEvent(EventLoggedOut, nil)
	case *events.PairSuccess:
		if s.client.Store.ID == nil {
			s.gateway.logger.Errorf("Pairing succeeded, but device ID is missing")
			return
		}
		s.device.ID = s.client.Store.ID.String()
		s.propagateEvent(EventPair, &EventPayload{PairDeviceID: s.device.ID})
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

// PtrTo returns a pointer to the given value, and is used for convenience when converting between
// concrete and pointer values without assigning to a variable.
func ptrTo[T any](t T) *T {
	return &t
}
