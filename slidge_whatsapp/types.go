package whatsapp

import (
	// Standard library.
	"strings"

	// Third-party libraries.
	"go.mau.fi/whatsmeow/types"
)

// TODO
type Address string

// TODO
type AddressKind int

// TODO
const (
	AddressUnknown AddressKind = iota
	AddressPhoneNumber
	AddressHidden
	AddressGroup
	AddressBot
)

// TODO
func (k AddressKind) Prefix() string {
	switch k {
	case AddressPhoneNumber:
		return "+"
	case AddressHidden:
		return "-"
	case AddressGroup:
		return "#"
	case AddressBot:
		return "="
	}
	return ""
}

// TODO
func (a Address) Kind() AddressKind {
	if a == "" {
		return AddressUnknown
	}
	switch string(a[0]) {
	case AddressPhoneNumber.Prefix():
		return AddressPhoneNumber
	case AddressHidden.Prefix():
		return AddressHidden
	case AddressGroup.Prefix():
		return AddressGroup
	case AddressBot.Prefix():
		return AddressBot
	}
	return AddressUnknown
}

// TODO
func (a Address) String() string {
	return string(a)
}

// TODO
func (a Address) toJID() types.JID {
	user := strings.TrimPrefix(string(a), a.Kind().Prefix())
	switch a.Kind() {
	case AddressPhoneNumber:
		return types.NewJID(user, types.DefaultUserServer)
	case AddressHidden:
		return types.NewJID(user, types.HiddenUserServer)
	case AddressGroup:
		return types.NewJID(user, types.GroupServer)
	case AddressBot:
		return types.NewJID(user, types.BotServer)
	}
	return types.EmptyJID
}

// TODO
func addressFromJID(jid types.JID) Address {
	switch jid.Server {
	case types.DefaultUserServer:
		return Address(AddressPhoneNumber.Prefix() + jid.User)
	case types.HiddenUserServer:
		return Address(AddressHidden.Prefix() + jid.User)
	case types.GroupServer:
		return Address(AddressGroup.Prefix() + jid.User)
	case types.BotServer:
		return Address(AddressBot.Prefix() + jid.User)
	}
	return ""
}

// Actor identifies the source of an event, typically either a [Contact] or [GroupParticipant].
type Actor struct {
	Address Address
	IsSelf  bool
}
