package whatsapp

import (
	// Third-party libraries.
	"go.mau.fi/whatsmeow/types"
)

// AddressKind represents any known kinds of [Address] values handled by the adapter.
type AddressKind int

// The list of known [Address] kinds.
const (
	AddressUnknown AddressKind = iota
	AddressPhoneNumber
	AddressHidden
	AddressGroup
	AddressBot
)

// A Address represents an identifier used for referencing specific contacts or groups, and allows
// for storing different kinds of representations based on the [AddressKind] assigned.
type Address struct {
	kind  AddressKind
	value string
}

// NewAddress returns a new [Address] for the value and kind given.
func NewAddress(v string, kind AddressKind) Address {
	return Address{kind: kind, value: v}
}

// ParseAddress return a new [Address] for the given string representation, deriving the concrete
// [AddressKind] based on the host-part as defined in [types.ParseJID].
func ParseAddress(v string) Address {
	if v == "" {
		return Address{}
	}
	jid, err := types.ParseJID(v)
	if err != nil {
		return Address{}
	}
	return newAddressFromJID(jid)
}

// NewAddressFromJID returns a new [Address] from the given WhatsApp [types.JID] representation.
func newAddressFromJID(jid types.JID) Address {
	switch jid.Server {
	case types.DefaultUserServer:
		return Address{value: jid.User, kind: AddressPhoneNumber}
	case types.HiddenUserServer:
		return Address{value: jid.User, kind: AddressHidden}
	case types.GroupServer:
		return Address{value: jid.User, kind: AddressGroup}
	case types.BotServer:
		return Address{value: jid.User, kind: AddressBot}
	}
	return Address{}
}

// Kind returns the [AddressKind] for the [Address], or [AddressUnknown] if there's no valid type.
func (a Address) Kind() AddressKind {
	return a.kind
}

// IsEmpty returns true if the [Address] doesn't contain a valid value.
func (a Address) IsEmpty() bool {
	return a.value == ""
}

// ToJID returns the [Address] as a WhatsApp [types.JID] representation, or [types.EmptyJID] if the
// [AddressKind] is unknown.
func (a Address) toJID() types.JID {
	switch a.Kind() {
	case AddressPhoneNumber:
		return types.NewJID(a.value, types.DefaultUserServer)
	case AddressHidden:
		return types.NewJID(a.value, types.HiddenUserServer)
	case AddressGroup:
		return types.NewJID(a.value, types.GroupServer)
	case AddressBot:
		return types.NewJID(a.value, types.BotServer)
	}
	return types.EmptyJID
}

// Bare strips all kind information from the [Address], ensuring that any subsequent calls only
// have access to the internal value.
func (a Address) Bare() Address {
	return Address{value: a.value}
}

// String returns a string representation for the [Address]. If the [Address] is of [AddressUnknown]
// kind, then the address value is returned verbatim, otherwise the address is returned in its full
// JID form (with user and host parts).
func (a Address) String() string {
	if a.Kind() == AddressUnknown {
		return a.value
	}
	return a.toJID().String()
}

// MarshalJSON implements the [json.Marshaler] interface, and allows for encoding [Address] types
// as their underlying string representations.
func (a Address) MarshalJSON() ([]byte, error) {
	return []byte(`"` + a.String() + `"`), nil
}
