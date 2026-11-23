import {useEffectiveBindings} from "../input/runtime.js";
import React, {createContext, useContext, useCallback, type ReactNode} from "react";
import {Box} from "@anthropic/ink";
import {copy, type Copy, type Language, type PageDefinition} from "./pageContract.js";
export const LanguageContext = createContext<Language>("en");
export function useCopy() {
 useEffectiveBindings();
 const language=useContext(LanguageContext);
 return useCallback((value:Copy) => copy(value,language),[language]);
}
export function PageFrame({page,width,header,footer,children}: {page:PageDefinition; width?:number;header?:ReactNode;footer?:ReactNode;children:ReactNode}) {
 const slotted=header!==undefined||footer!==undefined;
 return <Box flexDirection="column" paddingX={page.spacing.paddingX} paddingTop={page.spacing.paddingTop}
  width={width} flexGrow={1} minHeight={0}>
  {header!==undefined?<Box flexDirection="column" flexShrink={0}>{header}</Box>:null}
  {slotted?<Box flexDirection="column" flexGrow={1} minHeight={0}>{children}</Box>:children}
  {footer!==undefined?<Box flexDirection="column" flexShrink={0}>{footer}</Box>:null}
 </Box>;
}
