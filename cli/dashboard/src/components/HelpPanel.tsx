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
import {helpGuide,helpKeys,providerHelpGuide} from "../ui/helpContent.js";
import {PROVIDER_CONFIGURATION_ACTIONS} from "../ui/providerConfiguration.js";
import {moduleHelpGuide,moduleHelpKeys,type HelpTopic} from "../ui/moduleHelp.js";

type Props={scene:Scene;topic?:HelpTopic;onDismiss:()=>void};

/** Both views describe the current scene; command copy and key labels retain their original authorities. */
export const HelpPanel=memo(function HelpPanel({scene,topic,onDismiss}:Props){
 const t=useCopy(),language=useContext(LanguageContext);
 const {w}=usePanelSize({widthOffset:4,minWidth:20});
 const [tab,setTab]=useState(0),[contentFocused,setContentFocused]=useState(false);
 const scroll=useRef<ScrollBoxHandle>(null);
 useScrollInput(scroll,contentFocused);
 useEffect(()=>{scroll.current?.scrollTo(0);},[scene,topic,tab]);
 useInput((input,key)=>{
  const action=standardKey(input,key);
  if(action==="back"){if(contentFocused)setContentFocused(false);else onDismiss();}
  else if(action==="left"||action==="right"){setTab(n=>Math.max(0,Math.min(1,n+(action==="left"?-1:1))));}
  else if(!contentFocused){if(action==="activate")setContentFocused(true);}
  else if(action==="next"||action==="previous")setContentFocused(false);
  else if(action==="up"||action==="down")scroll.current?.scrollBy(action==="up"?-1:1);
 });
 const guide=topic==="providers"?providerHelpGuide(t):topic?moduleHelpGuide(topic,t):helpGuide(scene,t),commands=getKeybindings(scene,language);
 const providerGuide=providerHelpGuide(t);
 return <PageFrame page={pages.help}>
  <PageHeader width={w} selected={tab} contentFocused={contentFocused}
   items={[{id:"guide",label:t({en:"Usage guide",zh:"使用说明"})},{id:"keys",label:t({en:"Key reference",zh:"按键说明"})}]}/>
  <ScrollBox ref={scroll} flexDirection="column" flexGrow={1} minHeight={0}>
   {tab===0?<Box flexDirection="column" gap={1} paddingTop={1}>
    <Text bold>{guide.title}</Text><Text>{guide.purpose}</Text>
    {guide.steps.map((step,index)=><Text key={index}>{index+1}. {step}</Text>)}
    {!topic && <Text bold>{t({en:"Commands available here",zh:"此界面可用的命令"})}</Text>}
    {!topic && commands.map(command=><Box key={command.name} flexDirection="column">
     <Text bold>{"/"+command.slashName}</Text><Text dimColor>{command.title}</Text>
    </Box>)}
    {!topic && <><Text bold>{providerGuide.title}</Text><Text>{providerGuide.purpose}</Text>
     {providerGuide.steps.map((step,index)=><Text key={index}>{index+1}. {step}</Text>)}</>}
    {(!topic||topic==="providers")&&PROVIDER_CONFIGURATION_ACTIONS.map(action=><Box key={action.id} flexDirection="column">
     <Text bold>{t(action.label)}</Text><Text dimColor>{t(action.description)}</Text>
    </Box>)}
   </Box>:<Box flexDirection="column" gap={1} paddingTop={1}>
    <Text bold>{guide.title}</Text>
    {(topic?moduleHelpKeys(topic,t):helpKeys(scene,t)).map((row,index)=><Box key={index} flexDirection="column">
     <Text bold>{row.keys}</Text><Text>{row.meaning}</Text>
    </Box>)}
   </Box>}
  </ScrollBox>
  <Box flexShrink={0} minHeight={1}><Text dimColor wrap="wrap">
   {chordLabel("leftRight")} {t({en:"tabs",zh:"页签"})} · {contentFocused?chordLabel("upDown")+" "+t({en:"scroll",zh:"滚动"}):chordLabel("enter")+" "+t({en:"browse",zh:"浏览"})} · {chordLabel("escape")} {contentFocused?t({en:"tabs",zh:"返回页签"}):t({en:"close",zh:"关闭"})}
  </Text></Box>
 </PageFrame>;
});
