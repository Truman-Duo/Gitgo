import {test,expect} from "bun:test";
import {defaultProfile,defaultContextBudget,configurationJson,parseConfiguration,manualModelIds} from "./providerEditorDraft.js";
const connection={name:'API',base_url:'https://api.test/v1',preset:'custom',protocol:'openai_chat',api_key:'secret-not-exported'};
test("JSON defaults are complete, credentials are separate and unchanged roundtrips preserve existing parameters",()=>{
 const p=defaultProfile('one','openai_chat');const raw=configurationJson(connection,[p]);expect(raw).not.toContain('secret-not-exported');expect(JSON.parse(raw).models[0]).toMatchObject({context_window:512000,max_output_tokens:4096,request_preferences:{reasoning_effort:'',body:{}}});
 const parsed=parseConfiguration(raw,connection,[{...p,probe_receipt:'retain'}]);expect(parsed.profiles[0]!.probe_receipt).toBe('retain');
});
test("advanced JSON validates all profiles before returning a change and marks changed limits as user values",()=>{
 const profiles=[defaultProfile('one','openai_chat'),defaultProfile('two','openai_chat')],raw=JSON.parse(configurationJson(connection,profiles));
 raw.models[1].context_window=64000;raw.models[1].request_preferences={reasoning_effort:'high',body:{top_p:0.8}};
 expect(parseConfiguration(JSON.stringify(raw),connection,profiles).profiles[1]).toMatchObject({context_window:64000,context_source:'user',request_preferences:{reasoning_effort:'high',body:{top_p:0.8}}});
 raw.models[1].model_id='one';expect(()=>parseConfiguration(JSON.stringify(raw),connection,profiles)).toThrow('models[1].model_id');
 raw.models[1].model_id='two';raw.models[1].max_output_tokens=64000;expect(()=>parseConfiguration(JSON.stringify(raw),connection,profiles)).toThrow('models[1].max_output_tokens');
});
test("batch IDs accept CRLF and duplicates without building a second collection",()=>{
 expect(manualModelIds('one\r\ntwo\none\n\n')).toEqual(['one','two']);expect(()=>manualModelIds(' \n')).toThrow();
});

test("budget shortcuts normalize exact decimal tokens and respect a known smaller capacity",()=>{
 const profile=defaultProfile('one','openai_chat'),raw=JSON.parse(configurationJson(connection,[profile]));
 for(const [value,tokens] of [["256k",256000],["512k",512000],["1M",1000000]] as const){
  raw.models[0].context_window=value;
  expect(parseConfiguration(JSON.stringify(raw),connection,[profile]).profiles[0]!.context_window).toBe(tokens);
 }
 raw.models[0].context_window="1M";
 const small={...profile,deployment:{id:'one',name:'one',limits:{context_window:128000}}};
 expect(()=>parseConfiguration(JSON.stringify(raw),connection,[small])).toThrow('CONTEXT_BUDGET_EXCEEDS_CAPACITY');
 raw.models[0].context_window=128000;
 expect(parseConfiguration(JSON.stringify(raw),connection,[small]).profiles[0]!.context_window).toBe(128000);
 expect(defaultContextBudget(1050000)).toBe(1000000);expect(defaultContextBudget(128000)).toBe(128000);
});
