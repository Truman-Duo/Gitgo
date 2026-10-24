import React, {createContext, useContext, useCallback, type ReactNode} from "react";
import {Box} from "@anthropic/ink";
import {copy, type Copy, type Language, type PageDefinition} from "./pageContract.js";
export const LanguageContext = createContext<Language>("en");
export function useCopy() {
 const language=useContext(LanguageContext);
 return useCallback((value:Copy) => copy(value,language),[language]);
}
export function PageFrame({page,width,children}: {page:PageDefinition; width?:number;children:ReactNode}) {
 return <Box flexDirection="column" paddingX={page.spacing.paddingX} paddingTop={page.spacing.paddingTop}
  width={width} flexGrow={1} minHeight={0}>{children}</Box>;
}
