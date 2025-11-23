modules_enabled = {
  -- [...]
  "http_files"; -- to serve "no upload" attachments
  "privilege"; -- for roster sync and 'legacy carbons'
}

-- in slidge's config: no-upload-path=/var/lib/slidge/attachments
http_files_dir = "/var/lib/slidge/attachments"

local _privileges = {
    roster = "both";
    message = "outgoing";
    iq = {
      ["http://jabber.org/protocol/pubsub"] = "both";
      ["http://jabber.org/protocol/pubsub#owner"] = "set";
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
