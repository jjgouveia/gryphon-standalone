## Summary

`from pkg import submodule [as alias]` now records an import of the submodule's file, not only of `pkg/__init__.py`.

The import statement names the package, so the parser emitted one `IMPORTS_FROM` edge, to `pkg/__init__.py`. A call `alias.func()` then stayed a bare name: the post-build resolver only links a bare call to a definition when the calling file imports the file that defines it, and no edge pointed at `submodule.py`. Effects:

- the test that calls `alias.func()` got a `TESTED_BY` edge on a bare source, which `detect-changes` does not read, so the function was reported as an untested change;
- `tests_for` returned nothing for it;
- the PR comment built on `detect-changes` showed "Tested: no" for a function with a test.

I found it from that comment on a PR of this repository: `split_findings` was flagged untested, and `test_review_ab.py` calls it as `merge_mod.split_findings(...)` after `from gryphon.eval.review_ab import merge as merge_mod`.

## The change

`_extract_imports` adds, for each name of a Python `from X import Y [as Z]`, an `IMPORTS_FROM` edge to the file `X.Y` resolves to, through the existing `_resolve_python_module_in_repo`. An edge is added only when `X.Y` resolves to a repository file, so importing a function or a class adds nothing (there is no `X/Y.py`), and relative imports (`from . import y`) are handled by the same resolver.

## Evidence

False test gaps on this repository, built from scratch with and without the change: production functions that a test node calls (exactly, or by a bare name unique among production functions) but that have no `TESTED_BY` edge on their qualified name, which is what `detect-changes` reads.

| | before | after |
|---|---|---|
| functions with a `TESTED_BY` edge | 381 | **412** |
| functions a test calls but `detect-changes` calls untested | 41 | **8** |

The 8 left have other causes. I looked at two: a script loaded with `importlib.util.spec_from_file_location` (not an import statement) and a function called at module level, outside any test.

The full suite has the same 88 failures with and without the change (Windows path identity, all present on `staging`), 4 more tests pass.

## Test plan

- [x] New tests (`TestPythonSubmoduleImports`), built with `full_build` and the post-processing pipeline: an aliased submodule call gives a qualified `TESTED_BY` edge and `get_transitive_tests` finds the test; a plain import and several names in one statement; importing a function adds only the module's own edge; a name that is not a module adds nothing. The first two fail without the change.
- [x] `ruff` clean. `mypy` reports only the two `merge.py` errors that #25 fixes, none from this change.
- [ ] CI on Linux.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
