package whatsapp

import (
	// Standard library.
	"context"
	"errors"
	"fmt"
	"math/rand"
	"os"
	"runtime"
	"time"

	// Third-party libraries.
	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/binary/proto"
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

// HandleEventFunc represents a handler for incoming events sent to the Python Session, accepting an
// event type and payload. Note that this is distinct to the [Session.handleEvent] function, which
// may emit events into the Python Session event handler but which otherwise does not process across
// Python/Go boundaries.
type HandleEventFunc func(EventKind, *EventPayload)

// A Session represents a connection (active or not) between a linked device and WhatsApp. Active
// sessions need to be established by logging in, after which incoming events will be forwarded to
// the adapter event handler, and outgoing events will be forwarded to WhatsApp.
type Session struct {
	device       LinkedDevice      // The linked device this session corresponds to.
	eventHandler HandleEventFunc   // The event handler for the overarching Session.
	client       *whatsmeow.Client // The concrete client connection to WhatsApp for this session.
	gateway      *Gateway          // The Gateway this Session is attached to.
	presenceChan chan PresenceKind // A channel used for periodically refreshing contact presences.
}

// Login attempts to authenticate the given [Session], either by re-using the [LinkedDevice] attached
// or by initiating a pairing session for a new linked device. Callers are expected to have set an
// event handler in order to receive any incoming events from the underlying WhatsApp session.
func (s *Session) Login() error {
	var err error
	var store *store.Device

	// Try to fetch existing device from given device JID.
	if s.device.ID != "" {
		store, err = s.gateway.container.GetDevice(s.device.JID())
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
					s.GetContacts(false)
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
					s.GetContacts(false)
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
			case "code":
				s.propagateEvent(EventQRCode, &EventPayload{QRCode: e.Code})
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

	err := s.client.Logout()
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
		return "", fmt.Errorf("Cannot pair for uninitialized session")
	} else if s.client.Store.ID != nil {
		return "", fmt.Errorf("Refusing to pair for connected session")
	} else if phone == "" {
		return "", fmt.Errorf("Cannot pair for empty phone number")
	}

	code, err := s.client.PairPhone(phone, true, whatsmeow.PairClientChrome, "Chrome (Linux)")
	if err != nil {
		return "", fmt.Errorf("Failed to pair with phone number: %s", err)
	}

	return code, nil
}

// SendMessage processes the given Message and sends a WhatsApp message for the kind and contact JID
// specified within. In general, different message kinds require different fields to be set; see the
// documentation for the [Message] type for more information.
func (s *Session) SendMessage(message Message) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("Cannot send message for unauthenticated session")
	}

	jid, err := types.ParseJID(message.JID)
	if err != nil {
		return fmt.Errorf("Could not parse sender JID for message: %s", err)
	}

	var payload *proto.Message
	var extra whatsmeow.SendRequestExtra

	switch message.Kind {
	case MessageAttachment:
		// Handle message with attachment, if any.
		if len(message.Attachments) == 0 {
			return nil
		}

		defer os.Remove(message.Attachments[0].Path)

		// Upload attachment into WhatsApp before sending message.
		if payload, err = uploadAttachment(s.client, &message.Attachments[0]); err != nil {
			return fmt.Errorf("Failed uploading attachment: %s", err)
		}
		extra.ID = message.ID
	case MessageEdit:
		// Edit existing message by ID.
		payload = s.client.BuildEdit(s.device.JID().ToNonAD(), message.ID, s.getMessagePayload(message))
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
				return fmt.Errorf("Could not parse sender JID for message: %s", err)
			}
		}
		payload = s.client.BuildRevoke(jid, originJID, message.ID)
	case MessageReaction:
		// Send message as emoji reaction to a given message.
		payload = &proto.Message{
			ReactionMessage: &proto.ReactionMessage{
				Key: &proto.MessageKey{
					RemoteJid:   &message.JID,
					FromMe:      &message.IsCarbon,
					Id:          &message.ID,
					Participant: &message.OriginJID,
				},
				Text:              &message.Body,
				SenderTimestampMs: ptrTo(time.Now().UnixMilli()),
			},
		}
	default:
		payload = s.getMessagePayload(message)
		extra.ID = message.ID
	}

	s.gateway.logger.Debugf("Sending message to JID '%s': %+v", jid, payload)
	_, err = s.client.SendMessage(context.Background(), jid, payload, extra)
	return err
}

const (
	// The maximum size thumbnail image we'll send in outgoing URL preview messages.
	maxPreviewThumbnailSize = 1024 * 500 // 500KiB
)

