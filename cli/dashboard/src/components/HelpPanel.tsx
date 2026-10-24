import React, {memo,useContext,useEffect,useRef,useState} from "react";
import {Box,ScrollBox,Text,type ScrollBoxHandle} from "@anthropic/ink";
import {useManagedInput as useInput,useScrollInput} from "../input/runtime.js";
import type {Scene} from "../state/store.js";
import {standardKey,pages} from "../ui/pageContract.js";
import {LanguageContext,PageFrame,useCopy} from "../ui/PageFrame.js";
import {PageHeader} from "../ui/PageHeader.js";
import {getKeybindings} from "../keybindings.js";
import {chordLabel} from "../input/bindings.js";
import {usePanelSize} from "../theme/index.js";
import {helpGuide,helpKeys} from "../ui/helpContent.js";

type Props={scene:Scene;onDismiss:()=>void};

/** Both views describe the current scene; command copy and key labels retain their original authorities. */
export const HelpPanel=memo(function HelpPanel({scene,onDismiss}:Props){
 const t=useCopy(),language=useContext(LanguageContext);
 const {w}=usePanelSize({widthOffset:4,minWidth:20});
 const [tab,setTab]=useState(0),[contentFocused,setContentFocused]=useState(false);
 const scroll=useRef<ScrollBoxHandle>(null);
 useScrollInput(scroll);
 useEffect(()=>{scroll.current?.scrollTo(0);},[scene,tab]);
 useInput((input,key)=>{
  const action=standardKey(input,key);
  if(action==="back"){if(contentFocused)setContentFocused(false);else onDismiss();}
  else if(action==="left"||action==="right"){setTab(n=>Math.max(0,Math.min(1,n+(action==="left"?-1:1))));}
  else if(action==="activate"||action==="next")setContentFocused(n=>action==="next"?!n:true);
  else if(action==="up"||action==="down"){setContentFocused(true);scroll.current?.scrollBy(action==="up"?-1:1);}
 });
 const guide=helpGuide(scene,t),commands=getKeybindings(scene,language);
 return <PageFrame page={pages.help}>
  <PageHeader width={w} selected={tab} contentFocused={contentFocused}
   items={[{id:"guide",label:t({en:"Usage guide",zh:"使用说明"})},{id:"keys",label:t({en:"Key reference",zh:"按键说明"})}]}/>
  <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} minHeight={0}>
   {tab===0?<Box flexDirection="column" gap={1} paddingTop={1}>
    <Text bold>{guide.title}</Text><Text>{guide.purpose}</Text>
    {guide.steps.map((step,index)=><Text key={index}>{index+1}. {step}</Text>)}
    <Text bold>{t({en:"Commands available here",zh:"此界面可用的命令"})}</Text>
    {commands.map(command=><Box key={command.name} flexDirection="column">
     <Text bold>{"/"+command.slashName}</Text><Text dimColor>{command.title}</Text>
    </Box>)}
   </Box>:<Box flexDirection="column" gap={1} paddingTop={1}>
    <Text bold>{guide.title}</Text>
    {helpKeys(scene,t).map((row,index)=><Box key={index} flexDirection="column">
     <Text bold>{row.keys}</Text><Text>{row.meaning}</Text>
    </Box>)}
   </Box>}
  </ScrollBox>
  <Box flexShrink={0} minHeight={1}><Text dimColor wrap="wrap">
   {chordLabel("leftRight")} {t({en:"tabs",zh:"页签"})} · {chordLabel("upDown")} {t({en:"scroll",zh:"滚动"})} · {chordLabel("escape")} {contentFocused?t({en:"tabs",zh:"返回页签"}):t({en:"close",zh:"关闭"})}
  </Text></Box>
 </PageFrame>;
});
