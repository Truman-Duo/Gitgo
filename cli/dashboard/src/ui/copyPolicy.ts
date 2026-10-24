import ts from "typescript";
const fields=new Set(["label","title","description","placeholder","statusText","help","emptyLabel"]);
const brands=new Set(["Gitgo","MiB","(contract.yaml)","· UTC"]);
/** Scan owned static UI copy; provider/user content and protocol identifiers stay literal. */
export function ownedCopyViolations(source:string,file:string):string[]{
 const tree=ts.createSourceFile(file,source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX),found=new Set<string>();
 const report=(s:string)=>{if(/[A-Za-z]{2}/.test(s)&&!brands.has(s.trim()))found.add(s.trim());};
 const literal=(n:ts.Node|undefined):string|undefined=>n&&(ts.isStringLiteral(n)||ts.isNoSubstitutionTemplateLiteral(n))?n.text:undefined;
 function visible(e:ts.Expression):void{
  const s=literal(e);if(s!==undefined){report(s);return;}
  if(ts.isTemplateExpression(e))report(e.head.text+e.templateSpans.map(x=>x.literal.text).join(" "));
  else if(ts.isConditionalExpression(e)){visible(e.whenTrue);visible(e.whenFalse);}
  else if(ts.isParenthesizedExpression(e))visible(e.expression);
  else if(ts.isBinaryExpression(e)&&[ts.SyntaxKind.PlusToken,ts.SyntaxKind.BarBarToken,ts.SyntaxKind.QuestionQuestionToken].includes(e.operatorToken.kind)){visible(e.left);visible(e.right);}
  else if(ts.isCallExpression(e)&&ts.isIdentifier(e.expression)&&e.expression.text==="field"&&e.arguments[0])visible(e.arguments[0]);
 }
 function visit(n:ts.Node):void{
  if(ts.isJsxText(n))report(n.text);
  if(ts.isJsxExpression(n)&&n.expression&&!ts.isJsxAttribute(n.parent))visible(n.expression);
  if(ts.isJsxAttribute(n)&&fields.has(n.name.getText(tree))){const s=literal(n.initializer);if(s!==undefined)report(s);}
  if(ts.isPropertyAssignment(n)){
   const name=n.name.getText(tree).replace(/["']/g,"");
   if(fields.has(name)){const s=literal(n.initializer);if(s!==undefined)report(s);}
   if(name==="en"&&ts.isObjectLiteralExpression(n.parent)){
    const zh=n.parent.properties.find(p=>ts.isPropertyAssignment(p)&&p.name.getText(tree).replace(/["']/g,"")==="zh");
    if(!zh||ts.isPropertyAssignment(zh)&&literal(zh.initializer)==="")found.add("UI_TRANSLATION_MISSING: zh");
   }
  }
  if(ts.isCallExpression(n)&&ts.isIdentifier(n.expression)&&["setStatus","setResult"].includes(n.expression.text)){
   const s=literal(n.arguments[0]);if(s!==undefined)report(s);
  }
  ts.forEachChild(n,visit);
 }
 visit(tree);return [...found].sort();
}