// GetMessagePayload returns a concrete WhatsApp protocol message for the given Message representation.
// The specific fields set within the protocol message, as well as its type, can depend on specific
// fields set in the Message type, and may be nested recursively (e.g. when replying to a reply).
func (s *Session) getMessagePayload(message Message) *proto.Message {
	var payload *proto.Message

	// Compose extended message when made as a reply to a different message.
	if message.ReplyID != "" {
		// Fall back to our own JID if no origin JID has been specified, in which case we assume
		// we're replying to our own messages.
		if message.OriginJID == "" {
			message.OriginJID = s.device.JID().ToNonAD().String()
		}
		payload = &proto.Message{
			ExtendedTextMessage: &proto.ExtendedTextMessage{
				Text: &message.Body,
				ContextInfo: &proto.ContextInfo{
					StanzaId:      &message.ReplyID,
					QuotedMessage: &proto.Message{Conversation: ptrTo(message.ReplyBody)},
					Participant:   &message.OriginJID,
				},
			},
		}
	}

	// Add URL preview, if any was given in message.
	if message.Preview.URL != "" {
		if payload == nil {
			payload = &proto.Message{ExtendedTextMessage: &proto.ExtendedTextMessage{Text: &message.Body}}
		}

		payload.ExtendedTextMessage.MatchedText = &message.Preview.URL
		payload.ExtendedTextMessage.Title = &message.Preview.Title

		if message.Preview.ImagePath != "" {
			if buf, err := os.ReadFile(message.Preview.ImagePath); err == nil && len(buf) < maxPreviewThumbnailSize {
				payload.ExtendedTextMessage.JpegThumbnail = buf
			}
			os.Remove(message.Preview.ImagePath)
		}
	}

	// Attach any inline mentions extended metadata.
	if len(message.MentionJIDs) > 0 {
		if payload == nil {
			payload = &proto.Message{ExtendedTextMessage: &proto.ExtendedTextMessage{Text: &message.Body}}
		}
		if payload.ExtendedTextMessage.ContextInfo == nil {
			payload.ExtendedTextMessage.ContextInfo = &proto.ContextInfo{}
		}
		payload.ExtendedTextMessage.ContextInfo.MentionedJid = message.MentionJIDs
	}

	if payload == nil {
		payload = &proto.Message{Conversation: &message.Body}
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
		return fmt.Errorf("Cannot send chat state for unauthenticated session")
	}

	jid, err := types.ParseJID(state.JID)
	if err != nil {
		return fmt.Errorf("Could not parse sender JID for chat state: %s", err)
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
		return fmt.Errorf("Cannot send receipt for unauthenticated session")
	}

	var jid, senderJID types.JID
	var err error

	if receipt.GroupJID != "" {
		if senderJID, err = types.ParseJID(receipt.JID); err != nil {
			return fmt.Errorf("Could not parse sender JID for receipt: %s", err)
		} else if jid, err = types.ParseJID(receipt.GroupJID); err != nil {
			return fmt.Errorf("Could not parse group JID for receipt: %s", err)
		}
	} else {
		if jid, err = types.ParseJID(receipt.JID); err != nil {
			return fmt.Errorf("Could not parse sender JID for receipt: %s", err)
		}
	}

	ids := append([]types.MessageID{}, receipt.MessageIDs...)
	return s.client.MarkRead(ids, time.Unix(receipt.Timestamp, 0), jid, senderJID)
}

// SendPresence sets the activity state and (optional) status message for the current session and
// user. An error is returned if setting availability fails for any reason.
func (s *Session) SendPresence(presence PresenceKind, statusMessage string) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("Cannot send presence for unauthenticated session")
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
	if s.client == nil || s.client.Store.ID == nil {
		return nil, fmt.Errorf("Cannot get contacts for unauthenticated session")
	}

	// Synchronize remote application state with local state if requested.
	if refresh {
		err := s.client.FetchAppState(appstate.WAPatchCriticalUnblockLow, false, false)
		if err != nil {
			s.gateway.logger.Warnf("Could not get app state from server: %s", err)
		}
	}

	// Synchronize local contact state with overarching gateway for all local contacts.
	data, err := s.client.Store.Contacts.GetAllContacts()
	if err != nil {
		return nil, fmt.Errorf("Failed getting local contacts: %s", err)
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
		return nil, fmt.Errorf("Cannot get groups for unauthenticated session")
	}

	data, err := s.client.GetJoinedGroups()
	if err != nil {
		return nil, fmt.Errorf("Failed getting groups: %s", err)
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
		return Group{}, fmt.Errorf("Cannot create group for unauthenticated session")
	}

	var jids []types.JID
	for _, p := range participants {
		jid, err := types.ParseJID(p)
		if err != nil {
			return Group{}, fmt.Errorf("Could not parse participant JID: %s", err)
		}

		jids = append(jids, jid)
	}

	req := whatsmeow.ReqCreateGroup{Name: name, Participants: jids}
	info, err := s.client.CreateGroup(req)
	if err != nil {
		return Group{}, fmt.Errorf("Could not create group: %s", err)
	}

	return newGroup(s.client, info), nil
}

