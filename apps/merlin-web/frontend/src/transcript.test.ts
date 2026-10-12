import { expect, it } from "vitest";
import { displayTranscript } from "./transcript";

it("attaches usage after content without retaining a usage row", () => {
    const input = [
        { cursor: 1, kind: "content", value: "Hello" },
        { cursor: 2, kind: "usage", value: "12 tokens (10 in · 2 out)" },
    ];
    expect(displayTranscript(input)).toEqual([
        { ...input[0], usage: input[1].value },
    ]);
    expect(input[0]).not.toHaveProperty("usage");
});
it("moves usage preceding answer text from reasoning to its answer", () => {
    const output = displayTranscript([
        { cursor: 1, kind: "thinking", value: "Checking" },
        { cursor: 2, kind: "usage", value: "12 tokens" },
        { cursor: 3, kind: "content", value: "Ready" },
    ]);
    expect(output[0]).not.toHaveProperty("usage");
    expect(output[1].usage).toBe("12 tokens");
});
it("retains separate model-call usage across tool and user boundaries", () => {
    const output = displayTranscript([
        { cursor: 1, kind: "thinking", value: "Checking" },
        { cursor: 2, kind: "usage", value: "12 tokens" },
        { cursor: 3, kind: "tool", value: "browser_snapshot started" },
        { cursor: 4, kind: "content", value: "Ready" },
        { cursor: 5, kind: "usage", value: "20 tokens" },
        { cursor: 6, kind: "user", value: "Next" },
        { cursor: 7, kind: "content", value: "Next answer" },
    ]);
    expect(output[0].usage).toBe("12 tokens");
    expect(output[2].usage).toBe("20 tokens");
    expect(output[4]).not.toHaveProperty("usage");
});
