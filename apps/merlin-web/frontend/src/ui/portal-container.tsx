import * as React from 'react'

/**
 * The DOM node that layered surfaces (menus, tooltips) should portal INTO.
 *
 * By default Radix portals every menu and tooltip to `document.body`. Inside a
 * modal Dialog that is wrong twice over: the menu lands outside the dialog's
 * DOM, so the dialog's own outside-interaction logic treats clicking your own
 * menu as clicking away and closes underneath you; and the menu escapes the
 * dialog's stacking context.
 *
 * `DialogContent` publishes its node here, so anything rendered inside a dialog
 * portals into the dialog. Outside a dialog the value is `null` and Radix keeps
 * its `document.body` default.
 */
const PortalContainerContext = React.createContext<HTMLElement | null>(null)

export function PortalContainerProvider({ container, children }: { container: HTMLElement | null; children: React.ReactNode }) {
  return <PortalContainerContext.Provider value={container}>{children}</PortalContainerContext.Provider>
}

/** The nearest layered-surface host, or `null` for "use the document body". */
export function usePortalContainer(): HTMLElement | null {
  return React.useContext(PortalContainerContext)
}