// GetAvatar fetches a profile picture for the Contact or Group JID given. If a non-empty `avatarID`
// is also given, GetAvatar will return an empty [Avatar] instance with no error if the remote state
// for the given ID has not changed.
func (s *Session) GetAvatar(resourceID, avatarID string) (Avatar, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return Avatar{}, fmt.Errorf("Cannot get avatar for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return Avatar{}, fmt.Errorf("Could not parse JID for avatar: %s", err)
	}

	p, err := s.client.GetProfilePictureInfo(jid, &whatsmeow.GetProfilePictureParams{ExistingID: avatarID})
	if err != nil && !errors.Is(err, whatsmeow.ErrProfilePictureNotSet) {
		return Avatar{}, fmt.Errorf("Could not get avatar: %s", err)
	} else if p != nil {
		return Avatar{ID: p.ID, URL: p.URL}, nil
	}

	return Avatar{}, nil
}

// SetAvatar updates the profile picture for the Contact or Group JID given; it can also update the
// profile picture for our own user by providing an empty JID. The unique picture ID is returned,
// typically used as a cache reference or in providing to future calls for [Session.GetAvatar].
func (s *Session) SetAvatar(resourceID, avatarPath string) (string, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return "", fmt.Errorf("Cannot set avatar for unauthenticated session")
	}

	var jid types.JID
	var err error

	if avatarPath != "" {
		defer os.Remove(avatarPath)
	}

	// Setting the profile picture for the user expects an empty `resourceID`.
	if resourceID == "" {
		jid = types.EmptyJID
	} else if jid, err = types.ParseJID(resourceID); err != nil {
		return "", fmt.Errorf("Could not parse JID for avatar: %s", err)
	}

	if avatarPath == "" {
		return s.client.SetGroupPhoto(jid, nil)
	} else {
		// Ensure avatar is in JPEG format, and convert before setting if needed.
		if err = convertImage(&Attachment{Path: avatarPath}); err != nil {
			return "", fmt.Errorf("Failed converting avatar to JPEG: %s", err)
		}

		avatar, err := os.ReadFile(avatarPath)
		if err != nil {
			return "", fmt.Errorf("Failed reading avatar: %s", err)
		}

		return s.client.SetGroupPhoto(jid, avatar)
	}
}

// SetGroupName updates the name of a WhatsApp group
func (s *Session) SetGroupName(resourceID, name string) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("Cannot set group name for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return fmt.Errorf("Could not parse JID for group name change: %s", err)
	}

	return s.client.SetGroupName(jid, name)
}

// SetGroupTopic updates the topic of a WhatsApp group
func (s *Session) SetGroupTopic(resourceID, topic string) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("Cannot set group topic for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return fmt.Errorf("Could not parse JID for group topic change: %s", err)
	}

	return s.client.SetGroupTopic(jid, "", "", topic)
}

func (s *Session) SetAffiliation(groupID, participantID string, change whatsmeow.ParticipantChange) ([]types.GroupParticipant, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return make([]types.GroupParticipant, 0), fmt.Errorf("Cannot set affiliation for unauthenticated session")
	}

	groupJID, err := types.ParseJID(groupID)
	if err != nil {
		return make([]types.GroupParticipant, 0), fmt.Errorf("Could not parse JID for affiliation change: %s", err)
	}

	participantJID, err := types.ParseJID(participantID)
	if err != nil {
		return make([]types.GroupParticipant, 0), fmt.Errorf("Could not parse JID for affiliation change: %s", err)
	}
	return s.client.UpdateGroupParticipants(groupJID, []types.JID{participantJID}, change)
}

