import type {Copy} from "./pageContract.js";

/** Project backend error codes into a model-list observation, not a form failure.
 * Native transport may supply a code object or a "CODE: explanation" Error.
 * Do not repeat untranslated upstream explanations or expose response bodies.
 */
export function providerModelIssue(error:unknown):Copy {
 const value=error as {code?:unknown;message?:unknown}|null;
 const raw=typeof value?.code==="string"?value.code:typeof value?.message==="string"?value.message:typeof error==="string"?error:"";
 const code=raw.match(/^[A-Z][A-Z0-9_]{0,95}(?=:|$)/)?.[0] || "MODEL_LIST_UNAVAILABLE";
 if(code==="MODEL_LIST_HTTP_401")return {en:"Model list authentication failed (401); check the API Key or enter model IDs manually.",zh:"模型列表鉴权失败（401）；请核对密钥，或手填型号。"};
 if(code==="MODEL_LIST_HTTP_403")return {en:"Model list access denied (403); check account permissions or enter model IDs manually.",zh:"模型列表访问被拒绝（403）；请核对账号权限，或手填型号。"};
 if(code==="MODEL_LIST_HTTP_404"||code==="MODEL_LIST_HTTP_405")return {en:`Model list endpoint unavailable (${code.slice(-3)}); check the API address or enter model IDs manually.`,zh:`模型列表端点不可用（${code.slice(-3)}）；请核对 API 地址，或手填型号。`};
 if(code==="MODEL_LIST_TIMEOUT")return {en:"Model list request timed out; refresh later or enter model IDs manually.",zh:"模型列表请求超时；可稍后刷新或手填型号。"};
 if(code==="MODEL_LIST_TLS_FAILED")return {en:"Model list TLS verification failed; check certificates and the network route, or enter model IDs manually.",zh:"模型列表 TLS 验证失败；请检查证书与网络路线，或手填型号。"};
 if(code==="MODEL_LIST_DNS_FAILED")return {en:"Model list address could not be resolved; check the API address and network, or enter model IDs manually.",zh:"无法解析模型列表地址；请检查 API 地址与网络，或手填型号。"};
 if(code==="MODEL_LIST_INVALID")return {en:"The API returned an invalid model list; check the protocol or enter model IDs manually.",zh:"API 返回的模型列表格式无效；请核对协议，或手填型号。"};
 if(code==="INVALID_PROVIDER_CONNECTION")return {en:"Model list unavailable; check the API address and protocol, or enter model IDs manually.",zh:"暂时无法获取模型列表；请核对 API 地址和协议，或手填型号。"};
 return {en:`Model list unavailable (${code}); refresh later or enter model IDs manually.`,zh:`暂时无法获取模型列表（${code}）；可稍后刷新或手填型号。`};
}

/** Configuration rows and help share the descriptions of their actual actions. */
export const PROVIDER_CONFIGURATION_ACTIONS:{id:string;label:Copy;description:Copy}[]=[
  {id:"preset",label:{en:"Provider",zh:"模型服务商"},description:{en:"Choose the template before filling the API connection",zh:"先选择模板，再填写 API 连接"}},
  {id:"add",label:{en:"Add models",zh:"添加模型"},description:{en:"Choose multiple API candidates or paste one manual ID per line",zh:"批量选择 API 候选或粘贴每行一个手填型号"}},
  {id:"advanced",label:{en:"Advanced · configuration JSON",zh:"高级 · 配置 JSON"},description:{en:"Edit the complete API configuration and all model parameters; credentials remain separate",zh:"编辑完整 API 配置和全部模型参数；密钥单独管理"}},
];
