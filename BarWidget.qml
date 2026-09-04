import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Bar entry for mymind.
//   left click   → toggle the search overlay
//   right click  → save the clipboard
//   middle click → new note
BarWidget {
  id: root
  moduleName: "braden.mymind"

  readonly property string pluginId: "braden.mymind"

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function summon(mode) {
    if (!root.bar) return
    root.bar.run("omarchy-shell shell summon " + root.pluginId + " '" + JSON.stringify({ mode: mode }) + "'")
  }

  function toggleSearch() {
    if (!root.bar) return
    root.bar.run("omarchy-shell shell toggle " + root.pluginId + " '{\"mode\":\"search\"}'")
  }

  IpcHandler {
    target: "braden.mymind"

    function search(): void { root.toggleSearch() }
    function save(): void { root.summon("save") }
    function note(): void { root.summon("note") }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰧑"  // nf-md-brain U+F09D1
    tooltipText: "mymind — click: search · right: save clipboard · middle: new note"
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.RightButton) root.summon("save")
      else if (mouseButton === Qt.MiddleButton) root.summon("note")
      else root.toggleSearch()
    }
  }
}
