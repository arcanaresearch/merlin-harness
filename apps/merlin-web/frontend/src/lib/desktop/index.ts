// Browser adapter for the reused pb2 shell; native Electron controls are absent.
export type WindowButtonPosition = { x: number; y: number };
export const isDesktop = () => false;
export const getWindowButtonPosition =
    async (): Promise<WindowButtonPosition | null> => null;
export const onWindowButtonPosition =
    (_fn: (p: WindowButtonPosition | null) => void) => () => {};
export const onMenuAction = (_fn: (action: string) => void) => () => {};
export const getVersions = async () => ({ app: "0.2.0" });
