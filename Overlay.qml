import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick
import QtQuick.Controls as QQC
import qs.Commons
import qs.Ui

// mymind overlay. One surface, several modes:
//   search  — type to search your mind, Enter opens the source URL
//   save    — save whatever is on the clipboard (URL / text / image)
//   note    — write a Markdown note and save it
//   saved   — post-save follow-up: add tags, a note, or a space
//
// Summon with: omarchy-shell shell summon bradenr402.mymind '{"mode":"search"}'

Item {
  id: root

  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null

  readonly property string pluginId: (manifest && manifest.id) || "bradenr402.mymind"
  readonly property string cli: String(Qt.resolvedUrl("bin/omarchy-mymind")).replace(/^file:\/\//, "")

  property bool opened: false
  property string mode: "search"
  property bool busy: false
  property string busyLabel: ""
  property string status: ""
  property bool statusIsError: false

  // search
  property string query: ""
  property bool semantic: false
  property int selectedIndex: 0
  property var results: []
  property var thumbnails: ({})   // id -> local path
  property var thumbQueue: []
  property string lastSearched: ""

  // save / saved
  property var clipboard: null    // probe result
  property var saved: null        // last save result
  property var spaces: []
  property bool spacesLoaded: false
  property var pendingActions: [] // queue of argv for follow-up actions

  // ---- theme tokens (shared with the menu surface) ----
  property color background: Color.menu.background
  property color foreground: Color.menu.text
  property color border: Color.menu.border
  property var borderSpec: Border.surfaceSpec("menu", "border", border, Math.max(1, Style.space(2)))
  property color scrim: Color.menu.scrim
  property color selectedBackground: Color.menu.selectedBackground
  property color selectedText: Color.menu.selectedText
  property color accent: Color.accent
  property color urgent: Color.urgent
  readonly property int cornerRadius: Style.cornerRadius
  property string fontFamily: Style.font.menuFamily
  property int contentMargin: Style.spacing.panelPadding
  property int contentSpacing: Style.spacing.md
  property int cardWidth: Math.min(Style.space(560), panel.width - Style.gapsOut * 2)
  property int cardHeight: Math.min(Style.space(520), panel.height - Style.gapsOut * 2)
  property int rowHeight: Style.space(56)
  property int thumbSize: Style.space(40)

  // ------------------------------------------------------------------ //
  // Shell contract
  // ------------------------------------------------------------------ //

  function open(payloadJson) {
    var payload = {}
    try { payload = JSON.parse(payloadJson || "{}") || {} } catch (e) { payload = {} }
    var next = String(payload.mode || "search")
    root.status = ""
    root.statusIsError = false
    root.saved = null

    root.mode = next
    root.opened = true

    if (next === "search") {
      if (payload.query !== undefined) root.setQuery(String(payload.query))
      Qt.callLater(function() { searchField.forceActiveFocus(); searchField.selectAll() })
    } else if (next === "save") {
      root.clipboard = null
      root.startSaveClipboard()
    } else if (next === "note") {
      noteArea.text = payload.text ? String(payload.text) : ""
      Qt.callLater(function() { noteArea.forceActiveFocus() })
    }
  }

  function close() {
    root.opened = false
  }

  function dismiss() {
    root.opened = false
    if (root.shell && typeof root.shell.hide === "function") root.shell.hide(root.pluginId)
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  function setStatus(text, isError) {
    root.status = text || ""
    root.statusIsError = !!isError
  }

  function parseJson(raw) {
    try { return JSON.parse(String(raw || "").trim() || "{}") } catch (e) { return { ok: false, type: "BadOutput", detail: String(raw).slice(0, 200) } }
  }

  function describeError(res) {
    if (!res) return "Unknown error"
    if (res.type === "NotConfigured") return "No access key. Run `omarchy-mymind setup --credentials` in a terminal."
    if (res.type === "RateLimited") return "Rate limited — retry in " + (res.retryAfter || "?") + "s"
    return (res.type ? res.type + ": " : "") + (res.detail || "failed")
  }

  function notify(title, body) {
    Quickshell.execDetached(["omarchy-notification-send", title, body || ""])
  }

  // ------------------------------------------------------------------ //
  // Search
  // ------------------------------------------------------------------ //

  function setQuery(text) {
    root.query = text
    searchField.text = text
    searchDebounce.restart()
  }

  function runSearch() {
    var q = root.query.trim()
    if (!q) { root.results = []; root.lastSearched = ""; return }
    if (q === root.lastSearched && !searchProc.running) return
    if (searchProc.running) { searchDebounce.restart(); return }
    var argv = [root.cli, "search", q, "--limit", "20"]
    if (root.semantic) argv.push("--semantic")
    root.lastSearched = q
    root.busy = true
    root.busyLabel = "Searching…"
    searchProc.command = argv
    searchProc.running = true
  }

  function applySearch(res) {
    root.busy = false
    if (!res.ok) { root.setStatus(root.describeError(res), true); return }
    if (res.query !== root.query.trim()) return  // stale
    root.results = res.results || []
    root.selectedIndex = 0
    root.setStatus(res.results.length + " result" + (res.results.length === 1 ? "" : "s") + (res.semantic ? " · semantic" : "") + " · " + (res.cost || 0) + " cr", false)
    root.queueThumbnails()
  }

  function queueThumbnails() {
    var q = []
    for (var i = 0; i < root.results.length; i++) {
      var r = root.results[i]
      if (r.hasThumbnail && !root.thumbnails[r.id]) q.push(r.id)
    }
    root.thumbQueue = q
    root.pumpThumbnails()
  }

  function pumpThumbnails() {
    if (thumbProc.running || root.thumbQueue.length === 0) return
    var id = root.thumbQueue[0]
    root.thumbQueue = root.thumbQueue.slice(1)
    thumbProc.currentId = id
    thumbProc.command = [root.cli, "thumbnail", id, "--size", "128x128"]
    thumbProc.running = true
  }

  function selectedResult() {
    if (root.selectedIndex < 0 || root.selectedIndex >= root.results.length) return null
    return root.results[root.selectedIndex]
  }

  function move(delta) {
    if (root.results.length === 0) return
    root.selectedIndex = (root.selectedIndex + delta + root.results.length) % root.results.length
    resultList.positionViewAtIndex(root.selectedIndex, ListView.Contain)
  }

  function openSelected(inApp) {
    var r = root.selectedResult()
    if (!r) return
    var target = (!inApp && r.url) ? r.url : r.appUrl
    root.dismiss()
    Quickshell.execDetached(["omarchy-launch-browser", target])
  }

  function copySelected() {
    var r = root.selectedResult()
    if (!r) return
    var text = r.url || r.appUrl
    Quickshell.execDetached(["wl-copy", text])
    root.setStatus("Copied " + text, false)
  }

  function toggleSemantic() {
    root.semantic = !root.semantic
    root.lastSearched = ""
    root.runSearch()
  }

  // ------------------------------------------------------------------ //
  // Saving
  // ------------------------------------------------------------------ //

  function startSaveClipboard() {
    root.busy = true
    root.busyLabel = "Reading clipboard…"
    clipboardProc.running = true
  }

  function startSaveNote() {
    var body = noteArea.text
    if (!body.trim()) { root.setStatus("Nothing to save", true); return }
    root.busy = true
    root.busyLabel = "Saving note…"
    // Note bodies go over stdin, never argv: argv is world-readable in
    // /proc/<pid>/cmdline for the lifetime of the process.
    saveProc.stdinPayload = body
    saveProc.command = [root.cli, "save-note"]
    saveProc.running = true
  }

  function applySaveResult(res) {
    root.busy = false
    if (!res.ok) {
      if (res.cancelled) { root.dismiss(); return }
      root.setStatus(root.describeError(res), true)
      return
    }
    root.saved = res
    root.mode = "saved"
    root.opened = true
    tagsField.text = ""
    followupNote.text = ""
    spaceDropdown.value = ""
    var what = res.kind === "url" ? "Link" : res.kind === "note" ? "Note" : res.kind === "image" ? "Image" : "Item"
    root.setStatus(what + (res.bumped ? " already in your mind — bumped" : " saved") + (res.cost ? " · " + res.cost + " cr" : ""), false)
    Qt.callLater(function() { tagsField.forceActiveFocus() })
  }

  function ensureSpaces() {
    if (root.spacesLoaded || spacesProc.running) return
    spacesProc.running = true
  }

  function applyFollowups() {
    if (!root.saved || !root.saved.id) return
    var id = root.saved.id
    var actions = []
    var tags = tagsField.text.split(",").map(function(t) { return t.trim() }).filter(function(t) { return t.length > 0 })
    if (tags.length) actions.push({argv: [root.cli, "add-tags", id].concat(tags)})
    if (followupNote.text.trim()) actions.push({argv: [root.cli, "add-note", id], stdin: followupNote.text})
    if (spaceDropdown.value) actions.push({argv: [root.cli, "add-to-space", id, spaceDropdown.value]})
    if (!actions.length) { root.dismiss(); return }
    root.pendingActions = actions
    root.busy = true
    root.busyLabel = "Applying…"
    root.pumpActions()
  }

  function pumpActions() {
    if (actionProc.running) return
    if (root.pendingActions.length === 0) {
      root.busy = false
      root.setStatus("Done", false)
      root.notify("mymind", "Saved" + (root.saved && root.saved.title ? ": " + root.saved.title : ""))
      root.dismiss()
      return
    }
    var action = root.pendingActions[0]
    root.pendingActions = root.pendingActions.slice(1)
    actionProc.stdinPayload = action.stdin || ""
    actionProc.command = action.argv
    actionProc.running = true
  }

  function openSaved(inApp) {
    if (!root.saved) return
    var target = (!inApp && root.saved.url) ? root.saved.url : root.saved.appUrl
    if (!target) return
    root.dismiss()
    Quickshell.execDetached(["omarchy-launch-browser", target])
  }

  // ------------------------------------------------------------------ //
  // Processes
  // ------------------------------------------------------------------ //

  Timer {
    id: searchDebounce
    interval: 350
    onTriggered: root.runSearch()
  }

  Process {
    id: searchProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applySearch(root.parseJson(text))
    }
    onExited: function(code) {
      if (code !== 0 && root.busy) root.busy = false
      // If the query changed while we were running, search again.
      if (root.query.trim() !== root.lastSearched) searchDebounce.restart()
    }
  }

  Process {
    id: thumbProc
    property string currentId: ""
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var res = root.parseJson(text)
        if (res.ok && res.path) {
          var next = {}
          for (var k in root.thumbnails) next[k] = root.thumbnails[k]
          next[thumbProc.currentId] = res.path
          root.thumbnails = next
        }
      }
    }
    onExited: root.pumpThumbnails()
  }

  Process {
    id: clipboardProc
    command: [root.cli, "clipboard"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var res = root.parseJson(text)
        root.clipboard = res
        if (!res.ok || res.kind === "empty") {
          root.busy = false
          root.setStatus(res.kind === "empty" ? "Clipboard is empty" : root.describeError(res), true)
          return
        }
        root.busyLabel = "Saving " + (res.kind === "url" ? "link" : res.kind) + "…"
        saveProc.stdinPayload = ""
        saveProc.command = [root.cli, "save-clipboard"]
        saveProc.running = true
      }
    }
  }

  Process {
    id: saveProc
    // Set before running=true; written to the child's stdin once it starts,
    // then stdin is closed so the CLI sees EOF. Empty => stdin stays closed.
    property string stdinPayload: ""
    stdinEnabled: stdinPayload.length > 0
    onStarted: {
      if (stdinPayload.length > 0) {
        write(stdinPayload)
        stdinPayload = ""   // drops the reference and closes stdin via the binding
      }
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applySaveResult(root.parseJson(text))
    }
  }

  Process {
    id: spacesProc
    command: [root.cli, "spaces"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var res = root.parseJson(text)
        if (res.ok) {
          root.spaces = res.spaces || []
          root.spacesLoaded = true
        } else {
          root.setStatus(root.describeError(res), true)
        }
      }
    }
  }

  Process {
    id: actionProc
    property string stdinPayload: ""
    stdinEnabled: stdinPayload.length > 0
    onStarted: {
      if (stdinPayload.length > 0) {
        write(stdinPayload)
        stdinPayload = ""
      }
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var res = root.parseJson(text)
        if (!res.ok) {
          root.busy = false
          root.pendingActions = []
          root.setStatus(root.describeError(res), true)
          return
        }
        root.pumpActions()
      }
    }
  }

  // ------------------------------------------------------------------ //
  // UI
  // ------------------------------------------------------------------ //

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-mymind"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle { anchors.fill: parent; color: root.scrim }
    MouseArea { anchors.fill: parent; onClicked: root.dismiss() }

    BorderSurface {
      id: card
      width: root.cardWidth
      height: root.mode === "search" ? root.cardHeight : Math.min(root.cardHeight, body.implicitHeight + header.height + footer.height + root.contentSpacing * 2 + card.contentTopInset + card.contentBottomInset)
      radius: root.cornerRadius
      anchors.centerIn: parent
      color: root.background
      borderSpec: root.borderSpec
      padding: root.contentMargin

      MouseArea { anchors.fill: parent; onClicked: {} }

      // Global keys (Esc etc.) — fields forward what they don't consume.
      Keys.priority: Keys.BeforeItem
      Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Escape) { root.dismiss(); event.accepted = true }
      }

      Column {
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        spacing: root.contentSpacing

        // ---- header ----
        Item {
          id: header
          width: parent.width
          height: Style.space(28)

          Text {
            id: headerIcon
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "󰧑"  // nf-md-brain U+F09D1
            color: root.accent
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading
          }
          Text {
            anchors.left: headerIcon.right
            anchors.leftMargin: Style.spacing.md
            anchors.right: busyText.left
            anchors.rightMargin: Style.spacing.md
            anchors.verticalCenter: parent.verticalCenter
            text: root.mode === "search" ? "mymind" : root.mode === "note" ? "New note" : root.mode === "saved" ? "Saved to mymind" : "Save to mymind"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading
            font.bold: true
            elide: Text.ElideRight
          }
          Text {
            id: busyText
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: root.busy ? root.busyLabel : (root.mode === "search" ? (root.semantic ? "semantic" : "keyword") : "")
            color: root.foreground
            opacity: 0.6
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        // ---- body ----
        Item {
          id: body
          width: parent.width
          height: parent.height - header.height - footer.height - root.contentSpacing * 2
          implicitHeight: root.mode === "search" ? 0
                        : root.mode === "note" ? noteColumn.implicitHeight
                        : root.mode === "saved" ? savedColumn.implicitHeight
                        : saveColumn.implicitHeight

          // -------- search --------
          Column {
            visible: root.mode === "search"
            anchors.fill: parent
            spacing: root.contentSpacing

            TextField {
              id: searchField
              width: parent.width
              placeholderText: "Search your mind…"
              foreground: root.foreground
              accent: root.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              onTextChanged: { if (text !== root.query) { root.query = text; searchDebounce.restart() } }
              Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Down) { root.move(1); event.accepted = true }
                else if (event.key === Qt.Key_Up) { root.move(-1); event.accepted = true }
                else if (event.key === Qt.Key_PageDown) { root.move(5); event.accepted = true }
                else if (event.key === Qt.Key_PageUp) { root.move(-5); event.accepted = true }
                else if (event.key === Qt.Key_Tab) { root.toggleSemantic(); event.accepted = true }
                else if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
                  if (event.modifiers & Qt.ControlModifier) root.openSelected(true)
                  else if (root.results.length) root.openSelected(false)
                  else root.runSearch()
                  event.accepted = true
                }
                else if (event.key === Qt.Key_Y && (event.modifiers & Qt.ControlModifier)) { root.copySelected(); event.accepted = true }
                else if (event.key === Qt.Key_N && (event.modifiers & Qt.ControlModifier)) { root.move(1); event.accepted = true }
                else if (event.key === Qt.Key_P && (event.modifiers & Qt.ControlModifier)) { root.move(-1); event.accepted = true }
              }
            }

            ListView {
              id: resultList
              width: parent.width
              height: parent.height - searchField.height - root.contentSpacing
              clip: true
              model: root.results
              spacing: Style.spacing.xs
              boundsBehavior: Flickable.StopAtBounds
              currentIndex: root.selectedIndex

              delegate: Rectangle {
                required property int index
                required property var modelData
                readonly property bool hasCursor: index === root.selectedIndex
                readonly property string thumb: root.thumbnails[modelData.id] || ""

                width: resultList.width
                height: root.rowHeight
                radius: root.cornerRadius
                color: hasCursor ? root.selectedBackground : "transparent"

                Row {
                  anchors.fill: parent
                  anchors.margins: Style.spacing.sm
                  spacing: Style.spacing.md

                  Rectangle {
                    width: root.thumbSize
                    height: root.thumbSize
                    radius: Math.max(2, root.cornerRadius / 2)
                    anchors.verticalCenter: parent.verticalCenter
                    color: Util.alpha(root.foreground, 0.08)
                    clip: true
                    Image {
                      anchors.fill: parent
                      source: thumb ? Util.fileUrl(thumb) : ""
                      fillMode: Image.PreserveAspectCrop
                      asynchronous: true
                      visible: status === Image.Ready
                    }
                    Text {
                      anchors.centerIn: parent
                      visible: !thumb
                      text: modelData.type === "note" || !modelData.url ? "\uf0f6" : modelData.type === "image" ? "\uf03e" : "\uf0c1"
                      color: hasCursor ? root.selectedText : root.foreground
                      opacity: 0.6
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.icon
                    }
                  }

                  Column {
                    width: parent.width - root.thumbSize - Style.spacing.md
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Style.spacing.xxs
                    Text {
                      width: parent.width
                      text: modelData.title || "Untitled"
                      color: hasCursor ? root.selectedText : root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                      elide: Text.ElideRight
                      textFormat: Text.PlainText
                    }
                    Text {
                      width: parent.width
                      text: [modelData.domain, (modelData.tags || []).map(function(t) { return "#" + t }).join(" "), modelData.snippet].filter(function(s) { return s }).join("  ·  ")
                      color: hasCursor ? root.selectedText : root.foreground
                      opacity: 0.65
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      elide: Text.ElideRight
                      textFormat: Text.PlainText
                    }
                  }
                }

                MouseArea {
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  acceptedButtons: Qt.LeftButton | Qt.MiddleButton
                  onContainsMouseChanged: if (containsMouse) root.selectedIndex = index
                  onClicked: function(mouse) {
                    root.selectedIndex = index
                    root.openSelected(mouse.button === Qt.MiddleButton)
                  }
                }
              }

              Text {
                anchors.centerIn: parent
                visible: root.results.length === 0 && !root.busy
                text: root.statusIsError ? "" : root.query.trim() ? (root.lastSearched === root.query.trim() ? "No matches" : "") : "Type to search · Tab toggles semantic search"
                color: root.foreground
                opacity: 0.5
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
              }
            }
          }

          // -------- save (in flight / error) --------
          Column {
            id: saveColumn
            visible: root.mode === "save"
            width: parent.width
            spacing: root.contentSpacing

            Text {
              width: parent.width
              wrapMode: Text.Wrap
              textFormat: Text.PlainText
              text: {
                var c = root.clipboard
                if (!c) return "Reading clipboard…"
                if (c.kind === "url") return c.text
                if (c.kind === "image") return "Image from clipboard (" + c.mime + ")"
                if (c.kind === "text") return (c.preview || c.text || "").slice(0, 600)
                return "Clipboard is empty"
              }
              color: root.foreground
              opacity: 0.85
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              maximumLineCount: 8
              elide: Text.ElideRight
            }
          }

          // -------- note --------
          Column {
            id: noteColumn
            visible: root.mode === "note"
            width: parent.width
            spacing: root.contentSpacing

            BorderSurface {
              width: parent.width
              height: Style.space(220)
              radius: root.cornerRadius
              color: Util.alpha(root.foreground, 0.04)
              borderSpec: Border.controlSpec(noteArea.activeFocus ? "focus" : "normal", root.foreground, root.accent)

              QQC.ScrollView {
                anchors.fill: parent
                anchors.margins: Style.spacing.sm
                QQC.TextArea {
                  id: noteArea
                  placeholderText: "Write a note in Markdown…"
                  placeholderTextColor: Qt.darker(root.foreground, 1.6)
                  color: root.foreground
                  selectionColor: Style.selectionFillFor(root.foreground, root.accent)
                  selectedTextColor: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  wrapMode: TextEdit.Wrap
                  background: null
                  Keys.onPressed: function(event) {
                    if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && (event.modifiers & Qt.ControlModifier)) {
                      root.startSaveNote(); event.accepted = true
                    }
                  }
                }
              }
            }
          }

          // -------- saved: follow-ups --------
          Column {
            id: savedColumn
            visible: root.mode === "saved"
            width: parent.width
            spacing: root.contentSpacing

            Text {
              width: parent.width
              text: root.saved ? (root.saved.title || root.saved.url || root.saved.filename || "") : ""
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              elide: Text.ElideRight
              textFormat: Text.PlainText
              visible: text !== ""
            }

            Text {
              text: "Optionally add details, then Ctrl+Enter to apply (or Esc to skip):"
              color: root.foreground
              opacity: 0.6
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            TextField {
              id: tagsField
              width: parent.width
              placeholderText: "Tags, comma separated"
              foreground: root.foreground
              accent: root.accent
              font.family: root.fontFamily
              Keys.onPressed: function(event) {
                if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) { root.applyFollowups(); event.accepted = true }
                else if (event.key === Qt.Key_Tab) { followupNote.forceActiveFocus(); event.accepted = true }
              }
            }

            BorderSurface {
              width: parent.width
              height: Style.space(90)
              radius: root.cornerRadius
              color: Util.alpha(root.foreground, 0.04)
              borderSpec: Border.controlSpec(followupNote.activeFocus ? "focus" : "normal", root.foreground, root.accent)
              QQC.ScrollView {
                anchors.fill: parent
                anchors.margins: Style.spacing.sm
                QQC.TextArea {
                  id: followupNote
                  placeholderText: "Note (Markdown)"
                  placeholderTextColor: Qt.darker(root.foreground, 1.6)
                  color: root.foreground
                  selectionColor: Style.selectionFillFor(root.foreground, root.accent)
                  selectedTextColor: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  wrapMode: TextEdit.Wrap
                  background: null
                  Keys.onPressed: function(event) {
                    if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && (event.modifiers & Qt.ControlModifier)) { root.applyFollowups(); event.accepted = true }
                    else if (event.key === Qt.Key_Tab) { spaceDropdown.forceActiveFocus(); root.ensureSpaces(); event.accepted = true }
                    else if (event.key === Qt.Key_Backtab) { tagsField.forceActiveFocus(); event.accepted = true }
                  }
                }
              }
            }

            Row {
              width: parent.width
              spacing: Style.spacing.md

              Dropdown {
                id: spaceDropdown
                width: parent.width - applyButton.width - openButton.width - Style.spacing.md * 2
                label: "Space"
                showLabel: false
                foreground: root.foreground
                background: root.background
                accent: root.accent
                fontFamily: root.fontFamily
                options: {
                  var opts = [{ value: "", label: root.spacesLoaded ? "No space" : "Space… (click to load)" }]
                  for (var i = 0; i < root.spaces.length; i++) opts.push({ value: root.spaces[i].id, label: root.spaces[i].name })
                  return opts
                }
                MouseArea {
                  anchors.fill: parent
                  propagateComposedEvents: true
                  onPressed: function(mouse) { root.ensureSpaces(); mouse.accepted = false }
                }
              }

              Button {
                id: openButton
                text: "Open"
                tooltipText: "Open the source (Ctrl+O); Ctrl+Shift+O opens in mymind"
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
                bordered: true
                onClicked: root.openSaved(false)
              }

              Button {
                id: applyButton
                text: root.busy ? "…" : "Apply"
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
                bordered: true
                selected: true
                onClicked: root.applyFollowups()
              }
            }
          }
        }

        // ---- footer ----
        Item {
          id: footer
          width: parent.width
          height: Style.space(18)

          Text {
            anchors.left: parent.left
            anchors.right: hintText.left
            anchors.rightMargin: Style.spacing.md
            anchors.verticalCenter: parent.verticalCenter
            text: root.status
            color: root.statusIsError ? root.urgent : root.foreground
            opacity: root.statusIsError ? 1 : 0.6
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
            textFormat: Text.PlainText
          }
          Text {
            id: hintText
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: root.mode === "search" ? "↵ open · ^↵ in mymind · ^Y copy · ⇥ semantic"
                : root.mode === "note" ? "^↵ save · esc cancel"
                : root.mode === "saved" ? "^↵ apply · ^O open · esc done"
                : "esc cancel"
            color: root.foreground
            opacity: 0.45
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }

      // Card-wide shortcuts that should work regardless of focused field.
      Shortcut { sequences: ["Ctrl+O"]; enabled: root.opened && root.mode === "saved"; onActivated: root.openSaved(false) }
      Shortcut { sequences: ["Ctrl+Shift+O"]; enabled: root.opened && root.mode === "saved"; onActivated: root.openSaved(true) }
    }
  }
}
