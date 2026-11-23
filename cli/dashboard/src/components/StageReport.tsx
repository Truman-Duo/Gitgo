import React,{useState} from 'react';
import {Box,Text} from '@anthropic/ink';
import {colors,geometry} from '../theme/tokens.js';
import {SelectionText} from './Selection.js';
import {DetailViewport} from './DetailViewport.js';
import {StreamingMarkdownText} from './MarkdownText.js';
import {useDisclosureAction} from '../hooks/useDisclosureAction.js';

/** Public prose remains outside the bounded detail and is never collapsed.
 * Only the leading action controls its own transient, source-bound detail. */
export function StageReport({children,detail,verbose,width,identity,expanded:controlled,onExpandedChange}: {
 children:React.ReactNode;detail?:string;verbose:boolean;width:number;identity:string;
 expanded?:boolean;onExpandedChange?:(change:React.SetStateAction<boolean>)=>void;
}){
 const [choice,setChoice]=useState<{identity:string;expanded:boolean}>();
 const expanded=controlled ?? (choice?.identity===identity?choice.expanded:true);
 const available=Boolean(verbose&&detail);
 const change=(update:React.SetStateAction<boolean>)=>onExpandedChange?onExpandedChange(update):setChoice(previous=>({identity,
   expanded:typeof update==='function'?update(previous?.identity===identity?previous.expanded:true):update}));
 const close=()=>change(false);
 const {header,toggle}=useDisclosureAction(expanded,()=>change(previous=>!previous));
 return <Box flexDirection='column' flexShrink={0} minWidth={0}>
  <Box flexDirection='row' flexShrink={0} minWidth={0}>
   {available?<Box ref={header} onClick={toggle}
     clickMode='action' flexShrink={0} width={geometry.record.indent}>
     <SelectionText dimColor>{expanded?'▾':'▸'} </SelectionText>
    </Box>:null}
   <Box flexDirection='column' flexGrow={1} minWidth={0}>{children}</Box>
  </Box>
  {available&&expanded?<Box flexDirection='column' paddingLeft={geometry.record.indent} flexShrink={0}>
   <Box flexDirection='column' minWidth={0} flexShrink={0} paddingLeft={1}
     borderStyle='single' borderLeft borderTop={false} borderRight={false} borderBottom={false} borderColor={colors.divider.color}>
     <DetailViewport framed={false} maxRows={geometry.record.thoughtMaxRows} width={Math.max(1,width-geometry.record.indent*2)} onClose={close}>
      {bodyWidth=><StreamingMarkdownText content={detail!} width={bodyWidth} dimColor/>}
     </DetailViewport>
   </Box>
   <Text color={colors.divider.color}>└─</Text>
  </Box>:null}
 </Box>;
}
