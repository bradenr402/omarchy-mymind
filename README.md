# omarchy-mymind

An [Omarchy](https://omarchy.org) shell plugin for [mymind](https://mymind.com).
Search your mind from a keyboard-first overlay, and save links, notes and
clipboard contents to it without leaving your desktop.

Plugin id: `bradenr402.mymind`

## Features

| Mode | What it does |
|------|--------------|
| **Search** | Type to search (keyword by default, `Tab` toggles semantic). Enter opens the source URL, `Ctrl+Enter` opens the item in mymind, `Ctrl+Y` copies the URL. Thumbnails are fetched lazily and cached. |
| **Save clipboard** | Detects a URL, text, or image on the clipboard and saves it as a link, note, or image. For screenshots: press `PRINT` (Omarchy copies the capture to the clipboard), then save the clipboard. |
| **New note** | Markdown editor; `Ctrl+Enter` saves. |
| **After saving** | Optional follow-up card: add tags, a note, or put the item in a space. Nothing is added by default — `Esc` skips. |
| **Bar widget** | Brain glyph in the bar. Left click: search. Right click: save clipboard. Middle click: new note. |

## Layout

```
manifest.json      Omarchy plugin manifest (overlay + bar-widget)
Overlay.qml        The overlay UI (all modes)
BarWidget.qml      Bar icon + IPC handler
bin/mymind         Python 3 (stdlib only) API client: JWT signing, requests, 429 back-off
bin/setup          One-shot setup: PATH symlinks, access key, keybindings, menu entries
bin/mymind-setup   Access-key prompt only (writes ~/.config/mymind/credentials.json, 0600)
bin/dev-install    Copies the working tree into ~/.config/omarchy/plugins/ and restarts the shell
test/mock_api.py   Fake API server for UI testing without spending credits
```

The QML never touches the network itself; it shells out to `bin/mymind`, which
prints one JSON document per call. That keeps secrets in one place and makes
the CLI useful on its own.

## Install

```bash
omarchy plugin add https://github.com/bradenr402/omarchy-mymind.git --enable
~/.config/omarchy/plugins/bradenr402.mymind/bin/setup
```

`setup` walks you through the rest, asking before each step:

1. Puts the `mymind` and `mymind-setup` commands on your PATH (`~/.local/bin`).
2. Stores your access key (see below) if you haven't already.
3. Adds keybindings to `~/.config/hypr/bindings.lua`:
   `Super+Alt+.` search, `Super+Alt+M` save clipboard, `Super+Alt+N` new note.
   Skipped if any of those keys is already bound.
4. Adds *Trigger > mymind* and *Setup > mymind Access Key* to the Omarchy menu.
5. Enables the plugin with the bar widget in the right section.

Config edits live between `omarchy-mymind` marker comments; re-running `setup`
refreshes them and `setup --uninstall` removes them. `setup --yes` skips the
prompts. Nothing outside the markers is touched, and no step needs sudo.

Requirements: Omarchy 4 (Quattro), `python3` (stdlib only), `wl-clipboard`,
and `gum` for the interactive prompts. The plugin talks only to
`api.mymind.com`, using an access key you supply.

### Access key

Create a key at <https://access.mymind.com/extensions> with **Full access**
(saving needs write); the secret is shown once. `setup` (or `mymind-setup` on
its own) stores it in `~/.config/mymind/credentials.json` with mode `0600` and
verifies it with a 1-credit request.

The secret never leaves the machine; it is only used to HMAC-sign short-lived
JWTs bound to each request's method and path.

Prefer different keys or menu placement? Run `setup`, decline steps 3–4, and
copy what you want from `bin/setup` (`bindings_block` / `menu_block`).

## Summon payloads

`omarchy-shell shell summon bradenr402.mymind '<json>'`

| Field | Values |
|-------|--------|
| `mode` | `search` (default), `save`, `note` |
| `query` | Pre-fill the search box (`search` mode) |
| `text` | Pre-fill the editor (`note` mode) |

IPC shortcuts via the bar widget: `omarchy-shell bradenr402.mymind search|save|note`.

## CLI

```
mymind check
mymind search "design tools" [--semantic] [--limit 20]
mymind save-url <url> [--title T] [--tag t]... [--note MD] [--space ID]...
mymind save-note "<markdown>"          # or via stdin
mymind save-file <path>
mymind save-clipboard
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

## Remove

```bash
~/.config/omarchy/plugins/bradenr402.mymind/bin/setup --uninstall
omarchy plugin remove bradenr402.mymind
```

The first line removes the CLI symlinks, keybindings and menu entries; the
second removes the plugin. Your access key and caches are left in place so a
reinstall keeps working. For a clean slate:

```bash
rm -rf ~/.config/mymind ~/.cache/omarchy-mymind ~/.local/state/omarchy-mymind
```

## Development

```bash
git clone https://github.com/bradenr402/omarchy-mymind.git ~/Projects/omarchy-mymind
cd ~/Projects/omarchy-mymind
bin/dev-install
~/.config/omarchy/plugins/bradenr402.mymind/bin/setup
```

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