// FindContact attempts to check for a registered contact on WhatsApp corresponding to the given
// phone number, returning a concrete instance if found; typically, only the contact JID is set. No
// error is returned if no contact was found, but any unexpected errors will otherwise be returned
// directly.
func (s *Session) FindContact(phone string) (Contact, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return Contact{}, fmt.Errorf("Cannot find contact for unauthenticated session")
	}

	resp, err := s.client.IsOnWhatsApp([]string{phone})
	if err != nil {
		return Contact{}, fmt.Errorf("Failed looking up contact '%s': %s", phone, err)
	} else if len(resp) != 1 {
		return Contact{}, fmt.Errorf("Failed looking up contact '%s': invalid response", phone)
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
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("Cannot request history for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return fmt.Errorf("Could not parse JID for history request: %s", err)
	}

	info := &types.MessageInfo{
		ID:            oldestMessage.ID,
		MessageSource: types.MessageSource{Chat: jid, IsFromMe: oldestMessage.IsCarbon},
		Timestamp:     time.Unix(oldestMessage.Timestamp, 0).UTC(),
	}

	req := s.client.BuildHistorySyncRequest(info, maxHistorySyncMessages)
	_, err = s.client.SendMessage(context.Background(), s.device.JID().ToNonAD(), req, whatsmeow.SendRequestExtra{Peer: true})
	if err != nil {
		return fmt.Errorf("Failed to request history for %s: %s", resourceID, err)
	}

	return nil
}

// SetEventHandler assigns the given handler function for propagating internal events into the Python
// gateway. Note that the event handler function is not entirely safe to use directly, and all calls
// should instead be made via the [propagateEvent] function.
func (s *Session) SetEventHandler(h HandleEventFunc) {
	s.eventHandler = h
}

// PropagateEvent handles the given event kind and payload with the adapter event handler defined in
// SetEventHandler. If no event handler is set, this function will return early with no error.
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

	// Don't allow other Goroutines from using this thread, as this might lead to concurrent use of
	// the GIL, which can lead to crashes.
	s.gateway.logger.Debugf("Locking OS Thread (propagateEvent)")
	runtime.LockOSThread()
	defer UnlockOSThread2(s)

	s.eventHandler(kind, payload)
}

func UnlockOSThread2(s *Session) {
	s.gateway.logger.Debugf("Unlocking OS thread (propagateEvent)")
	runtime.UnlockOSThread()
}

// HandleEvent processes the given incoming WhatsApp event, checking its concrete type and
// propagating it to the adapter event handler. Unknown or unhandled events are ignored, and any
// errors that occur during processing are logged.
func (s *Session) handleEvent(evt interface{}) {
	s.gateway.logger.Debugf("Handling event '%T': %+v", evt, evt)

	switch evt := evt.(type) {
	case *events.AppStateSyncComplete:
		if len(s.client.Store.PushName) > 0 && evt.Name == appstate.WAPatchCriticalBlock {
			s.propagateEvent(EventConnected, &EventPayload{ConnectedJID: s.device.JID().ToNonAD().String()})
			if err := s.client.SendPresence(types.PresenceAvailable); err != nil {
				s.gateway.logger.Warnf("Failed to send available presence: %s", err)
			}
		}
	case *events.Connected, *events.PushNameSetting:
		if len(s.client.Store.PushName) == 0 {
			return
		}
		s.propagateEvent(EventConnected, &EventPayload{ConnectedJID: s.device.JID().ToNonAD().String()})
		if err := s.client.SendPresence(types.PresenceAvailable); err != nil {
			s.gateway.logger.Warnf("Failed to send available presence: %s", err)
		}
	case *events.HistorySync:
		switch evt.Data.GetSyncType() {
		case proto.HistorySync_PUSH_NAME:
			for _, n := range evt.Data.GetPushnames() {
				jid, err := types.ParseJID(n.GetId())
				if err != nil {
					continue
				}
				s.propagateEvent(newContactEvent(jid, types.ContactInfo{FullName: n.GetPushname()}))
				if err = s.client.SubscribePresence(jid); err != nil {
					s.gateway.logger.Warnf("Failed to subscribe to presence for %s", jid)
				}
			}
		case proto.HistorySync_INITIAL_BOOTSTRAP, proto.HistorySync_RECENT, proto.HistorySync_ON_DEMAND:
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
	case *events.CallTerminate:
		if evt.Reason == "timeout" {
			s.propagateEvent(newCallEvent(CallMissed, evt.BasicCallMeta))
		}
	case *events.LoggedOut:
		s.client.Disconnect()
		if err := s.client.Store.Delete(); err != nil {
			s.gateway.logger.Warnf("Unable to delete local device state on logout: %s", err)
		}
		s.client = nil
		s.propagateEvent(EventLoggedOut, nil)
	case *events.PairSuccess:
		if s.client.Store.ID == nil {
			s.gateway.logger.Errorf("Pairing succeeded, but device ID is missing")
			return
		}
		deviceID := s.client.Store.ID.String()
		s.propagateEvent(EventPair, &EventPayload{PairDeviceID: deviceID})
		if err := s.gateway.CleanupSession(LinkedDevice{ID: deviceID}); err != nil {
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
