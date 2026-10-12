import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useFollowScroll } from "./use-follow-scroll";

let resize: () => void;
let frame: FrameRequestCallback | undefined;
beforeEach(() => {
    vi.stubGlobal(
        "ResizeObserver",
        class {
            constructor(callback: () => void) {
                resize = callback;
            }
            observe() {}
            unobserve() {}
            disconnect() {}
        },
    );
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
        frame = callback;
        return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", () => {
        frame = undefined;
    });
});
afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
});

function Thread({ value }: { value: string }) {
    const ref = useFollowScroll("Chat");
    return (
        <div ref={ref} data-testid="thread">
            <div>{value}</div>
        </div>
    );
}
it("follows streaming and delayed layout, preserves scrollback, and follows again at bottom", () => {
    const { getByTestId, rerender } = render(<Thread value="Thinking" />);
    const element = getByTestId("thread");
    let height = 1000;
    let top = 0;
    Object.defineProperties(element, {
        scrollHeight: { get: () => height },
        clientHeight: { get: () => 300 },
        scrollTop: {
            get: () => top,
            set: (value: number) => {
                top = Math.min(value, height - 300);
            },
        },
    });
    rerender(<Thread value="Thinking more" />);
    expect(top).toBe(700);
    height = 1400;
    resize();
    frame?.(0);
    expect(top).toBe(1100);
    element.scrollTop = 500;
    fireEvent.scroll(element);
    height = 1800;
    rerender(<Thread value="Still thinking" />);
    resize();
    frame?.(0);
    expect(top).toBe(500);
    element.scrollTop = 1500;
    fireEvent.scroll(element);
    height = 2100;
    rerender(<Thread value="More thinking" />);
    expect(top).toBe(1800);
});
it("does not interpret browser scroll anchoring during growth as manual scrollback", () => {
    const { getByTestId, rerender } = render(<Thread value="Thinking" />);
    const element = getByTestId("thread");
    let height = 1000;
    let top = 0;
    Object.defineProperties(element, {
        scrollHeight: { get: () => height },
        clientHeight: { get: () => 300 },
        scrollTop: {
            get: () => top,
            set: (value: number) => {
                top = Math.min(value, height - 300);
            },
        },
    });
    rerender(<Thread value="Thinking more" />);
    height = 1500;
    element.scrollTop = 850;
    fireEvent.scroll(element);
    resize();
    frame?.(0);
    expect(element.scrollTop).toBe(1200);
});

it("pauses immediately for upward wheel input racing with streamed growth", () => {
    const { getByTestId, rerender } = render(<Thread value="Thinking" />);
    const element = getByTestId("thread");
    let height = 1000;
    let top = 0;
    Object.defineProperties(element, {
        scrollHeight: { get: () => height },
        clientHeight: { get: () => 300 },
        scrollTop: {
            get: () => top,
            set: (value: number) => {
                top = Math.min(value, height - 300);
            },
        },
    });
    rerender(<Thread value="More" />);
    fireEvent.wheel(element, { deltaY: -100 });
    height = 1300;
    element.scrollTop = 950;
    fireEvent.scroll(element);
    element.scrollTop = 900;
    fireEvent.scroll(element);
    rerender(<Thread value="Still streaming" />);
    expect(element.scrollTop).toBe(900);
});
it("does not resume following when a completed thinking block shrinks the layout", () => {
    const { getByTestId, rerender } = render(<Thread value="Thinking" />);
    const element = getByTestId("thread");
    let height = 1000;
    let top = 0;
    Object.defineProperties(element, {
        scrollHeight: { get: () => height },
        clientHeight: { get: () => 300 },
        scrollTop: {
            get: () => top,
            set: (value: number) => {
                top = Math.min(value, height - 300);
            },
        },
    });
    rerender(<Thread value="More" />);
    element.scrollTop = 400;
    fireEvent.scroll(element);
    height = 500;
    element.scrollTop = 200;
    rerender(<Thread value="Completed thinking" />);
    fireEvent.scroll(element);
    height = 1200;
    rerender(<Thread value="New thinking" />);
    expect(top).toBe(200);
    element.scrollTop = 900;
    fireEvent.scroll(element);
    height = 1500;
    rerender(<Thread value="Following again" />);
    expect(top).toBe(1200);
});
it("attaches scroll listeners when the chat mounts after initial loading", () => {
    function LoadingThread({ loaded }: { loaded: boolean }) {
        const ref = useFollowScroll("Chat");
        return loaded ? (
            <div ref={ref} data-testid="late-thread" />
        ) : (
            <p>Loading</p>
        );
    }
    const { getByTestId, rerender } = render(<LoadingThread loaded={false} />);
    rerender(<LoadingThread loaded />);
    const element = getByTestId("late-thread");
    let height = 1000;
    let top = 0;
    Object.defineProperties(element, {
        scrollHeight: { get: () => height },
        clientHeight: { get: () => 300 },
        scrollTop: {
            get: () => top,
            set: (value: number) => {
                top = Math.min(value, height - 300);
            },
        },
    });
    rerender(<LoadingThread loaded />);
    element.scrollTop = 300;
    fireEvent.scroll(element);
    height = 1500;
    rerender(<LoadingThread loaded />);
    expect(top).toBe(300);
});

it("preserves position when the reader opens details instead of following their expansion", () => {
    const { getByTestId, rerender } = render(<Thread value="Ready" />);
    const element = getByTestId("thread");
    let height = 1000,
        top = 700;
    Object.defineProperties(element, {
        scrollHeight: { get: () => height },
        clientHeight: { get: () => 300 },
        scrollTop: {
            get: () => top,
            set: (value: number) => {
                top = Math.min(value, height - 300);
            },
        },
    });
    rerender(<Thread value="Ready" />);
    const summary = document.createElement("summary");
    element.append(summary);
    fireEvent.click(summary);
    height = 2000;
    rerender(<Thread value="Expanded review" />);
    resize();
    frame?.(0);
    expect(top).toBe(700);
});
