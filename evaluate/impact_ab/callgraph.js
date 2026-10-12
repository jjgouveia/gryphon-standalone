// Function-level call graph of the repo, resolved by the TypeScript checker.
// An edge is a call or a JSX render (<Foo />) of a function declared in the repo, from the
// nearest enclosing named function (or the module, for top-level code).
//
// usage: node callgraph.js <repo-root> <typescript-package-dir> <out.json>
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const [root, tsDir, out] = process.argv.slice(2);
const ts = require(tsDir);
const rel = (f) => path.relative(root, f).split(path.sep).join("/");
const rootNorm = root.split(path.sep).join("/");

const cfg = ts.readConfigFile(path.join(root, "tsconfig.json"), ts.sys.readFile);
const parsed = ts.parseJsonConfigFileContent(cfg.config, ts.sys, root);
const tracked = execFileSync("git", ["ls-files", "*.ts", "*.tsx"], { cwd: root, maxBuffer: 1 << 28 })
  .toString().split("\n").filter(Boolean).filter((f) => !f.endsWith(".d.ts")).map((f) => path.join(root, f));
const roots = [...new Set([...parsed.fileNames, ...tracked].map((f) => path.normalize(f)))];
const compilerOptions = { ...parsed.options, noEmit: true, skipLibCheck: true, incremental: false };
const program = ts.createProgram(roots, compilerOptions);
const checker = program.getTypeChecker();

const inRepo = (sf) => !sf.isDeclarationFile && !sf.fileName.includes("node_modules") && sf.fileName.startsWith(rootNorm);
const lineOf = (sf, node) => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;

const isFnInit = (init) =>
  init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init) ||
    (ts.isCallExpression(init) && init.arguments.some((a) => ts.isArrowFunction(a) || ts.isFunctionExpression(a))));

// The declaration node (with a name) that stands for a function-like declaration, or null.
function declNode(d) {
  if (ts.isFunctionDeclaration(d) && d.name) return d;
  if (ts.isMethodDeclaration(d) && d.name) return d;
  if (ts.isVariableDeclaration(d) && ts.isIdentifier(d.name) && isFnInit(d.initializer)) return d;
  return null;
}

const nodes = {}; // key -> {name,file,line,exported}
function keyOf(d) {
  const sf = d.getSourceFile();
  const name = d.name.getText(sf), file = rel(sf.fileName), line = lineOf(sf, d.name);
  const key = `${file}:${line}:${name}`;
  if (!nodes[key]) {
    const exported = !!(ts.getCombinedModifierFlags(d) & ts.ModifierFlags.Export) ||
      (ts.isVariableDeclaration(d) && d.parent.parent && !!(ts.getCombinedModifierFlags(d.parent.parent) & ts.ModifierFlags.Export)) ||
      ts.isMethodDeclaration(d);
    nodes[key] = { name, file, line, exported, kind: ts.isMethodDeclaration(d) ? "method" : "function" };
  }
  return key;
}

function enclosingKey(node, sf) {
  for (let p = node.parent; p; p = p.parent) {
    const d = declNode(p);
    if (d) return keyOf(d);
  }
  return `<module>:${rel(sf.fileName)}`;
}

// `const X = lazy(() => import('./x'))` (or next/dynamic): the component is the module's export,
// not the const, so a render of X reaches the module's component.
function lazyTarget(vd) {
  const init = vd.initializer;
  if (!init || !ts.isCallExpression(init)) return null;
  if (!/^(React\.)?lazy$|^dynamic$/.test(init.expression.getText())) return null;
  let spec = null;
  const find = (n) => {
    if (ts.isCallExpression(n) && n.expression.kind === ts.SyntaxKind.ImportKeyword &&
        n.arguments[0] && ts.isStringLiteralLike(n.arguments[0])) spec = n.arguments[0].text;
    if (!spec) ts.forEachChild(n, find);
  };
  find(init);
  if (!spec) return null;
  const res = ts.resolveModuleName(spec, vd.getSourceFile().fileName, compilerOptions, ts.sys).resolvedModule;
  if (!res) return null;
  const modSf = program.getSourceFile(res.resolvedFileName);
  const modSym = modSf && checker.getSymbolAtLocation(modSf);
  if (!modSym) return null;
  const m = /\.then\([^)]*=>\s*\(?\s*\{?\s*default:\s*\w+\.(\w+)/.exec(init.getText());
  let exp = checker.getExportsOfModule(modSym).find((e) => e.name === (m ? m[1] : "default"));
  if (!exp) return null;
  if (exp.flags & ts.SymbolFlags.Alias) { try { exp = checker.getAliasedSymbol(exp); } catch (e) { return null; } }
  const d = exp.valueDeclaration || (exp.declarations && exp.declarations[0]);
  if (!d || !inRepo(d.getSourceFile())) return null;
  const dn = declNode(d);
  return dn ? keyOf(dn) : null;
}

function targetKey(id) {
  let sym = checker.getSymbolAtLocation(id);
  if (sym && sym.flags & ts.SymbolFlags.Alias) { try { sym = checker.getAliasedSymbol(sym); } catch (e) { return null; } }
  if (!sym) return null;
  const d = sym.valueDeclaration || (sym.declarations && sym.declarations[0]);
  if (!d || !inRepo(d.getSourceFile())) return null;
  if (ts.isVariableDeclaration(d)) {
    const lz = lazyTarget(d);
    if (lz) return lz;
  }
  const dn = declNode(d);
  return dn ? keyOf(dn) : null;
}

const edges = []; // [caller, callee, file, line]
for (const sf of program.getSourceFiles()) {
  if (!inRepo(sf)) continue;
  const visit = (node) => {
    let id = null;
    if (ts.isCallExpression(node)) {
      id = node.expression;
      if (ts.isPropertyAccessExpression(id)) id = id.name;
    } else if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      id = node.tagName;
      if (ts.isPropertyAccessExpression(id)) id = id.name;
    }
    if (id && ts.isIdentifier(id)) {
      const callee = targetKey(id);
      if (callee) edges.push([enclosingKey(node, sf), callee, rel(sf.fileName), lineOf(sf, id)]);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
}
fs.writeFileSync(out, JSON.stringify({ nodes, edges }));
console.error(`nodes ${Object.keys(nodes).length}, edges ${edges.length}`);
