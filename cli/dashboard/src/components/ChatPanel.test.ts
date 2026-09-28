import {describe, expect, test} from "bun:test";
import type {ChatMessage} from "../types.js";
import {visibleConversationMessages} from "./ChatPanel.js";

describe("visibleConversationMessages", () => {
  test("shows knowledge completion without exposing internal system steering", () => {
    const messages = [
      {role: "user", content: "task", timestamp: "1"},
      {role: "system", content: "internal governance", timestamp: "2"},
      {
        role: "system",
        kind: "knowledge_notice",
        content: "Knowledge review complete · no new lesson added.",
        timestamp: "3",
      },
    ] as ChatMessage[];

    expect(visibleConversationMessages(messages).map(message => message.content)).toEqual([
      "task",
      "Knowledge review complete · no new lesson added.",
    ]);
  });
});
