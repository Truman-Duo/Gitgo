import {createHash} from "node:crypto";
import {readdirSync} from "node:fs";
import {join,relative} from "node:path";
import {fileURLToPath} from "node:url";
import {ownedCopyViolations} from "./copyPolicy.js";
import {test,expect} from "bun:test";
import ts from "typescript";
import {readFileSync} from "node:fs";
import {pages} from "./pageContract.js";
const names=["DecisionPanel","StatsOverview","StatsPanel","UpdatePanel","BtwPanel","RuntimeMenu","RuntimeShell","RuntimeContextPanel","RuntimeToolsPanel","GovernancePanel","MemoryPanel","ContractTab","LessonsPanel","LessonsTab","PublishPanel","HelpPanel"];
const allowed=new Set(["· UTC","Gitgo","MiB","(contract.yaml)"]);
test("standard pages keep spacing, supported composition and bilingual static markup",()=>{
 for(const page of Object.values(pages)){
  expect(page.spacing).toEqual({paddingX:1,paddingTop:1});
  expect(page.title.en.trim().length).toBeGreaterThan(0);expect(page.title.zh.trim().length).toBeGreaterThan(0);
 }
 const violations:string[]=[];
 for(const name of names){
  const source=readFileSync(new URL("../components/"+name+".tsx",import.meta.url),"utf8");
  const tree=ts.createSourceFile(name,source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  function visit(node:ts.Node){
   // Also guard visible fallback/conditional text, which JSX-text-only scans miss.
   if(ts.isJsxExpression(node)&&node.expression&&!ts.isJsxAttribute(node.parent)){
    function visible(expr:ts.Expression):void {
     if(ts.isStringLiteral(expr)||ts.isNoSubstitutionTemplateLiteral(expr)){
      if(/[A-Za-z]{2}/.test(expr.text)&&!allowed.has(expr.text))
       violations.push(name+":"+(tree.getLineAndCharacterOfPosition(expr.pos).line+1)+" requires bilingual expression: "+expr.text);
     }else if(ts.isConditionalExpression(expr)){visible(expr.whenTrue);visible(expr.whenFalse);}
     else if(ts.isParenthesizedExpression(expr))visible(expr.expression);
     else if(ts.isBinaryExpression(expr)&&[ts.SyntaxKind.PlusToken,ts.SyntaxKind.BarBarToken,ts.SyntaxKind.QuestionQuestionToken].includes(expr.operatorToken.kind)){
      visible(expr.left);visible(expr.right);
     }else if(ts.isCallExpression(expr)&&ts.isIdentifier(expr.expression)&&expr.expression.text==="field"&&expr.arguments[0])visible(expr.arguments[0]);
    }
    visible(node.expression);
   }
   if(ts.isJsxText(node)){
    const text=node.text.trim();
    if(/[A-Za-z]{2}/.test(text)&&!allowed.has(text)){
     const line=tree.getLineAndCharacterOfPosition(node.pos).line+1;
     violations.push(name+":"+line+" requires bilingual copy: "+text);
    }
   }
   ts.forEachChild(node,visit);
  }
  visit(tree);
 }
 expect(violations).toEqual([]);
});

test("all component files reject newly introduced untranslated copy",()=>{
 const baseline=JSON.parse(readFileSync(new URL("./legacyCopyBaseline.json",import.meta.url),"utf8")) as Record<string,{source_sha256:string;owned_literals:string[]}>;
 const directory=fileURLToPath(new URL("../components/",import.meta.url)),failures:string[]=[];
 for(const file of componentSources(directory)){
  const key=relative(directory,file).replaceAll(String.fromCharCode(92),"/");
  const source=readFileSync(file,"utf8"),messages=ownedCopyViolations(source,file);
  const old=baseline[key],digest=createHash("sha256").update(source.replaceAll(String.fromCharCode(13,10),String.fromCharCode(10))).digest("hex");
  if(messages.length&&(!old||old.source_sha256!==digest))
   failures.push(key+": finish bilingual copy for this new or modified page: "+messages.join("; "));
 }
 expect(failures).toEqual([]);
});
test("new markup, field labels and missing Chinese need no manual page registration",()=>{
 expect(ownedCopyViolations('<Text>New page</Text>;<Editor placeholder="Enter a name"/>;const fields=[{label:"Output folder"}];',"new.tsx"))
  .toEqual(["Enter a name","New page","Output folder"]);
 expect(ownedCopyViolations('const label={en:"Only English"};',"new.tsx")).toContain("UI_TRANSLATION_MISSING: zh");
 expect(ownedCopyViolations('<Text>{t({en:"Ready",zh:"已就绪"})}{providerText}</Text>',"new.tsx")).toEqual([]);
});
test("command descriptions and usage guidance outside components require bilingual copy",()=>{
 for(const path of ["../keybindings.ts","./helpContent.ts"]){
  const source=readFileSync(new URL(path,import.meta.url),"utf8");
  expect(ownedCopyViolations(source,path)).toEqual([]);
 }
});
function componentSources(root:string):string[]{
 return readdirSync(root,{withFileTypes:true}).flatMap(e=>e.isDirectory()?componentSources(join(root,e.name)):
  /\.tsx?$/.test(e.name)&&!e.name.includes(".test.")?[join(root,e.name)]:[]);
}
