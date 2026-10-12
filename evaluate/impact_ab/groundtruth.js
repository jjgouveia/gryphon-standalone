// Ground truth for the impact benchmark: every call site of every function declared in the repo,
// resolved by the TypeScript checker (re-exports and import aliases included), plus how well a plain
// grep for the name would have found them.
//
// usage: node groundtruth.js <repo-root> <typescript-package-dir> <out.json>
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const [root, tsDir, out] = process.argv.slice(2);
const ts = require(tsDir);
const rel = (f) => path.relative(root, f).split(path.sep).join("/");

const cfgPath = path.join(root, "tsconfig.json");
const cfg = ts.readConfigFile(cfgPath, ts.sys.readFile);
const parsed = ts.parseJsonConfigFileContent(cfg.config, ts.sys, root);
const options = { ...parsed.options, noEmit: true, skipLibCheck: true, incremental: false };
console.error(`files in program: ${parsed.fileNames.length}`);
const tracked = execFileSync("git", ["ls-files", "*.ts", "*.tsx"], { cwd: root, maxBuffer: 1 << 28 })
  .toString().split("\n").filter(Boolean).filter((f) => !f.endsWith(".d.ts")).map((f) => path.join(root, f));
const roots = [...new Set([...parsed.fileNames, ...tracked].map((f) => path.normalize(f)))];
console.error(`tracked files added: ${roots.length - parsed.fileNames.length}`);
const program = ts.createProgram(roots, options);
const checker = program.getTypeChecker();

const inRepo = (sf) =>
  !sf.isDeclarationFile && !sf.fileName.includes("node_modules") && sf.fileName.startsWith(root.split(path.sep).join("/"));

const decls = new Map(); // key -> {name,file,line,kind,exported,calls:[]}
const lineOf = (sf, node) => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;

function declInfo(sym) {
  if (!sym) return null;
  const d = sym.valueDeclaration || (sym.declarations && sym.declarations[0]);
  if (!d) return null;
  const sf = d.getSourceFile();
  if (!inRepo(sf)) return null;
  let kind = null;
  if (ts.isFunctionDeclaration(d)) kind = "function";
  else if (ts.isMethodDeclaration(d)) kind = "method";
  else if (ts.isVariableDeclaration(d) && d.initializer &&
           (ts.isArrowFunction(d.initializer) || ts.isFunctionExpression(d.initializer))) kind = "function";
  if (!kind || !d.name) return null;
  const exported = !!(ts.getCombinedModifierFlags(d) & ts.ModifierFlags.Export) ||
    (ts.isVariableDeclaration(d) && d.parent.parent && ts.getCombinedModifierFlags(d.parent.parent) & ts.ModifierFlags.Export) ||
    (kind === "method");
  return { name: d.name.getText(sf), file: rel(sf.fileName), line: lineOf(sf, d.name), kind, exported: !!exported };
}

for (const sf of program.getSourceFiles()) {
  if (!inRepo(sf)) continue;
  const visit = (node) => {
    if (ts.isCallExpression(node)) {
      let id = node.expression;
      if (ts.isPropertyAccessExpression(id)) id = id.name;
      if (ts.isIdentifier(id)) {
        let sym = checker.getSymbolAtLocation(id);
        if (sym && sym.flags & ts.SymbolFlags.Alias) { try { sym = checker.getAliasedSymbol(sym); } catch (e) { sym = null; } }
        const info = declInfo(sym);
        if (info) {
          const key = `${info.file}:${info.line}:${info.name}`;
          if (!decls.has(key)) decls.set(key, { ...info, calls: [] });
          decls.get(key).calls.push({ file: rel(sf.fileName), line: lineOf(sf, id) });
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
}
console.error(`declarations with calls: ${decls.size}`);

// A plain grep for the name over every tracked TS/TSX file, import lines and the declaration dropped.
const files = execFileSync("git", ["ls-files", "*.ts", "*.tsx"], { cwd: root, maxBuffer: 1 << 28 })
  .toString().split("\n").filter(Boolean);
const lines = new Map(); // file -> string[]
for (const f of files) lines.set(f, fs.readFileSync(path.join(root, f), "utf8").split("\n"));
const nameDecls = new Map(); // name -> count of declarations in the repo
for (const d of decls.values()) nameDecls.set(d.name, (nameDecls.get(d.name) || 0) + 1);

const result = [];
for (const d of decls.values()) {
  const calls = new Set(d.calls.map((c) => `${c.file}:${c.line}`));
  if (calls.size < 3) continue;
  const re = new RegExp(`\\b${d.name.replace(/[$]/g, "\\$")}\\b`);
  const grep = new Set();
  for (const [f, ls] of lines) {
    ls.forEach((l, i) => {
      if (!re.test(l)) return;
      if (/^\s*(import\b|export\s*\{|export\s*\*)/.test(l) || /^\s*[\w$,\s]+\}?\s*from\s+['"]/.test(l)) return;
      if (f === d.file && i + 1 === d.line) return;
      grep.add(`${f}:${i + 1}`);
    });
  }
  const hit = [...calls].filter((c) => grep.has(c)).length;
  const precision = grep.size ? hit / grep.size : 0, recall = hit / calls.size;
  const f1 = precision + recall ? (2 * precision * recall) / (precision + recall) : 0;
  const callFiles = new Set(d.calls.map((c) => c.file));
  result.push({ ...d, calls: [...new Map(d.calls.map((c) => [`${c.file}:${c.line}`, c])).values()],
    n_calls: calls.size, n_call_files: callFiles.size, same_name_decls: nameDecls.get(d.name),
    grep: { lines: grep.size, precision: +precision.toFixed(3), recall: +recall.toFixed(3), f1: +f1.toFixed(3) } });
}
fs.writeFileSync(out, JSON.stringify(result, null, 1));
console.error(`written ${result.length} candidates`);
