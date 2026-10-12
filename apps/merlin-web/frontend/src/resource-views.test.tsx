import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import {
    StructuredFields,
    viewRows,
    savedProviderModels,
    recordSummary,
    pageGuide,
} from "./resource-views";
afterEach(cleanup);
describe("operator resources", () => {
    it("includes the default provider and deduplicates only the same model and endpoint", () => {
        const setup = {
            provider: {
                model: "deepseek",
                base_url: "https://deepseek.test",
                key_saved: true,
            },
            model_endpoints: [
                { model: "claude", base_url: "https://anthropic.test" },
                { model: "deepseek", base_url: "https://deepseek.test" },
                { model: "deepseek", base_url: "https://other.test" },
            ],
        };
        const saved = savedProviderModels(setup);
        expect(saved).toHaveLength(3);
        expect(saved[0]).toMatchObject({
            model: "deepseek",
            is_default: true,
            key_saved: true,
        });
    });
    it("unwraps actual doctor, lessons and conversation response shapes", () => {
        expect(
            viewRows("Diagnostics", [
                false,
                [{ name: "storage", status: "failed" }],
            ]),
        ).toEqual([{ name: "storage", status: "failed" }]);
        expect(
            viewRows("Lessons", {
                counts: { total: 1 },
                lessons: [{ id: "lesson" }],
            }),
        ).toEqual([{ id: "lesson" }]);
        expect(
            viewRows("Context", {
                messages: [{ role: "user", content: "Hi" }],
                compactions: [{ summary: "Earlier work" }],
            }),
        ).toHaveLength(2);
    });
    it("gives approval and memory records meaningful titles", () => {
        expect(
            recordSummary(
                "Permissions",
                {
                    tool: "shell_command",
                    reason: "Directory requires approval",
                },
                0,
            ),
        ).toEqual({
            title: "Shell command",
            description: "Directory requires approval",
        });
        expect(
            recordSummary(
                "Memory",
                { record_id: "memory/123", applicability: "Support bot" },
                0,
            ).title,
        ).toBe("Memory/123");
        expect(pageGuide("Diagnostics")).toContain("Health checks");
    });
    it("renders labeled fields with bounded long text and expandable collections", () => {
        render(
            <StructuredFields
                value={{
                    allowed_origins: Array.from(
                        { length: 12 },
                        (_, i) => `https://test${i}.local`,
                    ),
                    enabled: false,
                    content: "x".repeat(700),
                }}
            />,
        );
        expect(screen.getByText("Allowed origins")).toBeTruthy();
        expect(screen.getByText("No")).toBeTruthy();
        expect(screen.queryByText("https://test11.local")).toBeNull();
        fireEvent.click(screen.getByText("Show all 12"));
        expect(screen.getByText("https://test11.local")).toBeTruthy();
        fireEvent.click(screen.getByText("Read full text"));
        expect(screen.getByText("x".repeat(700))).toBeTruthy();
        expect(document.querySelector("pre")).toBeNull();
    });
});
