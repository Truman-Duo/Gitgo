import React from "react";
import {Text} from "@anthropic/ink";
import {colors} from "../theme/index.js";

export type InlineNoticeValue={text:string;tone:"info"|"warning"};

/** A current, owner-controlled observation, not a transient operation toast.
 * The caller clears/replaces it with the observed identity or result. This
 * renderer owns no timers, input, focus, navigation or business state.
 */
export function InlineNotice({notice,wrap="truncate-end"}:{notice?:InlineNoticeValue;wrap?:"truncate-end"|"wrap"}) {
 if(!notice)return null;
 return <Text color={notice.tone==="warning"?colors.warning:undefined} dimColor={notice.tone==="info"} wrap={wrap}>{notice.text}</Text>;
}
