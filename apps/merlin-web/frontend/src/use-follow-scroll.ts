import { useEffect, useLayoutEffect, useRef, useState } from "react";

/** Follow rendered growth until the reader scrolls up; layout is not reader intent. */
export function useFollowScroll(activeKey: unknown, enabled = true) {
    const [element, setElement] = useState<HTMLDivElement | null>(null);
    const following = useRef(true);
    const previous = useRef({ top: 0, height: 0 });
    const bottom = () => {
        if (!element || !enabled) return;
        if (!following.current) {
            previous.current.height = element.scrollHeight;
            return;
        }
        element.scrollTop = element.scrollHeight;
        previous.current = {
            top: element.scrollTop,
            height: element.scrollHeight,
        };
    };
    useLayoutEffect(bottom);
    useEffect(() => {
        if (!element) return;
        let frame = 0;
        const schedule = () => {
            cancelAnimationFrame(frame);
            frame = requestAnimationFrame(bottom);
        };
        const scroll = () => {
            const nearBottom =
                element.scrollHeight -
                    element.scrollTop -
                    element.clientHeight <
                70;
            if (
                nearBottom &&
                element.scrollTop > previous.current.top &&
                element.scrollHeight === previous.current.height
            )
                following.current = true;
            else if (!nearBottom && element.scrollTop < previous.current.top)
                following.current = false;
            previous.current = {
                top: element.scrollTop,
                height: element.scrollHeight,
            };
        };
        const wheel = (event: WheelEvent) => {
            if (event.deltaY < 0) {
                following.current = false;
                cancelAnimationFrame(frame);
            }
        };
        const inspect = (event: MouseEvent) => {
            if (
                event.target instanceof Element &&
                event.target.closest("summary, .review-list")
            ) {
                following.current = false;
                cancelAnimationFrame(frame);
            }
        };
        const resized = new ResizeObserver(schedule);
        const observed = new Set<Element>();
        const observe = () => {
            for (const child of observed) {
                if (child.parentElement !== element) {
                    resized.unobserve(child);
                    observed.delete(child);
                }
            }
            for (const child of element.children) {
                if (!observed.has(child)) {
                    resized.observe(child);
                    observed.add(child);
                }
            }
            schedule();
        };
        resized.observe(element);
        const mutated = new MutationObserver(observe);
        mutated.observe(element, {
            subtree: true,
            childList: true,
            characterData: true,
            attributes: true,
            attributeFilter: ["open"],
        });
        element.addEventListener("scroll", scroll);
        element.addEventListener("wheel", wheel, { passive: true });
        element.addEventListener("click", inspect);
        observe();
        return () => {
            cancelAnimationFrame(frame);
            resized.disconnect();
            mutated.disconnect();
            element.removeEventListener("scroll", scroll);
            element.removeEventListener("wheel", wheel);
            element.removeEventListener("click", inspect);
        };
    }, [activeKey, enabled, element]);
    return setElement;
}
