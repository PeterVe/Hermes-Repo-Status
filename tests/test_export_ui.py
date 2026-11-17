"""Execute the plain-ESM export UI in Node with minimal SDK/React adapters."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
let src = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/import\s*\{[\s\S]*?\}\s*from\s*'[^']+'\s*/g, '')
  .replace('export default {', 'const plugin = {');
let states = [], refs = [], cursor = 0, refCursor = 0, effects = [];
let status = {}, listing = {exports: []}, calls = [], invalidations = [], queries = [];
let statusError = null, listError = null, response = {running: true, status: 'running'};
const context = {
  console, EN: undefined,
  jsx: (type, props, key) => ({type: typeof type === 'function' ? type.name : type, ...props, key}),
  jsxs: (type, props, key) => ({type: typeof type === 'function' ? type.name : type, ...props, key}),
  useState: initial => { const i = cursor++; if (!(i in states)) states[i] = initial; return [states[i], x => {states[i] = typeof x === 'function' ? x(states[i]) : x}]; },
  useRef: initial => { const i = refCursor++; return refs[i] ||= {current: initial}; },
  useEffect: fn => effects.push(fn),
  useQuery: opts => { queries.push(opts); const isList = opts.queryKey.includes('export-list'); return {data: isList ? listing : status, isLoading: false, isError: Boolean(isList ? listError : statusError), error: isList ? listError : statusError, refetch: async () => {}}; },
  queryClient: {invalidateQueries: async x => invalidations.push(x), setQueryData: (key, data) => {status = data}},
  host: {}, Button: 'Button', Codicon: 'Codicon', Skeleton: 'Skeleton', StatusDot: 'StatusDot', Tip: 'Tip',
};
vm.createContext(context);
vm.runInContext(src + `
api = {rest: async (path, opts) => { calls.push({path, opts}); if (path === '/export/status') return status; if (path === '/export/list') return listing; if (response?.constructor?.name === 'Error') throw response; return response; }, revealPath: async path => calls.push({reveal: path})};
`, Object.assign(context, {calls, invalidations}));
// Live variables cross the VM boundary through property accessors.
Object.defineProperties(context, {
 status: {get: () => status, set: x => status = x},
 listing: {get: () => listing}, response: {get: () => response},
});
const project = {id: 'p1', name: 'Example', folders: [{path: 'C:/one', primary: true}, {path: 'D:/two'}]};
context.project = project;
function render() { cursor = refCursor = 0; effects = []; queries = []; const tree = vm.runInContext("ExportSection({project, cwd: 'C:/one/sub'})", context); for (const effect of effects) effect(); return tree; }
function nodes(tree) { if (!tree || typeof tree !== 'object') return []; return [tree, ...[tree.children, tree.more, tree.actions].flat(8).flatMap(nodes)]; }
function buttons(tree) { return nodes(tree).filter(n => n.type === 'button' || n.type === 'Button'); }
function text(tree) { return JSON.stringify(tree); }
"""


