import React from "react";
import {Box,Text} from "@anthropic/ink";
import {colors} from "../theme/index.js";

/** Endpoints replace content-row gutter cells after the inner line has painted. */
export function TurnGuide({first,last,children}: {first: boolean; last: boolean; children: React.ReactNode}) {
 return <Box flexDirection="column" width="100%">
   <Box flexDirection="column" width="100%" borderStyle="single" borderLeft
     borderRight={false} borderTop={false} borderBottom={false}
     borderColor={colors.chat.turnGuide} paddingLeft={1}>{children}</Box>
   {first ? <Box position="absolute" left={0} top={0} width={1} height={1}><Text color={colors.chat.turnGuide}>╭</Text></Box> : null}
   {last ? <Box position="absolute" left={0} bottom={0} width={1} height={1}><Text color={colors.chat.turnGuide}>╰</Text></Box> : null}
 </Box>;
}
