import { describe, expect, test } from "bun:test";
import type { CoreEvent, RuntimeStateEvent } from "../src/core";

describe("public core type exports", () => {
  test("RuntimeStateEvent is the canonical event type exported with CoreEvent compatibility", () => {
    const event: RuntimeStateEvent = {
      id: "evt-1",
      sequence: 1,
      timestamp: "2026-05-27T18:42:10.000Z",
      type: "chatMessageStarted",
      data: { role: "assistant" },
    };

    const compatible: CoreEvent = event;
    expect(compatible.type).toBe("chatMessageStarted");
  });
});
