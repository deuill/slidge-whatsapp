modules_enabled = {
  -- [...]
  "http_file_share"; -- for attachments with the "upload" option
  "privilege"; -- for roster sync and 'legacy carbons'
}

local _privileges = {
    roster = "both";
    message = "outgoing";
    iq = {
      ["http://jabber.org/protocol/pubsub"] = "both";
      ["http://jabber.org/protocol/pubsub#owner"] = "set";
      ["urn:xmpp:http:upload:0"] = "get";
    };
}

VirtualHost "example.org"
  -- for roster sync and 'legacy carbons'
  privileged_entities = {
    ["whatsapp.example.org"] =_privileges,
    ["other-walled-garden.example.org"] = _privileges,
    -- repeat for other slidge plugins…
  }

Component "whatsapp.example.org"
  component_secret = "secret"
  modules_enabled = {"privilege"}

Component "other-walled-garden.example.org"
  component_secret = "some-other-secret"
  modules_enabled = {"privilege"}

-- for attachments with the "upload" option
-- in whatsapp's config: upload-service=upload.example.org
Component "upload.example.org" "http_file_share"
    server_user_role = "prosody:registered"
    -- alternatively, you can be more specific with:
    -- http_file_share_access = { "whatsapp.example.org", "other-walled-garden.example.org" }
