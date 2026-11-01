import React from "react";
import {Text} from "@anthropic/ink";
import type {Feedback} from "../hooks/useFeedback.js";
import {colors} from "../theme/index.js";

export function FeedbackMessage({feedback}: {feedback?: Feedback}) {
  if (!feedback) return null;
  const color = feedback.tone === "error" ? colors.danger : feedback.tone === "warning" ? colors.warning
    : feedback.tone === "success" ? colors.success : undefined;
  return <Text color={color} wrap="wrap">{feedback.text.replace(/[\x00-\x1f\x7f-\x9f]/g, " ")}</Text>;
}
