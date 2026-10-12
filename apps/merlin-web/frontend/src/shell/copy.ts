/**
 * Every user-visible string the shell says.
 *
 * One vocabulary across the titlebar, sidebar, statusbar, boot chain and
 * command palette — the audit found six different words for the same
 * connection ("Ready", "Paired", "paired", "Connected", "Not connected", "Not
 * paired"). The word "pair" is gone from user-facing text entirely: it names an
 * implementation handshake, not something a person does.
 *
 * House rules (see `@/lib/copy`): "scanner engine", never runner/live store;
 * "you"/"this account", never operator; sentence case in prose; no ids, hashes
 * or file paths outside a Details disclosure or a Copy action.
 */


export const SHELL_COPY = {
  appName: 'Merlin',

  // Actions — the same label wherever the action appears (row, menu, palette).
  newChat: 'New session',
  newScan: 'New scan',
  toggleSidebar: 'Toggle sidebar',
  commandPalette: 'Command palette',
  settings: 'Settings',
  switchTheme: 'Switch theme',
  restartEngine: 'Restart engine',
  showLogs: 'Show logs',
  quit: 'Quit',
  reloadView: 'Reload view',
  close: 'Close',

  // Boot chain.
  bootTitle: 'Starting the scanner engine…',
  bootDetail: 'This takes a few seconds the first time.',
  engineStoppedDetail: 'Restart it to keep scanning.',
  engineFailedFallback: 'The scanner engine could not start',
  engineFailedDetail: 'Restart it, or open the logs to see what it reported.',
  crashTitle: 'Something went wrong',
  crashDetail: 'This part of the app stopped responding. Reloading it usually fixes it.',
  detailsLabel: 'Details',

  // Sidebar.
  chatsEmpty: 'No chats yet',
  sidebarNavLabel: 'Main',

  // The notification a finished scan produces. (The saved-report toast belongs
  // to the scans area, which can correlate it with the export it awaited.)
  scanFinishedTitle: 'Scan finished',
  // Confirmation for the sidebar row's "Copy scan id". Same wording as the
  // scans area's own DETAIL_COPY.copiedScanId — if that module ever exports
  // its constants, consume them instead of keeping two copies.
  scanIdCopied: 'Scan id copied',
  scanIdCopyFailed: 'Couldn’t copy the scan id',
  aboutItemLabel: 'About Merlin',

  // Palette.
  palettePlaceholder: 'Search actions, screens and scans…',
  paletteEmpty: 'Nothing matches that',
  paletteGroupActions: 'Actions',
  paletteGroupGoTo: 'Go to',
  paletteGroupScans: 'Recent scans',
  paletteGroupChats: 'Recent chats',
  paletteHintSelect: 'to open',
  paletteHintMove: 'to move',
  paletteHintClose: 'to close',
} as const
