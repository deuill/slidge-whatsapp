package whatsapp

import (
	// Standard library.
	"context"
	"fmt"
	"mime"
	"slices"
	"strings"

	// Internal packages.
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/media"

	// Third-party libraries.
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// EventKind represents all event types recognized by the Python session adapter, as emitted by the
// Go session adapter.
type EventKind int

// The event types handled by the overarching session adapter handler.
const (
	EventUnknown EventKind = iota
	EventQRCode
	EventPair
	EventConnect
	EventLoggedOut
	EventContact
	EventPresence
	EventMessage
	EventChatState
	EventReceipt
	EventGroup
	EventCall
)

// EventPayload represents the collected payloads for all event types handled by the overarching
// session adapter handler. Only specific fields will be populated in events emitted by internal
// handlers, see documentation for specific types for more information.
type EventPayload struct {
	QRCode       string
	PairDeviceID string
	Connect      Connect
	Contact      Contact
	Presence     Presence
	Message      Message
	ChatState    ChatState
	Receipt      Receipt
	Group        Group
	Call         Call
}

// HandleEventFunc represents a handler for incoming events sent to the Python adapter, accepting an
// event type and payload.
type HandleEventFunc func(EventKind, *EventPayload)

// Connect represents event data related to a connection to WhatsApp being established, or failing
// to do so (based on the [Connect.Error] result).
type Connect struct {
	JID   string // The device JID given for this connection.
	Error string // The connection error, if any.
}

// A Avatar represents a small image set for a Contact or Group.
type Avatar struct {
	ID  string // The unique ID for this avatar, used for persistent caching.
	URL string // The HTTP URL over which this avatar might be retrieved. Can change for the same ID.
}

// A Contact represents any entity that be communicated with directly in WhatsApp. This typically
// represents people, but may represent a business or bot as well, but not a group-chat.
type Contact struct {
	JID  string // The WhatsApp JID for this contact.
	Name string // The user-set, human-readable name for this contact.
}

// NewContactEvent returns event data meant for [Session.propagateEvent] for the contact information
// given. Unknown or invalid contact information will return an [EventUnknown] event with nil data.
func newContactEvent(jid types.JID, info types.ContactInfo) (EventKind, *EventPayload) {
	var contact = Contact{
		JID: jid.ToNonAD().String(),
	}

	for _, n := range []string{info.FullName, info.FirstName, info.BusinessName, info.PushName} {
		if n != "" {
			contact.Name = n
			break
		}
	}

	// Don't attempt to synchronize contacts with no user-readable name.
	if contact.Name == "" {
		return EventUnknown, nil
	}

	return EventContact, &EventPayload{Contact: contact}
}

// PresenceKind represents the different kinds of activity states possible in WhatsApp.
type PresenceKind int

// The presences handled by the overarching session event handler.
const (
	PresenceUnknown PresenceKind = iota
	PresenceAvailable
	PresenceUnavailable
)

// Precence represents a contact's general state of activity, and is periodically updated as
// contacts start or stop paying attention to their client of choice.
type Presence struct {
	JID      string
	Kind     PresenceKind
	LastSeen int64
}

// NewPresenceEvent returns event data meant for [Session.propagateEvent] for the primitive presence
// event given.
func newPresenceEvent(evt *events.Presence) (EventKind, *EventPayload) {
	var presence = Presence{
		JID:      evt.From.ToNonAD().String(),
		Kind:     PresenceAvailable,
		LastSeen: evt.LastSeen.Unix(),
	}

	if evt.Unavailable {
		presence.Kind = PresenceUnavailable
	}

	return EventPresence, &EventPayload{Presence: presence}
}

// MessageKind represents all concrete message types (plain-text messages, edit messages, reactions)
// recognized by the Python session adapter.
type MessageKind int

// The message types handled by the overarching session event handler.
const (
	MessagePlain MessageKind = iota
	MessageEdit
	MessageRevoke
	MessageReaction
	MessageAttachment
	MessagePoll
)

// A Message represents one of many kinds of bidirectional communication payloads, for example, a
// text message, a file (image, video) attachment, an emoji reaction, etc. Messages of different
// kinds are denoted as such, and re-use fields where the semantics overlap.
type Message struct {
	Kind        MessageKind  // The concrete message kind being sent or received.
	ID          string       // The unique message ID, used for referring to a specific Message instance.
	JID         string       // The JID this message concerns, semantics can change based on IsCarbon.
	GroupJID    string       // The JID of the group-chat this message was sent in, if any.
	OriginJID   string       // For reactions and replies in groups, the JID of the original user.
	Body        string       // The plain-text message body. For attachment messages, this can be a caption.
	Timestamp   int64        // The Unix timestamp denoting when this message was created.
	IsCarbon    bool         // Whether or not this message concerns the gateway user themselves.
	IsForwarded bool         // Whether or not the message was forwarded from another source.
	ReplyID     string       // The unique message ID this message is in reply to, if any.
	ReplyBody   string       // The full body of the message this message is in reply to, if any.
	Attachments []Attachment // The list of file (image, video, etc.) attachments contained in this message.
	Preview     Preview      // A short description for the URL provided in the message body, if any.
	Location    Location     // The location metadata for messages, if any.
	Poll        Poll         // The multiple-choice poll contained in the message, if any.
	Album       Album        // The image album message, if any.
	MentionJIDs []string     // A list of JIDs mentioned in this message, if any.
	Receipts    []Receipt    // The receipt statuses for the message, typically provided alongside historical messages.
	Reactions   []Message    // Reactions attached to message, typically provided alongside historical messages.
}

// A Attachment represents additional binary data (e.g. images, videos, documents) provided alongside
// a message, for display or storage on the recepient client.
type Attachment struct {
	MIME     string // The MIME type for attachment.
	Filename string // The recommended file name for this attachment. May be an auto-generated name.
	Caption  string // The user-provided caption, provided alongside this attachment.
	Data     []byte // Data for the attachment.

	// Internal fields.
	spec *media.Spec // Metadata specific to audio/video files, used in processing.
}

// GetSpec returns metadata for this attachment, as derived from the underlying attachment data.
func (a *Attachment) GetSpec(ctx context.Context) (*media.Spec, error) {
	if a.spec != nil {
		return a.spec, nil
	}

	spec, err := media.GetSpec(ctx, a.Data)
	if err != nil {
		return nil, err
	}

	a.spec = spec
	return a.spec, nil
}

// PreviewKind represents different ways of previewingadditional data inline with messages.
type PreviewKind int

const (
	PreviewPlain PreviewKind = iota
	PreviewVideo
)

// A Preview represents a short description for a URL provided in a message body, as usually derived
// from the content of the page pointed at.
type Preview struct {
	Kind        PreviewKind // The kind of preview to show, defaults to plain URL preview.
	URL         string      // The original (or canonical) URL this preview was generated for.
	Title       string      // The short title for the URL preview.
	Description string      // The (optional) long-form description for the URL preview.
	Thumbnail   []byte      // The (optional) thumbnail image data.
}

// A Location represents additional metadata given to location messages.
type Location struct {
	Latitude  float64
	Longitude float64
	Accuracy  int
	IsLive    bool

	// Optional fields given for named locations.
	Name    string
	Address string
	URL     string
}

// A Poll represents a multiple-choice question, on which each choice might be voted for one or more
// times.
type Poll struct {
	Title   string       // The human-readable name of this poll.
	Options []PollOption // The list of choices to vote on in the poll.
}

// A PollOption represents an individual choice within a broader poll.
type PollOption struct {
	Title string // The human-readable name for the poll option.
}

// A Album message represents a collection of media files, typically images and videos.
type Album struct {
	IsAlbum    bool // Whether or not the message is an album, regardless of calculated media counts.
	ImageCount int  // The calculated amount of images in the album, might not be accurate.
	VideoCount int  // The calculated amount of videos in the album, might not be accurate.
}

// NewMessageEvent returns event data meant for [Session.propagateEvent] for the primive message
// event given. Unknown or invalid messages will return an [EventUnknown] event with nil data.
func newMessageEvent(client *whatsmeow.Client, evt *events.Message) (EventKind, *EventPayload) {
	// Set basic data for message, to be potentially amended depending on the concrete version of
	// the underlying message.
	var ctx = context.Background()
	var message = Message{
		Kind:      MessagePlain,
		ID:        evt.Info.ID,
		JID:       evt.Info.Sender.ToNonAD().String(),
		Body:      evt.Message.GetConversation(),
		Timestamp: evt.Info.Timestamp.Unix(),
		IsCarbon:  evt.Info.IsFromMe,
	}

	if evt.Info.Chat.Server == types.BroadcastServer {
		// Handle non-carbon, non-status broadcast messages as plain messages; support for other
		// types is lacking in the XMPP world.
		if evt.Info.Chat.User == types.StatusBroadcastJID.User || message.IsCarbon {
			return EventUnknown, nil
		}
	} else if evt.Info.IsGroup {
		message.GroupJID = evt.Info.Chat.ToNonAD().String()
	} else if message.IsCarbon {
		message.JID = evt.Info.Chat.ToNonAD().String()
	}

	// Handle handle protocol messages (such as message deletion or editing).
	if p := evt.Message.GetProtocolMessage(); p != nil {
		switch p.GetType() {
		case waE2E.ProtocolMessage_MESSAGE_EDIT:
			if m := p.GetEditedMessage(); m != nil {
				message.Kind = MessageEdit
				message.ID = p.Key.GetID()
				message.Body = m.GetConversation()
			} else {
				return EventUnknown, nil
			}
		case waE2E.ProtocolMessage_REVOKE:
			message.Kind = MessageRevoke
			message.ID = p.Key.GetID()
			message.OriginJID = p.Key.GetParticipant()
			return EventMessage, &EventPayload{Message: message}
		}
	}

	// Handle emoji reaction to existing message.
	if r := evt.Message.GetReactionMessage(); r != nil {
		message.Kind = MessageReaction
		message.ID = r.Key.GetID()
		message.Body = r.GetText()
		return EventMessage, &EventPayload{Message: message}
	}

	// Handle location (static and live) message.
	if l := evt.Message.GetLocationMessage(); l != nil {
		message.Location = Location{
			Latitude:  l.GetDegreesLatitude(),
			Longitude: l.GetDegreesLongitude(),
			Accuracy:  int(l.GetAccuracyInMeters()),
			IsLive:    l.GetIsLive(),
			Name:      l.GetName(),
			Address:   l.GetAddress(),
			URL:       l.GetURL(),
		}
		return EventMessage, &EventPayload{Message: message}
	}

	if l := evt.Message.GetLiveLocationMessage(); l != nil {
		message.Body = l.GetCaption()
		message.Location = Location{
			Latitude:  l.GetDegreesLatitude(),
			Longitude: l.GetDegreesLongitude(),
			Accuracy:  int(l.GetAccuracyInMeters()),
			IsLive:    true,
		}
		return EventMessage, &EventPayload{Message: message}
	}

	// Handle poll messages.
	for _, p := range []*waE2E.PollCreationMessage{
		evt.Message.GetPollCreationMessageV3(),
		evt.Message.GetPollCreationMessageV2(),
		evt.Message.GetPollCreationMessage(),
	} {
		if p == nil {
			continue
		}
		message.Kind = MessagePoll
		message.Poll = Poll{Title: p.GetName()}
		for _, o := range p.GetOptions() {
			message.Poll.Options = append(message.Poll.Options, PollOption{
				Title: o.GetOptionName(),
			})
		}
		return EventMessage, &EventPayload{Message: message}
	}

	// Handle "album" messages, denoting a grouping of media messages to follow.
	if a := evt.Message.GetAlbumMessage(); a != nil {
		message.Album = Album{
			IsAlbum:    true,
			ImageCount: int(a.GetExpectedImageCount()),
			VideoCount: int(a.GetExpectedVideoCount()),
		}
		return EventMessage, &EventPayload{Message: message}
	}

	// Handle message attachments, if any.
	if attach, context, err := getMessageAttachments(ctx, client, evt.Message); err != nil {
		client.Log.Errorf("Failed getting message attachments: %s", err)
		return EventUnknown, nil
	} else if len(attach) > 0 {
		message.Attachments = append(message.Attachments, attach...)
		message.Kind = MessageAttachment
		if context != nil {
			message = getMessageWithContext(message, context)
		}
	}

	// Get extended information from message, if available. Extended messages typically represent
	// messages with additional context, such as replies, forwards, etc.
	if e := evt.Message.GetExtendedTextMessage(); e != nil {
		if message.Body == "" {
			message.Body = e.GetText()
		}

		message = getMessageWithContext(message, e.GetContextInfo())
	}

	// Ignore obviously invalid messages.
	if message.Kind == MessagePlain && message.Body == "" {
		return EventUnknown, nil
	}

	return EventMessage, &EventPayload{Message: message}
}

// GetMessageWithContext processes the given [Message] and applies any context metadata might be
// useful; examples of context include messages being quoted. If no context is found, the original
// message is returned unchanged.
func getMessageWithContext(message Message, info *waE2E.ContextInfo) Message {
	if info == nil {
		return message
	}

	message.ReplyID = info.GetStanzaID()
	message.OriginJID = info.GetParticipant()
	message.IsForwarded = info.GetIsForwarded()

	if q := info.GetQuotedMessage(); q != nil {
		if qe := q.GetExtendedTextMessage(); qe != nil {
			message.ReplyBody = qe.GetText()
		} else {
			message.ReplyBody = q.GetConversation()
		}
	}

	return message
}

// GetMessageAttachments fetches and decrypts attachments (images, audio, video, or documents) sent
// via WhatsApp. Any failures in retrieving any attachment will return an error immediately.
func getMessageAttachments(ctx context.Context, client *whatsmeow.Client, message *waE2E.Message) ([]Attachment, *waE2E.ContextInfo, error) {
	var result []Attachment
	var info *waE2E.ContextInfo
	var convertSpec *media.Spec
	var kinds = []whatsmeow.DownloadableMessage{
		message.GetImageMessage(),
		message.GetAudioMessage(),
		message.GetVideoMessage(),
		message.GetDocumentMessage(),
		message.GetStickerMessage(),
	}

	for _, msg := range kinds {
		// Handle data for specific attachment type.
		var a Attachment
		switch msg := msg.(type) {
		case *waE2E.ImageMessage:
			a.MIME, a.Caption = msg.GetMimetype(), msg.GetCaption()
		case *waE2E.AudioMessage:
			// Convert Opus-encoded voice messages to AAC-encoded audio, which has better support.
			a.MIME = msg.GetMimetype()
			if msg.GetPTT() {
				convertSpec = &media.Spec{MIME: media.TypeM4A}
			}
		case *waE2E.VideoMessage:
			a.MIME, a.Caption = msg.GetMimetype(), msg.GetCaption()
		case *waE2E.DocumentMessage:
			a.MIME, a.Caption, a.Filename = msg.GetMimetype(), msg.GetCaption(), msg.GetFileName()
		case *waE2E.StickerMessage:
			a.MIME = msg.GetMimetype()
		}

		// Ignore attachments with empty or unknown MIME types.
		if a.MIME == "" {
			continue
		}

		// Attempt to download and decrypt raw attachment data, if any.
		data, err := client.Download(ctx, msg)
		if err != nil {
			return nil, nil, err
		}

		a.Data = data

		// Convert incoming data if a specification has been given, ignoring any errors that occur.
		if convertSpec != nil {
			data, err = media.Convert(ctx, a.Data, convertSpec)
			if err == nil {
				a.Data, a.MIME = data, string(convertSpec.MIME)
			}
		}

		// Set filename from SHA256 checksum and MIME type, if none is already set.
		if a.Filename == "" {
			a.Filename = fmt.Sprintf("%x%s", msg.GetFileSHA256(), extensionByType(a.MIME))
		}

		result = append(result, a)
	}

	// Handle any contact vCard as attachment.
	if c := message.GetContactMessage(); c != nil {
		result = append(result, Attachment{
			MIME:     "text/vcard",
			Filename: c.GetDisplayName() + ".vcf",
			Data:     []byte(c.GetVcard()),
		})
		info = c.GetContextInfo()
	}

	return result, info, nil
}

const (
	// The MIME type used by voice messages on WhatsApp.
	voiceMessageMIME = string(media.TypeOgg) + "; codecs=opus"
	// The MIME type used by animated images on WhatsApp.
	animatedImageMIME = "image/gif"

	// The maximum image attachment size we'll attempt to process in any way, in bytes.
	maxConvertImageSize = 1024 * 1024 * 10 // 10MiB
	// The maximum audio/video attachment size we'll attempt to process in any way, in bytes.
	maxConvertAudioVideoSize = 1024 * 1024 * 20 // 20MiB

	// The maximum number of samples to return in media waveforms.
	maxWaveformSamples = 64
)

var (
	// Default target specification for voice messages.
	voiceMessageSpec = media.Spec{
		MIME:            media.MIMEType(voiceMessageMIME),
		AudioBitRate:    64,
		AudioChannels:   1,
		AudioSampleRate: 48000,
		StripMetadata:   true,
	}

	// Default target specification for generic audio messages.
	audioMessageSpec = media.Spec{
		MIME:            media.TypeM4A,
		AudioBitRate:    160,
		AudioSampleRate: 44100,
	}

	// Default target specification for video messages with inline preview.
	videoMessageSpec = media.Spec{
		MIME:             media.TypeMP4,
		AudioBitRate:     160,
		AudioSampleRate:  44100,
		VideoFilter:      "pad=ceil(iw/2)*2:ceil(ih/2)*2",
		VideoFrameRate:   25,
		VideoPixelFormat: "yuv420p",
		StripMetadata:    true,
	}

	// Default target specification for image messages with inline preview.
	imageMessageSpec = media.Spec{
		MIME:         media.TypeJPEG,
		ImageQuality: 85,
	}

	// Default target specifications for default and preview-size thumbnails.
	defaultThumbnailSpec = media.Spec{
		MIME:          media.TypeJPEG,
		ImageWidth:    100,
		StripMetadata: true,
	}
	previewThumbnailSpec = media.Spec{
		MIME:          media.TypeJPEG,
		ImageWidth:    250,
		StripMetadata: true,
	}
)

// ConvertAttachment attempts to process a given attachment from a less-supported type to a
// canonically supported one; for example, from `image/png` to `image/jpeg`.
//
// Decisions about which MIME types to convert to are based on the concrete MIME type inferred from
// the file itself, and care is taken to conform to WhatsApp semantics for the given input MIME
// type.
//
// If the input MIME type is unknown, or conversion is impossible, the given attachment is not
// changed.
func convertAttachment(ctx context.Context, attach *Attachment) error {
	var detectedMIME media.MIMEType
	if t := media.DetectMIMEType(attach.Data); t != media.TypeUnknown {
		detectedMIME = t
		if attach.MIME == "" || attach.MIME == "application/octet-stream" {
			attach.MIME = string(detectedMIME)
		}
	}

	var spec media.Spec
	switch detectedMIME {
	case media.TypePNG, media.TypeWebP:
		// Convert common image formats to JPEG for inline preview.
		if len(attach.Data) > maxConvertImageSize {
			return fmt.Errorf("attachment size %d exceeds maximum of %d", len(attach.Data), maxConvertImageSize)
		}

		spec = imageMessageSpec
	case media.TypeGIF:
		// Convert GIFs to JPEG or MP4, if animated, as required by WhatsApp.
		if len(attach.Data) > maxConvertImageSize {
			return fmt.Errorf("attachment size %d exceeds maximum of %d", len(attach.Data), maxConvertImageSize)
		}

		spec = imageMessageSpec
		if s, err := attach.GetSpec(ctx); err == nil && s.ImageFrameRate > 0 {
			spec = videoMessageSpec
			spec.ImageFrameRate = s.ImageFrameRate
		}
	case media.TypeM4A:
		if len(attach.Data) > maxConvertAudioVideoSize {
			return fmt.Errorf("attachment size %d exceeds maximum of %d", len(attach.Data), maxConvertAudioVideoSize)
		}

		spec = voiceMessageSpec

		if s, err := attach.GetSpec(ctx); err == nil {
			if s.AudioCodec == "alac" {
				// Don't attempt to process lossless files at all, as it's assumed that the sender
				// wants to retain these characteristics. Since WhatsApp will try (and likely fail)
				// to process this as an audio message anyways, set a unique MIME type.
				attach.MIME = "application/octet-stream"
				return nil
			}
		}
	case media.TypeOgg:
		if len(attach.Data) > maxConvertAudioVideoSize {
			return fmt.Errorf("attachment size %d exceeds maximum of %d", len(attach.Data), maxConvertAudioVideoSize)
		}

		spec = audioMessageSpec
		if s, err := attach.GetSpec(ctx); err == nil {
			if s.AudioCodec == "opus" {
				// Assume that Opus-encoded Ogg files are meant to be voice messages, and re-encode
				// them as such for WhatsApp.
				spec = voiceMessageSpec
			}
		}
	case media.TypeMP4, media.TypeWebM:
		if len(attach.Data) > maxConvertAudioVideoSize {
			return fmt.Errorf("attachment size %d exceeds maximum of %d", len(attach.Data), maxConvertAudioVideoSize)
		}

		spec = videoMessageSpec

		if s, err := attach.GetSpec(ctx); err == nil {
			// Try to see if there's a video stream for ostensibly video-related MIME types, as
			// these are some times misdetected as such.
			if s.VideoWidth == 0 && s.VideoHeight == 0 && s.AudioSampleRate > 0 && s.Duration > 0 {
				spec = voiceMessageSpec
				spec.SourceMIME = media.TypeM4A
			}
		}
	default:
		// Detected source MIME not in list we're willing to convert, move on without error.
		return nil
	}

	// Convert attachment between file-types, if source MIME matches the known list of convertable types.
	if spec.SourceMIME == "" {
		spec.SourceMIME = detectedMIME
	}

	data, err := media.Convert(ctx, attach.Data, &spec)
	if err != nil {
		return fmt.Errorf("failed converting attachment: %w", err)
	}

	attach.Data, attach.MIME = data, string(spec.MIME)
	return nil
}

// KnownMediaTypes represents MIME type to WhatsApp media types known to be handled by WhatsApp in a
// special way (that is, not as generic file uploads).
var knownMediaTypes = map[string]whatsmeow.MediaType{
	"image/jpeg": whatsmeow.MediaImage,
	"audio/mpeg": whatsmeow.MediaAudio,
	"audio/mp4":  whatsmeow.MediaAudio,
	"audio/aac":  whatsmeow.MediaAudio,
	"audio/ogg":  whatsmeow.MediaAudio,
	"video/mp4":  whatsmeow.MediaVideo,
}

// UploadAttachment attempts to push the given attachment data to WhatsApp according to the MIME
// type specified within. Attachments are handled as generic file uploads unless they're of a
// specific format; in addition, certain MIME types may be automatically converted to a
// well-supported type via FFmpeg (if available).
func uploadAttachment(ctx context.Context, client *whatsmeow.Client, attach *Attachment) (*waE2E.Message, error) {
	var originalMIME = attach.MIME
	if err := convertAttachment(ctx, attach); err != nil {
		client.Log.Warnf("failed to auto-convert attachment: %s", err)
	}

	mediaType := knownMediaTypes[getBaseMediaType(attach.MIME)]
	if mediaType == "" {
		mediaType = whatsmeow.MediaDocument
	}

	if len(attach.Data) == 0 {
		return nil, fmt.Errorf("attachment file contains no data")
	}

	upload, err := client.Upload(ctx, attach.Data, mediaType)
	if err != nil {
		return nil, err
	}

	var message *waE2E.Message
	switch mediaType {
	case whatsmeow.MediaImage:
		message = &waE2E.Message{
			ImageMessage: &waE2E.ImageMessage{
				URL:           &upload.URL,
				DirectPath:    &upload.DirectPath,
				MediaKey:      upload.MediaKey,
				Mimetype:      &attach.MIME,
				FileEncSHA256: upload.FileEncSHA256,
				FileSHA256:    upload.FileSHA256,
				FileLength:    ptrTo(uint64(len(attach.Data))),
			},
		}
		t, err := media.Convert(ctx, attach.Data, &defaultThumbnailSpec)
		if err != nil {
			client.Log.Warnf("failed generating attachment thumbnail: %s", err)
		} else {
			message.ImageMessage.JPEGThumbnail = t
		}
	case whatsmeow.MediaAudio:
		spec, err := attach.GetSpec(ctx)
		if err != nil {
			client.Log.Warnf("failed fetching attachment metadata: %s", err)
			spec = &media.Spec{}
		}
		message = &waE2E.Message{
			AudioMessage: &waE2E.AudioMessage{
				URL:           &upload.URL,
				DirectPath:    &upload.DirectPath,
				MediaKey:      upload.MediaKey,
				Mimetype:      &attach.MIME,
				FileEncSHA256: upload.FileEncSHA256,
				FileSHA256:    upload.FileSHA256,
				FileLength:    ptrTo(uint64(len(attach.Data))),
				Seconds:       ptrTo(uint32(spec.Duration.Seconds())),
			},
		}
		if attach.MIME == voiceMessageMIME {
			message.AudioMessage.PTT = ptrTo(true)
			if spec != nil {
				w, err := media.GetWaveform(ctx, attach.Data, spec, maxWaveformSamples)
				if err != nil {
					client.Log.Warnf("failed generating attachment waveform: %s", err)
				} else {
					message.AudioMessage.Waveform = w
				}
			}
		}
	case whatsmeow.MediaVideo:
		spec, err := attach.GetSpec(ctx)
		if err != nil {
			client.Log.Warnf("failed fetching attachment metadata: %s", err)
			spec = &media.Spec{}
		}
		message = &waE2E.Message{
			VideoMessage: &waE2E.VideoMessage{
				URL:           &upload.URL,
				DirectPath:    &upload.DirectPath,
				MediaKey:      upload.MediaKey,
				Mimetype:      &attach.MIME,
				FileEncSHA256: upload.FileEncSHA256,
				FileSHA256:    upload.FileSHA256,
				FileLength:    ptrTo(uint64(len(attach.Data))),
				Seconds:       ptrTo(uint32(spec.Duration.Seconds())),
				Width:         ptrTo(uint32(spec.VideoWidth)),
				Height:        ptrTo(uint32(spec.VideoHeight)),
			},
		}
		t, err := media.Convert(ctx, attach.Data, &defaultThumbnailSpec)
		if err != nil {
			client.Log.Warnf("failed generating attachment thumbnail: %s", err)
		} else {
			message.VideoMessage.JPEGThumbnail = t
		}
		if originalMIME == animatedImageMIME {
			message.VideoMessage.GifPlayback = ptrTo(true)
		}
	case whatsmeow.MediaDocument:
		message = &waE2E.Message{
			DocumentMessage: &waE2E.DocumentMessage{
				URL:           &upload.URL,
				DirectPath:    &upload.DirectPath,
				MediaKey:      upload.MediaKey,
				Mimetype:      &attach.MIME,
				FileEncSHA256: upload.FileEncSHA256,
				FileSHA256:    upload.FileSHA256,
				FileLength:    ptrTo(uint64(len(attach.Data))),
				FileName:      &attach.Filename,
			},
		}
		switch media.MIMEType(attach.MIME) {
		case media.TypePDF:
			if spec, err := attach.GetSpec(ctx); err != nil {
				client.Log.Warnf("failed fetching attachment metadata: %s", err)
			} else {
				message.DocumentMessage.PageCount = ptrTo(uint32(spec.DocumentPage))
			}
			t, err := media.Convert(ctx, attach.Data, &previewThumbnailSpec)
			if err != nil {
				client.Log.Warnf("failed generating attachment thumbnail: %s", err)
			} else {
				message.DocumentMessage.JPEGThumbnail = t
			}
		}
	}

	return message, nil
}

// KnownExtensions represents MIME type to file-extension mappings for basic, known media types.
var knownExtensions = map[string]string{
	"image/jpeg": ".jpg",
	"audio/ogg":  ".oga",
	"audio/mp4":  ".m4a",
	"video/mp4":  ".mp4",
}

// ExtensionByType returns the file extension for the given MIME type, or a generic extension if the
// MIME type is unknown.
func extensionByType(typ string) string {
	// Handle common, known MIME types first.
	if ext := knownExtensions[typ]; ext != "" {
		return ext
	}
	if ext, _ := mime.ExtensionsByType(typ); len(ext) > 0 {
		return ext[0]
	}
	return ".bin"
}

// GetBaseMediaType returns the media type without any additional parameters.
func getBaseMediaType(typ string) string {
	return strings.SplitN(typ, ";", 2)[0]
}

// NewEventFromHistory returns event data meant for [Session.propagateEvent] for the primive history
// message given. Currently, only events related to group-chats will be handled, due to uncertain
// support for history back-fills on 1:1 chats.
//
// Otherwise, the implementation largely follows that of [newMessageEvent], however the base types
// used by these two functions differ in many small ways which prevent unifying the approach.
//
// Typically, this will return [EventMessage] events with appropriate [Message] payloads; unknown or
// invalid messages will return an [EventUnknown] event with nil data.
func newEventFromHistory(client *whatsmeow.Client, info *waWeb.WebMessageInfo) (EventKind, *EventPayload) {
	// Handle message as group message is remote JID is a group JID in the absence of any other,
	// specific signal, or don't handle at all if no group JID is found.
	var ctx = context.Background()
	var jid = info.GetKey().GetRemoteJID()
	if j, _ := types.ParseJID(jid); j.Server != types.GroupServer {
		return EventUnknown, nil
	}

	// Set basic data for message, to be potentially amended depending on the concrete version of
	// the underlying message.
	var message = Message{
		Kind:      MessagePlain,
		ID:        info.GetKey().GetID(),
		GroupJID:  info.GetKey().GetRemoteJID(),
		Body:      info.GetMessage().GetConversation(),
		Timestamp: int64(info.GetMessageTimestamp()),
		IsCarbon:  info.GetKey().GetFromMe(),
	}

	if info.Participant != nil {
		message.JID = info.GetParticipant()
	} else if info.GetKey().GetFromMe() {
		message.JID = client.Store.ID.ToNonAD().String()
	} else {
		// It's likely we cannot handle this message correctly if we don't know the concrete
		// sender, so just ignore it completely.
		return EventUnknown, nil
	}

	// Handle handle protocol messages (such as message deletion or editing), while ignoring known
	// unhandled types.
	switch info.GetMessageStubType() {
	case waWeb.WebMessageInfo_CIPHERTEXT:
		return EventUnknown, nil
	case waWeb.WebMessageInfo_CALL_MISSED_VOICE, waWeb.WebMessageInfo_CALL_MISSED_VIDEO:
		return EventCall, &EventPayload{Call: Call{
			State:     CallMissed,
			JID:       info.GetKey().GetRemoteJID(),
			Timestamp: int64(info.GetMessageTimestamp()),
		}}
	case waWeb.WebMessageInfo_REVOKE:
		if p := info.GetMessageStubParameters(); len(p) > 0 {
			message.Kind = MessageRevoke
			message.ID = p[0]
			return EventMessage, &EventPayload{Message: message}
		} else {
			return EventUnknown, nil
		}
	}

	// Handle emoji reaction to existing message.
	for _, r := range info.GetReactions() {
		if r.GetText() != "" {
			message.Reactions = append(message.Reactions, Message{
				Kind:      MessageReaction,
				ID:        r.GetKey().GetID(),
				JID:       r.GetKey().GetRemoteJID(),
				Body:      r.GetText(),
				Timestamp: r.GetSenderTimestampMS() / 1000,
				IsCarbon:  r.GetKey().GetFromMe(),
			})
		}
	}

	// Handle message attachments, if any.
	if attach, context, err := getMessageAttachments(ctx, client, info.GetMessage()); err != nil {
		client.Log.Errorf("Failed getting message attachments: %s", err)
		return EventUnknown, nil
	} else if len(attach) > 0 {
		message.Attachments = append(message.Attachments, attach...)
		message.Kind = MessageAttachment
		if context != nil {
			message = getMessageWithContext(message, context)
		}
	}

	// Handle pre-set receipt status, if any.
	for _, r := range info.GetUserReceipt() {
		// Ignore self-receipts for the moment, as these cannot be handled correctly by the adapter.
		if client.Store.ID.ToNonAD().String() == r.GetUserJID() {
			continue
		}
		var receipt = Receipt{MessageIDs: []string{message.ID}, JID: r.GetUserJID(), GroupJID: message.GroupJID}
		switch info.GetStatus() {
		case waWeb.WebMessageInfo_DELIVERY_ACK:
			receipt.Kind = ReceiptDelivered
			receipt.Timestamp = r.GetReceiptTimestamp()
		case waWeb.WebMessageInfo_READ:
			receipt.Kind = ReceiptRead
			receipt.Timestamp = r.GetReadTimestamp()
		}
		message.Receipts = append(message.Receipts, receipt)
	}

	// Get extended information from message, if available. Extended messages typically represent
	// messages with additional context, such as replies, forwards, etc.
	if e := info.GetMessage().GetExtendedTextMessage(); e != nil {
		if message.Body == "" {
			message.Body = e.GetText()
		}

		message = getMessageWithContext(message, e.GetContextInfo())
	}

	// Ignore obviously invalid messages.
	if message.Kind == MessagePlain && message.Body == "" {
		return EventUnknown, nil
	}

	return EventMessage, &EventPayload{Message: message}
}

// ChatStateKind represents the different kinds of chat-states possible in WhatsApp.
type ChatStateKind int

// The chat states handled by the overarching session event handler.
const (
	ChatStateUnknown ChatStateKind = iota
	ChatStateComposing
	ChatStatePaused
)

// A ChatState represents the activity of a contact within a certain discussion, for instance,
// whether the contact is currently composing a message. This is separate to the concept of a
// Presence, which is the contact's general state across all discussions.
type ChatState struct {
	Kind     ChatStateKind
	JID      string
	GroupJID string
}

// NewChatStateEvent returns event data meant for [Session.propagateEvent] for the primitive
// chat-state event given.
func newChatStateEvent(evt *events.ChatPresence) (EventKind, *EventPayload) {
	var state = ChatState{JID: evt.Sender.ToNonAD().String()}
	if evt.IsGroup {
		state.GroupJID = evt.Chat.ToNonAD().String()
	}
	switch evt.State {
	case types.ChatPresenceComposing:
		state.Kind = ChatStateComposing
	case types.ChatPresencePaused:
		state.Kind = ChatStatePaused
	}
	return EventChatState, &EventPayload{ChatState: state}
}

// ReceiptKind represents the different types of delivery receipts possible in WhatsApp.
type ReceiptKind int

// The delivery receipts handled by the overarching session event handler.
const (
	ReceiptUnknown ReceiptKind = iota
	ReceiptDelivered
	ReceiptRead
)

// A Receipt represents a notice of delivery or presentation for [Message] instances sent or
// received. Receipts can be delivered for many messages at once, but are generally all delivered
// under one specific state at a time.
type Receipt struct {
	Kind       ReceiptKind // The distinct kind of receipt presented.
	MessageIDs []string    // The list of message IDs to mark for receipt.
	JID        string
	GroupJID   string
	Timestamp  int64
	IsCarbon   bool
}

// NewReceiptEvent returns event data meant for [Session.propagateEvent] for the primive receipt
// event given. Unknown or invalid receipts will return an [EventUnknown] event with nil data.
func newReceiptEvent(evt *events.Receipt) (EventKind, *EventPayload) {
	var receipt = Receipt{
		MessageIDs: slices.Clone(evt.MessageIDs),
		JID:        evt.Sender.ToNonAD().String(),
		Timestamp:  evt.Timestamp.Unix(),
		IsCarbon:   evt.IsFromMe,
	}

	if len(receipt.MessageIDs) == 0 {
		return EventUnknown, nil
	}

	if evt.Chat.Server == types.BroadcastServer {
		receipt.JID = evt.BroadcastListOwner.ToNonAD().String()
	} else if evt.IsGroup {
		receipt.GroupJID = evt.Chat.ToNonAD().String()
	} else if receipt.IsCarbon {
		receipt.JID = evt.Chat.ToNonAD().String()
	}

	switch evt.Type {
	case types.ReceiptTypeDelivered:
		receipt.Kind = ReceiptDelivered
	case types.ReceiptTypeRead:
		receipt.Kind = ReceiptRead
	}

	return EventReceipt, &EventPayload{Receipt: receipt}
}

// GroupAffiliation represents the set of privilidges given to a specific participant in a group.
type GroupAffiliation int

const (
	GroupAffiliationNone  GroupAffiliation = iota // None, or normal member group affiliation.
	GroupAffiliationAdmin                         // Can perform some management operations.
	GroupAffiliationOwner                         // Can manage group fully, including destroying the group.
)

// A Group represents a named, many-to-many chat space which may be joined or left at will. All
// fields apart from the group JID are considered to be optional, and may not be set in cases where
// group information is being updated against previous assumed state. Groups in WhatsApp are
// generally invited to out-of-band with respect to overarching adaptor; see the documentation for
// [Session.GetGroups] for more information.
type Group struct {
	JID          string             // The WhatsApp JID for this group.
	Name         string             // The user-defined, human-readable name for this group.
	Subject      GroupSubject       // The longer-form, user-defined description for this group.
	Nickname     string             // Our own nickname in this group-chat.
	Participants []GroupParticipant // The list of participant contacts for this group, including ourselves.
}

// A GroupSubject represents the user-defined group description and attached metadata thereof, for a
// given [Group].
type GroupSubject struct {
	Subject  string // The user-defined group description.
	SetAt    int64  // The exact time this group description was set at, as a timestamp.
	SetByJID string // The JID of the user that set the subject.
}

// GroupParticipantAction represents the distinct set of actions that can be taken when encountering
// a group participant, typically to add or remove.
type GroupParticipantAction int

const (
	GroupParticipantActionAdd     GroupParticipantAction = iota // Default action; add participant to list.
	GroupParticipantActionRemove                                // Remove participant from list, if existing.
	GroupParticipantActionPromote                               // Make group member into administrator.
	GroupParticipantActionDemote                                // Make group administrator into member.
)

// ToParticipantChange converts our public [GroupParticipantAction] to the internal [ParticipantChange]
// representation.
func (a GroupParticipantAction) toParticipantChange() whatsmeow.ParticipantChange {
	switch a {
	case GroupParticipantActionRemove:
		return whatsmeow.ParticipantChangeRemove
	case GroupParticipantActionPromote:
		return whatsmeow.ParticipantChangePromote
	case GroupParticipantActionDemote:
		return whatsmeow.ParticipantChangeDemote
	default:
		return whatsmeow.ParticipantChangeAdd
	}
}

// A GroupParticipant represents a contact who is currently joined in a given group. Participants in
// WhatsApp can always be derived back to their individual [Contact]; there are no anonymous groups
// in WhatsApp.
type GroupParticipant struct {
	JID         string                 // The WhatsApp JID for this participant.
	Affiliation GroupAffiliation       // The set of priviledges given to this specific participant.
	Action      GroupParticipantAction // The specific action to take for this participant; typically to add.
}

// NewGroupParticipant returns a [GroupParticipant], filling fields from the internal participant
// type. This is a no-op if [types.GroupParticipant.Error] is non-zero, and other fields may only
// be set optionally.
func newGroupParticipant(p types.GroupParticipant) GroupParticipant {
	if p.Error > 0 {
		return GroupParticipant{}
	}
	var affiliation = GroupAffiliationNone
	if p.IsSuperAdmin {
		affiliation = GroupAffiliationOwner
	} else if p.IsAdmin {
		affiliation = GroupAffiliationAdmin
	}
	return GroupParticipant{
		JID:         p.JID.ToNonAD().String(),
		Affiliation: affiliation,
	}
}

// NewGroupEvent returns event data meant for [Session.propagateEvent] for the primive group event
// given. Group data returned by this function can be partial, and callers should take care to only
// handle non-empty values.
func newGroupEvent(evt *events.GroupInfo) (EventKind, *EventPayload) {
	var group = Group{JID: evt.JID.ToNonAD().String()}
	if evt.Name != nil {
		group.Name = evt.Name.Name
	}
	if evt.Topic != nil {
		group.Subject = GroupSubject{
			Subject:  evt.Topic.Topic,
			SetAt:    evt.Topic.TopicSetAt.Unix(),
			SetByJID: evt.Topic.TopicSetBy.ToNonAD().String(),
		}
	}
	for _, p := range evt.Join {
		group.Participants = append(group.Participants, GroupParticipant{
			JID:    p.ToNonAD().String(),
			Action: GroupParticipantActionAdd,
		})
	}
	for _, p := range evt.Leave {
		group.Participants = append(group.Participants, GroupParticipant{
			JID:    p.ToNonAD().String(),
			Action: GroupParticipantActionRemove,
		})
	}
	for _, p := range evt.Promote {
		group.Participants = append(group.Participants, GroupParticipant{
			JID:         p.ToNonAD().String(),
			Action:      GroupParticipantActionPromote,
			Affiliation: GroupAffiliationAdmin,
		})
	}
	for _, p := range evt.Demote {
		group.Participants = append(group.Participants, GroupParticipant{
			JID:         p.ToNonAD().String(),
			Action:      GroupParticipantActionDemote,
			Affiliation: GroupAffiliationNone,
		})
	}
	return EventGroup, &EventPayload{Group: group}
}

// NewGroup returns a concrete [Group] for the primitive data given. This function will generally
// populate fields with as much data as is available from the remote, and is therefore should not
// be called when partial data is to be returned.
func newGroup(client *whatsmeow.Client, info *types.GroupInfo) Group {
	var participants []GroupParticipant
	for i := range info.Participants {
		p := newGroupParticipant(info.Participants[i])
		if p.JID == "" {
			continue
		}
		participants = append(participants, p)
	}
	return Group{
		JID:  info.JID.ToNonAD().String(),
		Name: info.Name,
		Subject: GroupSubject{
			Subject:  info.Topic,
			SetAt:    info.TopicSetAt.Unix(),
			SetByJID: info.TopicSetBy.ToNonAD().String(),
		},
		Nickname:     client.Store.PushName,
		Participants: participants,
	}
}

// CallState represents the state of the call to synchronize with.
type CallState int

// The call states handled by the overarching session event handler.
const (
	CallUnknown CallState = iota
	CallIncoming
	CallMissed
)

// CallStateFromReason converts the given (internal) reason string to a public [CallState]. Calls
// given invalid or unknown reasons will return the [CallUnknown] state.
func callStateFromReason(reason string) CallState {
	switch reason {
	case "", "timeout":
		return CallMissed
	default:
		return CallUnknown
	}
}

// A Call represents an incoming or outgoing voice/video call made over WhatsApp. Full support for
// calls is currently not implemented, and this structure contains the bare minimum data required
// for notifying on missed calls.
type Call struct {
	State     CallState
	JID       string
	Timestamp int64
}

// NewCallEvent returns event data meant for [Session.propagateEvent] for the call metadata given.
func newCallEvent(state CallState, meta types.BasicCallMeta) (EventKind, *EventPayload) {
	if state == CallUnknown || meta.From.IsEmpty() {
		return EventUnknown, nil
	}

	return EventCall, &EventPayload{Call: Call{
		State:     state,
		JID:       meta.From.ToNonAD().String(),
		Timestamp: meta.Timestamp.Unix(),
	}}
}
