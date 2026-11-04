import React from "react";
import {Text} from "@anthropic/ink";
import {colors} from "../theme/index.js";
export type ConnectivityState="unknown"|"checking"|"online"|"offline";
/** Controlled last-check status; no polling, inference or animation ownership. */
export function ConnectivityDot({state}:{state:ConnectivityState}){
 const color=state==="online"?colors.success:state==="offline"?colors.danger:state==="checking"?colors.warning:colors.named.gray;
 return <Text color={color}>● </Text>;
}
