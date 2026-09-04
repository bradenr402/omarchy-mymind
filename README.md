# omarchy-mymind

An [Omarchy](https://omarchy.org) shell plugin for [mymind](https://mymind.com).
Search your mind from a keyboard-first overlay, and save links, notes, clipboard
contents and screenshots to it without leaving your desktop.

Plugin id: `braden.mymind`

## Features

| Mode | What it does |
|------|--------------|
| **Search** | Type to search (keyword by default, `Tab` toggles semantic). Enter opens the source URL, `Ctrl+Enter` opens the item in mymind, `Ctrl+Y` copies the URL. Thumbnails are fetched lazily and cached. |
| **Save clipboard** | Detects a URL, text, or image on the clipboard and saves it as a link, note, or image. |
| **New note** | Markdown editor; `Ctrl+Enter` saves. |
| **Screenshot** | Region capture via `omarchy capture screenshot`, uploaded as an image. |
| **After saving** | Optional follow-up card: add tags, a note, or put the item in a space. Nothing is added by default — `Esc` skips. |
| **Bar widget** | Brain glyph in the bar. Left click: search. Right click: save clipboard. Middle click: new note. |

## Layout

```
manifest.json      Omarchy plugin manifest (overlay + bar-widget)
Overlay.qml        The overlay UI (all modes)
BarWidget.qml      Bar icon + IPC handler
bin/mymind         Python 3 (stdlib only) API client: JWT signing, requests, 429 back-off
bin/mymind-setup   Interactive access-key setup (writes ~/.config/mymind/credentials.json, 0600)
bin/dev-install    Copies the working tree into ~/.config/omarchy/plugins/ and restarts the shell
test/mock_api.py   Fake API server for UI testing without spending credits
```

The QML never touches the network itself; it shells out to `bin/mymind`, which
prints one JSON document per call. That keeps secrets in one place and makes
the CLI useful on its own.

## Install (local, for now)

```bash
git clone <this repo> ~/Projects/omarchy-mymind
cd ~/Projects/omarchy-mymind
bin/dev-install                                   # copies into ~/.config/omarchy/plugins/braden.mymind
omarchy plugin enable braden.mymind --section right --before omarchy.tray
```

Once published, `omarchy plugin add <git-url> --enable` will do the same.

### Access key

1. Create a key at <https://access.mymind.com/extensions>. Pick **Full access**
   (saving needs write). The secret is shown once.
2. Run `~/.config/omarchy/plugins/braden.mymind/bin/mymind-setup` and paste the
   `kid` and `secret`. It stores them in `~/.config/mymind/credentials.json`
   with mode `0600` and verifies them with a 1-credit request.

The secret never leaves the machine; it is only used to HMAC-sign short-lived
JWTs bound to each request's method and path.

### Keybindings

Add to `~/.config/hypr/bindings.lua` (adjust to taste):

```lua
o.bind("SUPER + ALT + PERIOD", "mymind: search",          "omarchy-shell shell toggle braden.mymind '{\"mode\":\"search\"}'")
o.bind("SUPER + ALT + M",      "mymind: save clipboard",  "omarchy-shell shell summon braden.mymind '{\"mode\":\"save\"}'")
o.bind("SUPER + ALT + N",      "mymind: new note",        "omarchy-shell shell summon braden.mymind '{\"mode\":\"note\"}'")
o.bind("SUPER + ALT + PRINT",  "mymind: save screenshot", "omarchy-shell shell summon braden.mymind '{\"mode\":\"screenshot\"}'")
```

### Menu entries

Add to `~/.config/omarchy/extensions/omarchy-menu.jsonc`:

```jsonc
"trigger.mymind":                {"icon":"󰧑","label":"mymind","aliases":["mymind"]},
"trigger.mymind.search":         {"icon":"","label":"Search","action":"omarchy-shell shell summon braden.mymind '{\"mode\":\"search\"}'"},
"trigger.mymind.save-clipboard": {"icon":"","label":"Save Clipboard","action":"omarchy-shell shell summon braden.mymind '{\"mode\":\"save\"}'"},
"trigger.mymind.note":           {"icon":"󰎞","label":"New Note","action":"omarchy-shell shell summon braden.mymind '{\"mode\":\"note\"}'"},
"trigger.mymind.screenshot":     {"icon":"󰄀","label":"Save Screenshot","action":"omarchy-shell shell summon braden.mymind '{\"mode\":\"screenshot\"}'"},
"setup.mymind":                  {"icon":"󰧑","label":"mymind Access Key","action":"omarchy-launch-floating-terminal-with-presentation ~/.config/omarchy/plugins/braden.mymind/bin/mymind-setup"},
```

## Summon payloads

`omarchy-shell shell summon braden.mymind '<json>'`

| Field | Values |
|-------|--------|
| `mode` | `search` (default), `save`, `note`, `screenshot` |
| `query` | Pre-fill the search box (`search` mode) |
| `text` | Pre-fill the editor (`note` mode) |
| `captureMode` | `region` (default), `windows`, `fullscreen`, `smart` (`screenshot` mode) |

IPC shortcuts via the bar widget: `omarchy-shell braden.mymind search|save|note|screenshot`.

## CLI

```
mymind check
mymind search "design tools" [--semantic] [--limit 20]
mymind save-url <url> [--title T] [--tag t]... [--note MD] [--space ID]...
mymind save-note "<markdown>"          # or via stdin
mymind save-file <path>
mymind save-clipboard
mymind screenshot [--mode region] [--delete]
mymind spaces
mymind add-tags <id> tag [tag...]
mymind add-note <id> "<markdown>"
mymind add-to-space <id> <spaceId>
mymind thumbnail <id> [--size 96x96]
mymind open <id> [--app]
```

Rate limits: after a `429` the CLI records the reset time in
`~/.local/state/omarchy-mymind/backoff.json` and refuses to call the API until
it passes (`mymind clear-backoff` overrides). Costs are surfaced in the overlay
footer.

## Development

```bash
bin/dev-install            # sync + restart shell (QML is cached; a restart is required for QML edits)
NO_RESTART=1 bin/dev-install   # sync only (enough for bin/ changes)
bin/dev-install --watch    # resync on every save
omarchy plugin validate .  # manifest/structure check
journalctl --user -f -o cat | grep -i mymind
```

To exercise the UI without spending credits, run `python3 test/mock_api.py`
and add `"apiUrl": "http://127.0.0.1:8765"` to your credentials file (any
kid/secret pair works with the mock). Remove it afterwards.

## Notes / limitations

- mymind's API content scope is not yet enforced; any key can see everything.
- Search costs 10–250 credits per query plus 1–250 for fetching the matching
  objects; the overlay debounces input by 350 ms to limit spend.
- Objects can carry many notes via the API but the mymind app currently shows
  only the first.
- Thumbnails cost 1 credit each (cached on disk in `~/.cache/omarchy-mymind`).

## License

MIT
