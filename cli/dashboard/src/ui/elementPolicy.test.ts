import {test, expect} from "bun:test";
import {existsSync, readFileSync} from "node:fs";
import {dirname, resolve, relative} from "node:path";
import {fileURLToPath} from "node:url";
import ts from "typescript";
import {ownedCopyViolations} from "./copyPolicy.js";

const root = fileURLToPath(new URL("../", import.meta.url));
// Follow actual production imports. Archived, unreachable panels do not gain
// a silent exemption if somebody wires them into the product again.
function productionSources() {
  const seen = new Map<string, string>();
  function visit(file: string) {
    if (seen.has(file)) return;
    const source=readFileSync(file,"utf8"); seen.set(file,source);
    const tree=ts.createSourceFile(file,source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
    ts.forEachChild(tree,node=>{
      if ((!ts.isImportDeclaration(node) && !ts.isExportDeclaration(node)) || !node.moduleSpecifier || !ts.isStringLiteral(node.moduleSpecifier)) return;
      const spec=node.moduleSpecifier.text;
      if (!spec.startsWith(".")) return;
      const base=resolve(dirname(file),spec.replace(/\.js$/, ""));
      const next=[base+".ts",base+".tsx",resolve(base,"index.ts")].find(existsSync);
      if(next?.startsWith(root))visit(next);
    });
  }
  visit(resolve(root,"main.tsx"));
  return [...seen].filter(([file])=>relative(root,file).replaceAll("\\","/").startsWith("components/"));
}

function selectionViolations(source: string, file: string) {
  const tree=ts.createSourceFile(file,source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX),problems: string[]=[];
  function visit(node: ts.Node) {
    if(ts.isPropertyAccessExpression(node) && node.getText(tree)==="colors.selection") problems.push("selection colors belong to Selection");
    if(ts.isCallExpression(node) && node.expression.getText(tree)==="useSelectionStyle") problems.push("selection state belongs to Selection");
    if(ts.isJsxAttribute(node)) {
      const name=node.name.getText(tree),value=node.initializer;
      if(["color","backgroundColor","borderColor"].includes(name) && value && ts.isStringLiteral(value)) problems.push("literal control color");
      if(name==="bold" && value && ts.isJsxExpression(value) && value.expression
        && ![ts.SyntaxKind.TrueKeyword,ts.SyntaxKind.FalseKeyword].includes(value.expression.kind)) {
        // Notice severity is semantic emphasis; it is not a selection state.
        if(!file.endsWith("RuntimeNoticeList.tsx"))problems.push("dynamic weight belongs to Selection");
      }
    }
    if(ts.isConditionalExpression(node) && [node.whenTrue,node.whenFalse].some(n=>ts.isStringLiteral(n)&&n.text==="› "))problems.push("selection rows have no leading marker");
    ts.forEachChild(node,visit);
  }
  visit(tree);return [...new Set(problems)];
}

test("every reachable product control uses shared selection, colors and bilingual copy",()=>{
  const failures: string[]=[];
  for(const [file,source] of productionSources()){
    const name=relative(root,file).replaceAll("\\","/");
    const problems=[...ownedCopyViolations(source,file),...(name==="components/Selection.tsx"?[]:selectionViolations(source,file))];
    if(problems.length)failures.push(name+": "+problems.join("; "));
  }
  expect(failures).toEqual([]);
});

test("the control gate rejects a new page even without manual registration",()=>{
  expect(selectionViolations('<Text color="#ffffff" bold={selected}>{selected ? "› " : "  "}Choice</Text>',"NewPanel.tsx"))
    .toEqual(["literal control color","dynamic weight belongs to Selection","selection rows have no leading marker"]);
  expect(ownedCopyViolations('<SelectionText>New action</SelectionText>',"NewPanel.tsx")).toContain("New action");
});