@unittest.skipUnless(shutil.which('node'), 'Node is required to execute desktop UI tests')
class ExportUITests(unittest.TestCase):
    def run_js(self, checks: str):
        result = subprocess.run(['node', '-e', HARNESS + '\n(async () => {\n' + checks + '\n})().catch(e => {console.error(e); process.exitCode = 1})', str(ROOT / 'desktop/plugin.js')], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_disabled_without_export_directory(self):
        self.run_js("""
status = {enabled: true, export_dir: ''};
let tree = render();
assert.ok(text(tree).includes('export_dir'));
assert.equal(buttons(tree).filter(b => ['Export', 'Dry run'].includes(b.children)).length, 2);
assert.ok(buttons(tree).filter(b => ['Export', 'Dry run'].includes(b.children)).every(b => b.disabled));
status = {enabled: false, export_dir: 'C:/archives'};
assert.ok(buttons(render()).filter(b => ['Export', 'Dry run'].includes(b.children)).every(b => b.disabled));
assert.equal(calls.length, 0);
""")

    def test_start_sends_project_directories_and_dry_run(self):
        self.run_js("""
status = {enabled: true, export_dir: 'C:/archives', status: 'idle'};
let tree = render();
await buttons(tree).find(b => b.children === 'Dry run').onClick();
const request = calls.find(c => c.path === '/export');
assert.equal(request.opts.method, 'POST');
assert.equal(JSON.stringify(request.opts.body), JSON.stringify({project, directories: ['C:/one', 'D:/two'], dry_run: true, cwd: 'C:/one/sub'}));
assert.ok(invalidations.some(x => x.queryKey.includes('export-status')));
assert.ok(buttons(render()).filter(b => ['Export', 'Dry run'].includes(b.children)).every(b => b.disabled));
status = {enabled: true, export_dir: 'C:/archives', status: 'completed'};
response = {running: false, status: 'completed'};
await buttons(render()).find(b => b.children === 'Export').onClick();
assert.equal(calls.filter(c => c.path === '/export')[1].opts.body.dry_run, false);
""")

    def test_progress_polls_while_running_then_refreshes_archives(self):
        self.run_js("""
status = {enabled: true, export_dir: 'C:/archives', state: 'running', step: 'verify', files: 12, bytes: 2048, estimated_bytes: 4096, percent: 25};
let tree = render();
assert.ok(text(tree).includes('Verifying · files: 12 · 2048 bytes · 25%'));
assert.ok(text(tree).includes('Estimated size: 4 kB'));
assert.equal(nodes(tree).find(n => n.type === 'progress').value, 25);
let opts = queries.find(x => x.queryKey.includes('export-status'));
assert.equal(opts.refetchInterval(status), 2000);
assert.equal(opts.refetchInterval({state: {data: status}}), 2000);
status = {...status, state: 'completed', running: false, percent: 100};
render();
assert.ok(invalidations.some(x => x.queryKey.includes('export-list')));
opts = queries.find(x => x.queryKey.includes('export-status'));
assert.ok(opts.refetchInterval(status) >= 30000);
assert.ok(opts.refetchInterval({state: {data: status}}) >= 30000);
""")

    def test_archives_and_preserved_work_folder_reveal_paths(self):
        self.run_js("""
status = {enabled: true, export_dir: 'C:/archives', status: 'error', error: 'disk full', work_dir: 'C:/archives/_work/project'};
listing = {exports: [{name: 'project.zip', path: 'C:/archives/project.zip', size: 4096, mtime: 1700000000}]};
let tree = render();
assert.ok(text(tree).includes('Export failed: disk full'));
assert.ok(text(tree).includes('project.zip'));
assert.ok(text(tree).includes('4 kB'));
for (const p of ['C:/archives/_work/project', 'C:/archives/project.zip']) {
  const link = nodes(tree).find(n => n.type === 'button' && n.title === 'Reveal: ' + p);
  assert.ok(link, p);
  await link.onClick();
  assert.ok(calls.some(c => c.reveal === p));
}
const listQuery = queries.find(x => x.queryKey.includes('export-list'));
await listQuery.queryFn();
assert.ok(calls.some(c => c.path === '/export/list'));
""")

    def test_start_and_status_errors_are_visible_and_refreshable(self):
        self.run_js("""
status = {enabled: true, export_dir: 'C:/archives', status: 'idle'};
response = new Error('connection refused');
await buttons(render()).find(b => b.children === 'Export').onClick();
assert.ok(text(render()).includes('Export failed: connection refused'));
statusError = new Error('offline');
assert.ok(text(render()).includes('Export failed: offline'));
assert.ok(buttons(render()).filter(b => ['Export', 'Dry run'].includes(b.children)).every(b => b.disabled));
statusError = null;
listError = new Error('cannot list');
let tree = render();
assert.ok(text(tree).includes('Export failed: cannot list'));
await buttons(tree).find(b => b.children === 'Refresh').onClick();
assert.ok(invalidations.some(x => x.queryKey.includes('export-list')));
""")

    def test_section_is_mounted_with_resolved_workspace(self):
        self.run_js("""
context.usePluginI18n = () => {};
vm.runInContext("useRepo = () => ({project, cwd: 'C:/one', local: {}, gitByPath: new Map(), isRepo: false});", context);
const pane = vm.runInContext('RepoPane()', context);
const section = nodes(pane).find(n => n.type === 'ExportSection');
assert.ok(section);
assert.equal(section.project.name, 'Example');
assert.equal(section.cwd, 'C:/one');
""")

    def test_export_warnings_are_visible(self):
        self.run_js("""
status = {enabled: true, export_dir: 'C:/archives', state: 'dry_run', warnings: ['Uncheckpointed WAL omitted']};
assert.ok(text(render()).includes('Uncheckpointed WAL omitted'));
""")

    def test_core_archive_field_is_revealable_after_completion(self):
        self.run_js("""
status = {enabled: true, export_dir: 'C:/archives', state: 'completed', running: false, archive: 'C:/archives/core.zip', work_dir: 'C:/archives/_work/deleted'};
const tree = render();
const archive = nodes(tree).find(n => n.type === 'button' && n.title === 'Reveal: C:/archives/core.zip');
assert.ok(archive);
await archive.onClick();
assert.ok(calls.some(c => c.reveal === 'C:/archives/core.zip'));
assert.ok(!text(tree).includes('Reveal: C:/archives/_work/deleted'));
""")


if __name__ == '__main__':
    unittest.main()
