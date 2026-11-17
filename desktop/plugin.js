// Repo Status — Hermes Desktop half (disk plugin: plain ESM, no JSX, no build).
// Reads the read-only backend in ../dashboard/plugin_api.py through ctx.rest.
//
// Layout (top → bottom):
//   Project: <name>                                   [diff] [refresh]
//   Primary folder   ▸ <path>  git          (▸ expands the other project folders)
//   External repo    GitHub · owner/repo ↗
//   Git              branch → upstream · server state · last commit · git user
//   Uncommitted      ▸ N · first file       (▸ expands the full list)
//   Kam zapisoval Hermes   one row per repo / "Mimo git", each ▸ expandable
import {
  Button,
  Codicon,
  host,
  PALETTE_AREA,
  PANES_AREA,
  Skeleton,
  STATUSBAR_AREAS,
  StatusDot,
  Tip,
  queryClient,
  useQuery,
  usePluginI18n,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'repo-status'
const LOCAL_REFRESH_MS = 15_000
const REMOTE_REFRESH_MS = 60_000

let api = null // set in register(): { rest, openExternal, revealPath, runAction }

const KIND_ICON = { github: 'github', gitlab: 'source-control', forgejo: 'source-control', local: 'folder', other: 'globe' }

const REMOTE_TONE = {
  up_to_date: 'good', local_ahead: 'warn', behind: 'bad', diverged: 'bad', remote_newer: 'bad', not_on_remote: 'warn',
  no_upstream: 'warn', detached: 'warn', no_remote: 'muted', skipped: 'muted', not_a_repo: 'muted', error: 'warn'
}

// ── i18n: one catalog per Hermes language (locales/<lang>.json, served by GET /i18n) ──
// English ships inline (EN at the bottom of this file) so the panel never shows raw keys;
// the other languages arrive from the backend and register on top. Plain strings carry
// positional {0}, {1} placeholders; the Desktop translator wants functions for those.

/** Translate a catalog key with positional args (follows the app's display language). */
function T(key, ...args) {
  if (api?.t) return api.t(key, ...args)
  const v = EN[key]
  return v == null ? key : format(v, args)
}

/** Label for a code (`remote_short.up_to_date`); an unknown code shows as itself. */
function L(prefix, code) {
  if (code == null || code === '') return '—'
  const key = `${prefix}.${code}`
  const text = T(key)
  return text === key ? String(code) : text
}

function format(text, args) {
  return String(text).replace(/\{(\d+)\}/g, (_m, i) => (args[Number(i)] ?? '').toString())
}

/** Flat dotted catalog → nested message tree with {n} strings turned into functions. */
function toMessages(flat) {
  const out = {}
  for (const [key, value] of Object.entries(flat || {})) {
    if (key.startsWith('_') || typeof value !== 'string') continue
    const parts = key.split('.')
    let node = out
    for (const part of parts.slice(0, -1)) {
      if (typeof node[part] !== 'object' || node[part] === null) node[part] = {}
      node = node[part]
    }
    node[parts[parts.length - 1]] = /\{\d+\}/.test(value) ? (...args) => format(value, args) : value
  }
  return out
}

// ── data ────────────────────────────────────────────────────────────────────

const q = v => encodeURIComponent(v || '')
const localKey = cwd => [PLUGIN_ID, 'local', cwd]
const remoteKey = cwd => [PLUGIN_ID, 'remote', cwd]

const fetchLocal = cwd => api.rest(`/status?cwd=${q(cwd)}&remote=false`, { timeoutMs: 20_000 })
const fetchRemote = (cwd, fresh = false) => api.rest(`/remote?cwd=${q(cwd)}${fresh ? '&fresh=true' : ''}`, { timeoutMs: 30_000 })

async function refreshAll(cwd, fresh) {
  if (!api) return
  await queryClient.invalidateQueries({ queryKey: [PLUGIN_ID, 'project'] })
  if (!cwd) return
  await queryClient.invalidateQueries({ queryKey: localKey(cwd) })
  await queryClient.invalidateQueries({ queryKey: [PLUGIN_ID, 'touched'] })
  if (fresh) {
    try {
      queryClient.setQueryData(remoteKey(cwd), await fetchRemote(cwd, true))
    } catch (error) {
      host.notifyError(error, T('ui.server_check_failed'))
    }
  } else {
    await queryClient.invalidateQueries({ queryKey: remoteKey(cwd) })
  }
}

// The focused chat's project through the public gateway RPC: its name, its
// folders (primary first) and the chat's own stored cwd. A detached chat has
// host.state.cwd === '' but its stored session row still carries the cwd.
const norm = p => (p || '').replace(/[\\/]+$/, '').replace(/\//g, '\\').toLowerCase()
const inside = (path, base) => Boolean(path && base) && (norm(path) === norm(base) || norm(path).startsWith(norm(base) + '\\'))

async function resolveProject(storedId, sessionCwd) {
  const tree = await host.request('projects.tree', { preview_limit: 0 })
  const projects = (tree?.projects || []).filter(p => !p.isNoProject)
  // 1) the project that lists this chat; 2) else the project whose folder holds the chat's cwd
  // (a brand-new chat can be missing from sessionIds until the tree refreshes).
  let node = projects.find(p => (p.sessionIds || []).includes(storedId))
  if (!node && sessionCwd) {
    const folders = p => [p.path, ...(p.repos || []).map(r => r.path)].filter(Boolean)
    node = projects
      .filter(p => folders(p).some(f => inside(sessionCwd, f)))
      .sort((a, b) => Number(a.isAuto) - Number(b.isAuto))[0] // an explicit project beats an auto (git-root) one
  }
  if (!node) return { project: null, cwd: '' }

  let info = null
  try {
    const list = await host.request('projects.list', {})
    info = (list?.projects || []).find(p => p.id === node.id) || null
  } catch {
    info = null
  }
  let folders = (info?.folders || []).map(f => ({ path: f.path, label: f.label, primary: Boolean(f.is_primary) }))
  if (!folders.length) {
    const seen = new Set()
    for (const p of [node.path, ...(node.repos || []).map(r => r.path)]) {
      if (p && !seen.has(p.toLowerCase())) {
        seen.add(p.toLowerCase())
        folders.push({ path: p, label: null, primary: folders.length === 0 })
      }
    }
  }
  folders = folders.sort((a, b) => Number(b.primary) - Number(a.primary))

  let cwd = ''
  try {
    const res = await host.request('projects.project_sessions', { project_id: node.id })
    outer: for (const repo of res?.project?.repos || []) {
      for (const lane of repo.groups || []) {
        const row = (lane.sessions || []).find(s => s.id === storedId)
        if (row) {
          cwd = row.cwd || lane.path || repo.path || ''
          break outer
        }
      }
    }
  } catch {
    cwd = ''
  }
  return { project: { id: node.id, name: info?.name || node.label, folders }, cwd: cwd || info?.primary_path || node.path || '' }
}

let lastCwd = ''

function useWorkspace() {
  const sessionCwd = useValue(host.state.cwd)
  const storedId = useValue(host.state.focusedStoredSessionId)
  const projectQuery = useQuery({
    queryKey: [PLUGIN_ID, 'project', storedId, sessionCwd],
    queryFn: () => resolveProject(storedId, sessionCwd),
    enabled: Boolean(api && (storedId || sessionCwd)),
    staleTime: 5 * 60_000,
    retry: 1
  })
  const project = projectQuery.data?.project || null
  const folderPaths = (project?.folders || []).map(f => f.path)
  const folderQuery = useQuery({
    queryKey: [PLUGIN_ID, 'folders', folderPaths.join('|')],
    queryFn: () => api.rest('/folders', { method: 'POST', body: { paths: folderPaths } }),
    enabled: Boolean(api && folderPaths.length),
    staleTime: 60_000
  })
  const gitByPath = new Map((folderQuery.data?.folders || []).map(f => [f.path, f]))
  const firstGitFolder = (project?.folders || []).find(f => gitByPath.get(f.path)?.git_root)?.path || ''
  const resolvedCwd = projectQuery.data?.cwd || ''
  const cwd = sessionCwd || resolvedCwd || firstGitFolder
  lastCwd = cwd
  return {
    cwd,
    cwdSource: sessionCwd ? 'session' : resolvedCwd ? 'stored' : cwd ? 'project' : null,
    resolving: Boolean(storedId) && projectQuery.isLoading,
    project,
    gitByPath
  }
}

function useRepo() {
  const ws = useWorkspace()
  const { cwd } = ws
  const busy = useValue(host.state.busy)
  const local = useQuery({
    queryKey: localKey(cwd),
    queryFn: () => fetchLocal(cwd),
    enabled: Boolean(cwd && api),
    refetchInterval: LOCAL_REFRESH_MS,
    staleTime: 5_000
  })
  const isRepo = Boolean(local.data?.ok)
  const remote = useQuery({
    queryKey: remoteKey(cwd),
    queryFn: () => fetchRemote(cwd),
    enabled: Boolean(cwd && api && isRepo && (local.data?.remotes || []).length),
    refetchInterval: REMOTE_REFRESH_MS,
    staleTime: 30_000
  })

  // Re-read git truth when a turn settles (the agent may have written files).
  const wasBusy = useRef(busy)
  useEffect(() => {
    if (wasBusy.current && !busy && cwd) void queryClient.invalidateQueries({ queryKey: localKey(cwd) })
    wasBusy.current = busy
  }, [busy, cwd])

  const data = local.data
  const remoteCheck = !isRepo
    ? null
    : !(data.remotes || []).length
      ? { status: 'no_remote' }
      : remote.data || (remote.isLoading ? { status: 'checking' } : null)
  return { ...ws, local, remote, data, isRepo, remoteCheck, summary: isRepo ? summarize(data, remoteCheck) : null }
}

// Same rules as gitprobe.summarize(), with the server check merged in on the client.
function summarize(p, rc) {
  const ch = p.changes || {}
  const br = p.branch || {}
  const status = rc?.status
  const reasons = []
  if ((ch.total || 0) > 0) reasons.push('uncommitted')
  if (status === 'local_ahead' || status === 'diverged' || (['error', 'remote_newer', 'checking', undefined].includes(status) && (br.ahead || 0) > 0)) reasons.push('unpushed')
  if (['behind', 'diverged', 'remote_newer'].includes(status) || (['error', 'checking', undefined].includes(status) && (br.behind || 0) > 0)) reasons.push('server_newer')
  if (status === 'not_on_remote') reasons.push('not_on_remote')
  if (status === 'no_upstream') reasons.push('no_upstream')
  if (status === 'error') reasons.push('remote_unreachable')
  if ((ch.conflicted || 0) > 0) reasons.push('conflicts')
  const tone = reasons.includes('conflicts') || reasons.includes('server_newer') ? 'bad' : reasons.length ? 'warn' : 'good'
  return { current: reasons.length === 0, reasons, tone }
}

// ── formatting ──────────────────────────────────────────────────────────────

function ago(iso) {
  if (!iso) return T('time.never')
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  if (s < 90) return T('time.seconds_ago', s)
  const m = Math.round(s / 60)
  if (m < 90) return T('time.minutes_ago', m)
  const h = Math.round(m / 60)
  if (h < 48) return T('time.hours_ago', h)
  return T('time.days_ago', Math.round(h / 24))
}

/** Compact age without "ago" (session rows): 5 min · 3 h · 2 d. */
function age(iso) {
  if (!iso) return '—'
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  if (s < 90) return `${s} s`
  const m = Math.round(s / 60)
  if (m < 90) return `${m} min`
  const h = Math.round(m / 60)
  return h < 48 ? `${h} h` : `${Math.round(h / 24)} d`
}

function clock(ts) {
  const d = new Date(ts * 1000)
  const sameDay = d.toDateString() === new Date().toDateString()
  return sameDay
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function marker(f) {
  if (f.conflicted) return '‼'
  if (f.untracked) return '?'
  const xy = f.xy || '..'
  return xy[0] !== '.' ? xy[0] : xy[1]
}

const MARKER_CLASS = { '?': 'text-emerald-500', A: 'text-emerald-500', D: 'text-destructive', '‼': 'text-destructive', R: 'text-sky-500' }

// A 404 from our own namespace means the gateway still runs an older plugin_api.py
// (the desktop half reloads on Rescan, the backend only on a gateway restart).
function errText(error) {
  const msg = String(error?.message || error || '')
  if (/\b404\b|not found/i.test(msg)) return T('ui.backend_outdated')
  return msg
}

// Slovak plural: 1 provider · 2–4 provideri · 5+ providerov (forms = [one, few, many]).
function plural(n, [one, few, many]) {
  return `${n} ${n === 1 ? one : n >= 2 && n <= 4 ? few : many}`
}

// Keep the END of a long path visible: "C:\REPOS\TCS\TCS.Core-dotnet" → "…\TCS\TCS.Core-dotnet".
function tailPath(p, max = 34) {
  if (!p || p.length <= max) return p
  const parts = p.split(/[\\/]/)
  let out = parts.pop()
  while (parts.length && out.length + parts[parts.length - 1].length + 1 <= max - 2) out = `${parts.pop()}\\${out}`
  return `…\\${out}`
}

// ── building blocks ─────────────────────────────────────────────────────────

const LABEL = 'shrink-0 w-[6.75rem] whitespace-nowrap pt-px text-[0.6875rem] text-(--ui-text-tertiary)'

function Chevron({ open, onToggle, label }) {
  return jsx('button', {
    type: 'button',
    'aria-label': label,
    'aria-expanded': open,
    onClick: onToggle,
    className: 'inline-flex size-4 shrink-0 items-center justify-center rounded text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground',
    children: jsx(Codicon, { name: open ? 'chevron-down' : 'chevron-right', size: '0.75rem' })
  })
}

// One labelled line; with `more` it gets a ▸ that reveals the extra lines below.
function Field({ label, children, more, defaultOpen = false, actions }) {
  const [open, setOpen] = useState(defaultOpen)
  return jsxs('div', {
    className: 'py-1',
    children: [
      jsxs('div', {
        className: 'flex min-w-0 items-start gap-2 text-xs',
        children: [
          jsx('div', { className: LABEL, children: label }),
          jsxs('div', {
            className: 'flex min-w-0 flex-1 items-start gap-1',
            children: [
              more ? jsx(Chevron, { open, onToggle: () => setOpen(!open), label: open ? T('ui.collapse') : T('ui.expand') }) : null,
              jsx('div', { className: 'min-w-0 flex-1 text-(--ui-text-secondary)', children })
            ]
          }),
          actions || null
        ]
      }),
      more && open ? jsx('div', { className: 'mt-1 space-y-0.5 pl-[7.75rem]', children: more }) : null
    ]
  })
}

// A folder path that opens Explorer on click.
function FolderLink({ path, git, mono = true, title, max = 40 }) {
  return jsxs('span', {
    className: 'inline-flex min-w-0 max-w-full items-center gap-1',
    children: [
      jsx('button', {
        type: 'button',
        title: title || T('ui.open_in_explorer', path),
        onClick: () => void api?.revealPath(path),
        className: `min-w-0 truncate text-left ${mono ? 'font-mono text-[0.6875rem]' : ''} text-foreground underline decoration-dotted underline-offset-2 hover:text-sky-500`,
        children: tailPath(path, max) // the end of a path tells folders apart; the tooltip has it all
      }),
      git ? jsx('span', { className: 'shrink-0 rounded border border-(--ui-border) px-1 text-[0.625rem] leading-4 text-(--ui-text-tertiary)', children: 'git' }) : null
    ]
  })
}

function ExternalLink({ href, children }) {
  return jsxs('button', {
    type: 'button',
    title: href,
    onClick: () => void api?.openExternal(href),
    className: 'inline-flex min-w-0 items-center gap-1 text-left text-foreground underline decoration-dotted underline-offset-2 hover:text-sky-500',
    children: [jsx('span', { className: 'min-w-0 truncate', children }), jsx(Codicon, { name: 'link-external', size: '0.7rem' })]
  })
}

function Mono({ children }) {
  return jsx('span', { className: 'font-mono text-[0.6875rem] text-foreground', children })
}

function Section({ title, children }) {
  return jsxs('div', {
    className: 'border-t border-(--ui-border) px-3 py-2',
    children: [title ? jsx('div', { className: 'mb-1 text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary)', children: title }) : null, children]
  })
}

function FileLine({ f, stat }) {
  const m = marker(f)
  return jsxs('div', {
    className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem]',
    title: f.path,
    children: [
      jsx('span', { className: `w-3 shrink-0 text-center font-mono ${MARKER_CLASS[m] || 'text-amber-500'}`, children: m }),
      jsx('span', { className: 'min-w-0 flex-1 truncate font-mono text-foreground', children: f.path }),
      stat && !stat.binary
        ? jsxs('span', {
            className: 'shrink-0 font-mono tabular-nums',
            children: [jsx('span', { className: 'text-emerald-500', children: `+${stat.added}` }), ' ', jsx('span', { className: 'text-destructive', children: `−${stat.removed}` })]
          })
        : null
    ]
  })
}

// ── sections ────────────────────────────────────────────────────────────────

function ProjectSection({ project, gitByPath, repoRoot, cwd, cwdSource }) {
  const folders = project?.folders?.length ? project.folders : cwd ? [{ path: cwd, primary: true }] : []
  if (!folders.length) return null
  const [first, ...rest] = folders
  const isGit = f => Boolean(gitByPath.get(f.path)?.git_root) || (repoRoot && f.path.toLowerCase() === repoRoot.toLowerCase())
  return jsxs(Section, {
    children: [
      jsx(Field, {
        label: project ? T('ui.primary_folder') : T('ui.folder'),
        more: rest.length
          ? rest.map(f =>
              jsx(
                'div',
                { className: 'flex min-w-0 items-center gap-1 text-xs', children: jsx(FolderLink, { path: f.path, git: isGit(f), title: f.label ? `${f.label} – ${f.path}` : undefined }) },
                f.path
              )
            )
          : null,
        children: jsxs('span', {
          className: 'inline-flex min-w-0 max-w-full flex-wrap items-center gap-1',
          children: [
            jsx(FolderLink, { path: first.path, git: isGit(first) }),
            rest.length ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `+${rest.length}` }) : null
          ]
        })
      }),
      jsx(FilesSection, { folder: repoRoot || first.path }),
      repoRoot && !folders.some(f => f.path.toLowerCase() === repoRoot.toLowerCase())
        ? jsx(Field, {
            label: T('ui.chat_git_repo'),
            children: jsxs('span', {
              className: 'inline-flex min-w-0 max-w-full items-center gap-1',
              children: [
                jsx(FolderLink, { path: repoRoot, git: true }),
                cwdSource === 'stored' ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `· ${T('ui.stored_folder')}` }) : null
              ]
            })
          })
        : null
    ]
  })
}

function RemoteSection({ data }) {
  const remotes = data.remotes || []
  if (!remotes.length) return jsx(Field, { label: T('ui.external_repo'), children: T('repo.no_remote') })
  const [first, ...rest] = remotes
  const line = r =>
    jsxs('span', {
      className: 'inline-flex min-w-0 max-w-full items-center gap-1',
      children: [
        jsx(Codicon, { name: KIND_ICON[r.kind] || 'globe', size: '0.8rem' }),
        jsx('span', { className: 'shrink-0', children: r.kind_label }),
        r.web_url ? jsx(ExternalLink, { href: r.web_url, children: r.repo || r.url }) : jsx(Mono, { children: r.url }),
        remotes.length > 1 ? jsx('span', { className: 'shrink-0 text-[0.6875rem] text-(--ui-text-tertiary)', children: `(${r.name})` }) : null
      ]
    })
  return jsx(Field, {
    label: T('ui.external_repo'),
    more: rest.length ? rest.map(r => jsx('div', { className: 'text-xs', children: line(r) }, r.name)) : null,
    children: line(first)
  })
}

function GitSection({ data, remoteCheck }) {
  const br = data.branch || {}
  const lc = data.last_commit
  const id = data.identity || {}
  const scope = (id.email || id.name || {}).scope
  const [tone, text] = remoteCheck?.status === 'checking' ? ['muted', T('ui.checking')] : remoteCheck?.status ? [REMOTE_TONE[remoteCheck.status] || 'muted', L('remote_short', remoteCheck.status)] : ['muted', '—']
  const counts = []
  if (remoteCheck?.ahead) counts.push(`↑${remoteCheck.ahead}`)
  if (remoteCheck?.behind) counts.push(`↓${remoteCheck.behind}`)
  return jsxs('div', {
    children: [
      jsx(Field, {
        label: T('repo.branch'),
        more: [
          lc
            ? jsx('div', { className: 'truncate text-[0.6875rem]', title: `${lc.oid}\n${lc.author} <${lc.email}>\n${lc.date}`, children: [jsx(Mono, { children: lc.short }, 'o'), ` ${lc.subject} · ${lc.author} · ${ago(lc.date)}`] }, 'lc')
            : jsx('div', { className: 'text-[0.6875rem]', children: T('repo.no_commit') }, 'lc'),
          remoteCheck?.error ? jsx('div', { className: 'text-[0.6875rem] text-destructive', children: remoteCheck.error }, 'err') : null,
          (data.remotes || []).length
            ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: T('ui.checked_and_fetch', remoteCheck?.checked_at ? ago(remoteCheck.checked_at) : '—', ago(data.fetch?.last_fetch)) }, 'f')
            : null,
          (data.repo?.worktrees || []).length > 1
            ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `worktrees: ${data.repo.worktrees.map(w => w.branch || (w.head || '').slice(0, 7)).join(', ')}` }, 'wt')
            : null
        ],
        children: jsxs('span', {
          className: 'inline-flex min-w-0 max-w-full flex-wrap items-center gap-1.5',
          children: [
            jsx(Mono, { children: br.detached ? `detached ${(br.oid || '').slice(0, 7)}` : br.head }),
            br.upstream ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `→ ${br.upstream}` }) : null,
            jsx(StatusDot, { tone }),
            jsx('span', { className: 'text-[0.6875rem]', children: text }),
            counts.length ? jsx(Mono, { children: counts.join(' ') }) : null
          ]
        })
      }),
      jsx(Field, {
        label: T('ui.git_user'),
        children:
          id.name || id.email
            ? jsxs('span', {
                children: [
                  jsx('span', { className: 'text-foreground', children: id.name?.value || '?' }),
                  ` <${id.email?.value || '?'}>`,
                  jsx('span', { className: 'text-(--ui-text-tertiary)', children: ` · ${L('gitscope', scope)}` })
                ]
              })
            : jsx('span', { className: 'text-destructive', children: T('ui.not_set') })
      })
    ]
  })
}

// Hermes' working material inside the repo (.hermes/ plans, sketches, notes): shown apart,
// never counted as uncommitted source changes.
function WorkFilesSection({ data }) {
  const wf = data.work_files || {}
  if (!wf.total) return null
  const dirs = Object.entries(wf.dirs || {})
  const root = data.repo?.root || ''
  return jsx(Field, {
    label: T('ui.work_files'),
    more: [
      jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: T('ui.work_files_note') }, 'note'),
      ...(wf.files || []).map(f => jsx(FileLine, { f }, f.path)),
      wf.truncated ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `… ${T('common.and_more', wf.truncated)}` }, 'more') : null
    ],
    children: jsxs('span', {
      className: 'inline-flex min-w-0 max-w-full items-center gap-1.5 text-[0.6875rem]',
      children: [
        jsx('span', { className: 'shrink-0 font-medium text-(--ui-text-secondary)', children: wf.total }),
        ...dirs.map(([d]) => jsx(FolderLink, { path: root ? `${root}\\${d.replace(/\/$/, '').replace(/\//g, '\\')}` : d, title: T('ui.open_in_explorer', d) }, d))
      ]
    })
  })
}

function UncommittedSection({ data }) {
  const ch = data.changes || {}
  const ds = data.diffstat || {}
  const files = ch.files || []
  const stat = new Map((ds.files || []).map(f => [f.path, f]))
  if (!ch.total) {
    const clean = data.work_files?.total ? T('ui.no_code_changes') : T('ui.tree_clean')
    return jsx(Field, { label: T('ui.uncommitted'), children: jsx('span', { className: 'text-emerald-500', children: clean }) })
  }
  const parts = [
    ch.staged ? `${ch.staged} ${T('changes.staged')}` : null,
    ch.unstaged ? `${ch.unstaged} ${T('changes.unstaged')}` : null,
    ch.untracked ? `${ch.untracked} ${T('changes.untracked')}` : null,
    ch.conflicted ? `${ch.conflicted} ${T('changes.conflicted')}` : null
  ].filter(Boolean)
  return jsx(Field, {
    label: T('ui.uncommitted'),
    actions: jsx(Tip, {
      label: T('ui.diff_in_review'),
      children: jsx(Button, {
        variant: 'ghost',
        size: 'icon-xs',
        'aria-label': T('ui.open_diff'),
        onClick: () => api?.runAction('view.toggleReview'),
        children: jsx(Codicon, { name: 'diff', size: '0.8rem' })
      })
    }),
    more: [
      jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `${parts.join(' · ')}${ds.count ? ` · ${T('ui.lines_changed', ds.added, ds.removed)}` : ''}` }, 'sum'),
      ...files.map(f => jsx(FileLine, { f, stat: stat.get(f.path) }, f.path)),
      ch.truncated ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `… ${T('common.and_more', ch.truncated)}` }, 'more') : null
    ],
    children: jsxs('span', {
      className: 'inline-flex min-w-0 max-w-full items-center gap-1.5',
      children: [
        jsx('span', { className: 'shrink-0 font-medium text-amber-500', children: ch.total }),
        files[0] ? jsx('span', { className: 'min-w-0 truncate font-mono text-[0.6875rem] text-foreground', title: files[0].path, children: files[0].path }) : null
      ]
    })
  })
}

// ── Kam zapisoval Hermes (ledger) ───────────────────────────────────────────

function TouchedGroup({ group }) {
  const files = group.files || []
  const title = group.in_repo ? group.root : T('common.outside_git_short')
  const label = jsxs('span', {
    className: 'inline-flex items-center gap-1',
    children: [
      group.outside_cwd ? jsx(Tip, { label: T('ui.outside_chat_folder'), children: jsx(Codicon, { name: 'warning', size: '0.75rem', className: 'text-amber-500' }) }) : null,
      `${group.file_count}✍ ${group.probable || 0}⌨`
    ]
  })
  const fileRow = f =>
    jsxs(
      'div',
      {
        className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem]',
        title: `${f.path}\n${f.tools.join(', ')} · ${f.evidence}${f.path_guessed ? ` · ${T('touched.path_guessed')}` : ''}`,
        children: [
          jsx(StatusDot, { tone: f.still_uncommitted ? 'warn' : f.still_uncommitted === false ? 'good' : 'muted' }),
          group.in_repo
            ? jsx('span', { className: 'min-w-0 flex-1 truncate font-mono text-foreground', children: f.rel_path || f.path })
            : jsx('span', { className: 'min-w-0 flex-1', children: jsx(FolderLink, { path: f.path }) }),
          jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary)', children: `${f.actions.map(a => L('action', a)).join('/')} ${clock(f.last_ts)}` })
        ]
      },
      f.path
    )
  return jsx(Field, {
    label,
    defaultOpen: false,
    more: [
      ...files.map(fileRow),
      group.file_count > files.length ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `… ${T('common.and_more', group.file_count - files.length)}` }, 'more') : null,
      ...(group.commands || []).slice(0, 5).map((c, i) =>
        jsxs(
          'div',
          {
            className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
            title: `${c.tool} · ${T('touched.probable_note')}${c.folder ? `\n${c.folder}` : ''}`,
            children: [
              jsx(Codicon, { name: 'terminal', size: '0.7rem' }),
              jsx('span', { className: 'min-w-0 flex-1 truncate font-mono', children: c.detail || c.tool }),
              jsx('span', { className: 'shrink-0', children: clock(c.ts) })
            ]
          },
          `c${i}`
        )
      )
    ],
    children: group.in_repo
      ? jsxs('span', {
          className: 'inline-flex min-w-0 max-w-full items-center gap-1',
          children: [jsx(FolderLink, { path: title, git: true, max: 26 }), group.branch ? jsx('span', { className: 'shrink-0 font-mono text-[0.6875rem] text-(--ui-text-tertiary)', children: group.branch }) : null]
        })
      : jsxs('span', {
          className: 'flex min-w-0 max-w-full items-center gap-1.5',
          children: [
            jsx('span', { className: 'shrink-0 whitespace-nowrap font-medium text-foreground', children: T('common.outside_git_short') }),
            files[0] ? jsx('span', { className: 'min-w-0 truncate font-mono text-[0.6875rem] text-(--ui-text-tertiary)', title: files[0].path, children: files[0].path }) : null
          ]
        })
  })
}

function HermesChanges({ cwd }) {
  const sessionId = useValue(host.state.focusedStoredSessionId)
  const busy = useValue(host.state.busy)
  const [scope, setScope] = useState('session')
  const useSession = scope === 'session' && Boolean(sessionId)
  const query = useQuery({
    queryKey: [PLUGIN_ID, 'touched', scope, sessionId, cwd],
    queryFn: () => api.rest(useSession ? `/touched?session=${q(sessionId)}&cwd=${q(cwd)}` : `/touched?hours=24&cwd=${q(cwd)}`, { timeoutMs: 30_000 }),
    enabled: Boolean(api),
    refetchInterval: 30_000,
    staleTime: 10_000
  })
  const wasBusy = useRef(busy)
  useEffect(() => {
    if (wasBusy.current && !busy) void queryClient.invalidateQueries({ queryKey: [PLUGIN_ID, 'touched'] })
    wasBusy.current = busy
  }, [busy])

  const groups = query.data?.groups || []
  const outside = groups.filter(g => g.outside_cwd).length
  return jsxs(Section, {
    title: T('touched.title'),
    children: [
      jsxs('div', {
        className: 'mb-1 flex items-center gap-1 text-[0.6875rem]',
        children: [
          ...[
            ['session', T('ui.this_chat')],
            ['all', T('ui.all_24h')]
          ].map(([value, label]) =>
            jsx(
              'button',
              {
                type: 'button',
                onClick: () => setScope(value),
                className: `rounded px-1.5 py-0.5 ${scope === value ? 'bg-(--chrome-action-hover) text-foreground' : 'text-(--ui-text-tertiary) hover:text-foreground'}`,
                children: label
              },
              value
            )
          ),
          outside ? jsx('span', { className: 'ml-auto text-amber-500', children: `⚠ ${T('ui.outside_cwd_count', outside)}` }) : null
        ]
      }),
      query.isLoading
        ? jsx(Skeleton, { className: 'h-4' })
        : query.isError
          ? jsx('div', { className: 'text-xs text-amber-500', children: errText(query.error) })
          : !groups.length
            ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: scope === 'session' ? T('ui.touched_empty_chat') : T('ui.touched_empty_24h') })
            : jsx('div', { children: groups.map(g => jsx(TouchedGroup, { group: g }, g.root || 'none')) })
    ]
  })
}

// ── Hermes settings (skills, memory, cron, config, SOUL, plugins) ──────────


function SettingsItem({ it }) {
  const who = it.by_hermes ? ['good', 'Hermes'] : it.unattributed ? ['warn', T('settings.unknown_origin')] : ['muted', T('settings.hermes_file')]
  return jsxs('div', {
    className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem]',
    title: [...(it.details || []), it.sessions?.length ? `sessions: ${it.sessions.join(', ')}` : ''].filter(Boolean).join('\n'),
    children: [
      jsx(StatusDot, { tone: who[0] }),
      jsx('span', { className: 'min-w-0 flex-1 truncate font-mono text-foreground', children: it.item }),
      jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary)', children: `${it.actions.map(a => L('settings_action', a)).join('/')} · ${who[1]} · ${clock(it.last_ts)}` })
    ]
  })
}

function HermesSettings() {
  const [hours, setHours] = useState(168)
  const busy = useValue(host.state.busy)
  const query = useQuery({
    queryKey: [PLUGIN_ID, 'hermes-settings', hours],
    queryFn: () => api.rest(`/hermes-settings?hours=${hours}`, { timeoutMs: 60_000 }),
    enabled: Boolean(api),
    refetchInterval: 120_000,
    staleTime: 30_000
  })
  const wasBusy = useRef(busy)
  useEffect(() => {
    if (wasBusy.current && !busy) void queryClient.invalidateQueries({ queryKey: [PLUGIN_ID, 'hermes-settings'] })
    wasBusy.current = busy
  }, [busy])
  const cats = query.data?.categories || []
  return jsxs(Section, {
    title: T('settings.section'),
    children: [
      jsx('div', {
        className: 'mb-1 flex items-center gap-1 text-[0.6875rem]',
        children: [
          [24, '24 h'],
          [168, T('ui.days_n', 7)],
          [720, T('ui.days_n', 30)]
        ].map(([value, label]) =>
          jsx(
            'button',
            {
              type: 'button',
              onClick: () => setHours(value),
              className: `rounded px-1.5 py-0.5 ${hours === value ? 'bg-(--chrome-action-hover) text-foreground' : 'text-(--ui-text-tertiary) hover:text-foreground'}`,
              children: label
            },
            value
          )
        )
      }),
      query.isLoading
        ? jsx(Skeleton, { className: 'h-4' })
        : query.isError
          ? jsx('div', { className: 'text-xs text-amber-500', children: errText(query.error) })
          : !cats.length
            ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: T('settings.empty') })
            : jsx('div', {
                children: cats.map(c =>
                  jsx(
                    Field,
                    {
                      label: `${L('category', c.category)} (${c.item_count})`,
                      more: [
                        ...c.items.map(it => jsx(SettingsItem, { it }, it.item)),
                        c.item_count > c.items.length ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `… ${T('common.and_more', c.item_count - c.items.length)}` }, 'more') : null
                      ],
                      children: jsxs('span', {
                        className: 'inline-flex min-w-0 max-w-full items-center gap-1.5',
                        children: [
                          c.items.some(i => i.unattributed) ? jsx(Tip, { label: T('ui.some_unattributed'), children: jsx(Codicon, { name: 'warning', size: '0.75rem', className: 'text-amber-500' }) }) : null,
                          jsx('span', { className: 'min-w-0 truncate font-mono text-[0.6875rem] text-foreground', children: c.items[0]?.item }),
                          jsx('span', { className: 'shrink-0 text-[0.6875rem] text-(--ui-text-tertiary)', children: clock(c.items[0]?.last_ts || 0) })
                        ]
                      })
                    },
                    c.category
                  )
                )
              })
    ]
  })
}

// ── Files, external communication, sessions ────────────────────────────

const SOURCE_LABELS = {
  local: 'Desktop', desktop: 'Desktop', cli: 'CLI', tui: 'TUI', discord: 'Discord', telegram: 'Telegram', slack: 'Slack',
  teams: 'Teams', whatsapp: 'WhatsApp', signal: 'Signal', matrix: 'Matrix', email: 'E-mail', cron: 'Cron', api: 'API',
  acp: 'IDE (ACP)', webhook: 'Webhook', kanban: 'Kanban', subagent: 'Subagent', delegate: 'Subagent'
}
const sourceLabel = s => {
  const code = (s || '').toLowerCase()
  const text = T(`source.${code}`)
  return text !== `source.${code}` ? text : SOURCE_LABELS[code] || s || '—'
}

function fmtSize(n) {
  if (n == null) return ''
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`
  if (n >= 1024) return `${Math.round(n / 1024)} kB`
  return `${n} B`
}

const useDir = (path, enabled = true) =>
  useQuery({
    queryKey: [PLUGIN_ID, 'ls', path],
    queryFn: () => api.rest(`/ls?path=${q(path)}`, { timeoutMs: 20_000 }),
    enabled: Boolean(api && path && enabled),
    staleTime: 15_000
  })

function DirEntry({ e, depth }) {
  const [open, setOpen] = useState(false)
  return jsxs('div', {
    children: [
      jsxs('div', {
        className: 'flex min-w-0 items-center gap-1 text-[0.6875rem]',
        style: { paddingLeft: `${depth * 0.75}rem` },
        children: [
          e.dir ? jsx(Chevron, { open, onToggle: () => setOpen(!open), label: open ? T('ui.collapse') : T('ui.expand') }) : jsx('span', { className: 'inline-block w-4 shrink-0' }),
          jsx(Codicon, { name: e.dir ? (open ? 'folder-opened' : 'folder') : 'file', size: '0.75rem', className: 'shrink-0 text-(--ui-text-tertiary)' }),
          jsx('button', {
            type: 'button',
            title: `${e.path}\n${T('ui.click_open_explorer')}`,
            onClick: () => void api?.revealPath(e.path),
            className: `min-w-0 flex-1 truncate text-left font-mono hover:text-sky-500 ${e.hidden ? 'text-(--ui-text-tertiary)' : 'text-foreground'}`,
            children: e.name
          }),
          e.dir ? null : jsx('span', { className: 'shrink-0 tabular-nums text-(--ui-text-tertiary)', children: fmtSize(e.size) })
        ]
      }),
      e.dir && open ? jsx(DirList, { path: e.path, depth: depth + 1 }) : null
    ]
  })
}

function DirList({ path, depth = 0 }) {
  const query = useDir(path)
  if (query.isLoading) return jsx(Skeleton, { className: 'my-0.5 h-3' })
  if (query.isError) return jsx('div', { className: 'text-[0.6875rem] text-amber-500', children: errText(query.error) })
  const d = query.data
  if (!d?.ok) return jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: d?.error || '—' })
  return jsxs('div', {
    children: [
      ...d.entries.map(e => jsx(DirEntry, { e, depth }, e.path)),
      d.truncated ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', style: { paddingLeft: `${depth * 0.75}rem` }, children: `… ${T('common.and_more', d.truncated)}` }) : null,
      !d.entries.length ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: T('ui.empty_folder') }) : null
    ]
  })
}

function FilesSection({ folder }) {
  const query = useDir(folder)
  if (!folder) return null
  const d = query.data
  return jsx(Field, {
    label: T('ui.files'),
    more: [jsx(DirList, { path: folder }, 'root')],
    children: jsxs('span', {
      className: 'inline-flex min-w-0 max-w-full items-center gap-1.5 text-[0.6875rem]',
      children: [
        jsx(FolderLink, { path: folder }),
        d?.ok ? jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary)', children: T('ui.items_count', d.count) }) : null
      ]
    })
  })
}

// Sessions of the project (lanes of projects.project_sessions) or all (session.list), newest first.
function useSessionList(project, scope) {
  return useQuery({
    queryKey: [PLUGIN_ID, 'sessions', scope, project?.id || ''],
    queryFn: async () => {
      if (scope === 'project' && project?.id) {
        const res = await host.request('projects.project_sessions', { project_id: project.id })
        const seen = new Map()
        for (const repo of res?.project?.repos || []) {
          for (const lane of repo.groups || []) {
            for (const s of lane.sessions || []) if (!seen.has(s.id)) seen.set(s.id, { ...s, lane: lane.label })
          }
        }
        return [...seen.values()].sort((a, b) => (b.last_active || b.started_at || 0) - (a.last_active || a.started_at || 0))
      }
      const res = await host.request('session.list', { limit: 60 })
      return (res?.sessions || []).slice().sort((a, b) => (b.last_active || b.started_at || 0) - (a.last_active || a.started_at || 0))
    },
    enabled: Boolean(api && (scope === 'all' || project?.id)),
    staleTime: 20_000,
    refetchInterval: 60_000
  })
}

function useSessionsMeta(ids) {
  return useQuery({
    queryKey: [PLUGIN_ID, 'sessions-meta', ids.join('|')],
    queryFn: () => api.rest('/sessions-meta', { method: 'POST', body: { ids } }),
    enabled: Boolean(api && ids.length),
    staleTime: 20_000,
    refetchInterval: 60_000
  })
}

function CapacityBar({ ctx }) {
  if (!ctx?.used) return jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary)', title: T('ui.capacity_unknown'), children: '—' })
  const pct = ctx.max ? Math.min(100, Math.round((ctx.used / ctx.max) * 100)) : null
  const tone = pct == null ? 'bg-(--ui-text-tertiary)' : pct >= 85 ? 'bg-red-500' : pct >= 60 ? 'bg-amber-500' : 'bg-emerald-500'
  return jsxs('span', {
    className: 'inline-flex shrink-0 items-center gap-1 tabular-nums',
    title: T('ui.capacity_tip', ctx.used.toLocaleString(), ctx.max ? ctx.max.toLocaleString() : '?', clock(ctx.ts)),
    children: [
      jsx('span', { className: 'relative inline-block h-1.5 w-8 overflow-hidden rounded bg-(--ui-border)', children: jsx('span', { className: `absolute inset-y-0 left-0 ${tone}`, style: { width: `${pct ?? 100}%` } }) }),
      jsx('span', { className: 'text-(--ui-text-tertiary)', children: `${tok(ctx.used)}/${ctx.max ? tok(ctx.max) : '?'}` })
    ]
  })
}

function SessionRow({ s, meta, current }) {
  const providers = meta?.providers || []
  const provText = [...new Set(providers.map(p => p.provider))].join(', ')
  const open = () => {
    if (host.openSession) void host.openSession(s.id)
  }
  return jsxs('div', {
    className: `rounded px-1 py-0.5 ${current ? 'bg-(--chrome-action-hover)' : ''}`,
    children: [
      jsxs('div', {
        className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem]',
        children: [
          jsx(StatusDot, { tone: current ? 'good' : s.is_active ? 'warn' : 'muted' }),
          jsx('button', {
            type: 'button',
            onClick: open,
            title: `${s.title || s.id}\n${s.id}\n${T('ui.click_switch_session')}`,
            className: 'min-w-0 flex-1 truncate text-left text-foreground hover:text-sky-500',
            children: s.title || s.preview || s.id
          }),
          jsx(CapacityBar, { ctx: meta?.context }),
          jsx('span', { className: 'w-12 shrink-0 text-right text-(--ui-text-tertiary)', children: age(new Date((s.last_active || s.started_at || 0) * 1000).toISOString()) })
        ]
      }),
      jsx('div', {
        className: 'truncate pl-3.5 text-[0.625rem] text-(--ui-text-tertiary)',
        title: providers.map(p => `${p.provider}/${p.model} – ${p.calls}×`).join('\n'),
        children: [sourceLabel(s.handoff_platform || s.source), s.lane ? T('ui.branch_lane', s.lane) : null, provText || null].filter(Boolean).join(' · ')
      })
    ]
  })
}

function SessionsSection({ project }) {
  const storedId = useValue(host.state.focusedStoredSessionId)
  // null = automatic: the project's sessions once the project is known. A fixed initial
  // value would stick to "all" because the project resolves after the first render.
  const [scope, setScope] = useState(null)
  const wanted = scope ?? (project ? 'project' : 'all')
  const effective = wanted === 'project' && !project ? 'all' : wanted
  const list = useSessionList(project, effective)
  const sessions = list.data || []
  const ids = sessions.slice(0, 60).map(s => s.id)
  const meta = useSessionsMeta(ids).data?.sessions || {}
  const first = sessions[0]
  return jsxs('div', {
    children: [
      jsx(Field, {
        label: `${T('ui.sessions')} (${sessions.length})`,
        more: [
          jsx(
            'div',
            {
              className: 'mb-1 flex items-center gap-1 text-[0.6875rem]',
              children: [
                ['project', T('ui.scope_project')],
                ['all', T('ui.scope_all')]
              ].map(([value, label]) =>
                jsx(
                  'button',
                  {
                    type: 'button',
                    disabled: value === 'project' && !project,
                    onClick: () => setScope(value),
                    className: `rounded px-1.5 py-0.5 disabled:opacity-40 ${effective === value ? 'bg-(--chrome-action-hover) text-foreground' : 'text-(--ui-text-tertiary) hover:text-foreground'}`,
                    children: label
                  },
                  value
                )
              )
            },
            'scope'
          ),
          list.isLoading ? jsx(Skeleton, { className: 'h-3' }, 'sk') : null,
          list.isError ? jsx('div', { className: 'text-[0.6875rem] text-amber-500', children: errText(list.error) }, 'err') : null,
          ...sessions.slice(0, 60).map(s => jsx(SessionRow, { s, meta: meta[s.id], current: s.id === storedId }, s.id)),
          sessions.length > 60 ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `… ${T('common.and_more', sessions.length - 60)}` }, 'more') : null
        ],
        children: first
          ? jsxs('span', {
              className: 'inline-flex min-w-0 max-w-full items-center gap-1.5 text-[0.6875rem]',
              children: [
                jsx('span', { className: 'min-w-0 truncate text-foreground', children: first.title || first.id }),
                jsx(CapacityBar, { ctx: meta[first.id]?.context })
              ]
            })
          : jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: list.isLoading ? '…' : T('common.none') })
      })
    ]
  })
}

function ExternalSection({ project }) {
  const storedId = useValue(host.state.focusedStoredSessionId)
  const list = useSessionList(project, project ? 'project' : 'all')
  const current = (list.data || []).find(s => s.id === storedId)
  const folders = (project?.folders || []).map(f => f.path)
  const channels = useQuery({
    queryKey: [PLUGIN_ID, 'channels', project?.id || '', project?.name || ''],
    queryFn: () => api.rest('/channels', { method: 'POST', body: { id: project?.id, name: project?.name, folders } }),
    enabled: Boolean(api && project),
    staleTime: 5 * 60_000
  })
  const meta = useSessionsMeta(storedId ? [storedId] : []).data?.sessions?.[storedId]
  const chans = channels.data?.channels || []
  const providers = meta?.providers || []
  const chatSource = current ? sourceLabel(current.handoff_platform || current.source) : null
  return jsx(Field, {
    label: T('ui.external_comm'),
    more: [
      jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: T('ui.chat_source', chatSource || T('ui.unknown_source')) + (current?.handoff_platform && current?.source && current.handoff_platform !== current.source ? ` (${T('ui.originally', sourceLabel(current.source))})` : '') }, 'src'),
      ...chans.map(c =>
        jsxs(
          'div',
          {
            className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem]',
            title: `${c.label} · ${c.channel_id}\n${c.summary}`,
            children: [
              jsx(Codicon, { name: c.platform === 'discord' ? 'comment-discussion' : 'broadcast', size: '0.75rem', className: 'shrink-0 text-(--ui-text-tertiary)' }),
              jsx('span', { className: 'shrink-0 text-foreground', children: c.label }),
              jsx('button', {
                type: 'button',
                title: T('ui.copy_channel_id'),
                onClick: () => void api?.copy(c.channel_id),
                className: 'shrink-0 font-mono text-(--ui-text-tertiary) hover:text-sky-500',
                children: `#${c.channel_id}`
              }),
              jsx('span', { className: 'min-w-0 truncate text-(--ui-text-tertiary)', children: c.summary })
            ]
          },
          `${c.platform}:${c.channel_id}`
        )
      ),
      !chans.length && project ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: T('ui.no_channel') }, 'none') : null,
      providers.length
        ? jsx(
            'div',
            {
              className: 'mt-1 text-[0.6875rem] text-(--ui-text-tertiary)',
              children: [
                T('ui.chat_providers'),
                ...providers.map(p => jsx('div', { className: 'truncate pl-2 font-mono text-foreground', children: `${p.provider} / ${p.model} – ${p.calls}×${p.source === 'usage' ? ` (${T('ui.from_statistics')})` : ''}` }, `${p.provider}/${p.model}`))
              ]
            },
            'prov'
          )
        : null
    ],
    children: jsxs('span', {
      className: 'inline-flex min-w-0 max-w-full items-center gap-1.5 text-[0.6875rem]',
      children: [
        jsx('span', { className: 'shrink-0 text-foreground', children: chatSource || '—' }),
        chans.length ? jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary)', children: `· ${T('ui.channels_count', chans.length)} (${[...new Set(chans.map(c => c.label))].join(', ')})` }) : null,
        providers.length
          ? jsx('span', { className: 'min-w-0 truncate text-(--ui-text-tertiary)', children: `· ${T('ui.providers_count', [...new Set(providers.map(p => p.provider))].length)}` })
          : null
      ]
    })
  })
}

// ── Hermes variables (HERMES_*, TERMINAL_*; no secrets) ─────────────────────

function EnvSection({ cwd }) {
  const storedId = useValue(host.state.focusedStoredSessionId)
  const query = useQuery({
    queryKey: [PLUGIN_ID, 'env'],
    queryFn: () => api.rest('/env', { timeoutMs: 15_000 }),
    enabled: Boolean(api),
    staleTime: 5 * 60_000
  })
  const d = query.data
  const core = d?.core || []
  const other = d?.other || []
  const home = core.find(c => c.name === 'HERMES_HOME')
  const profile = core.find(c => c.name === 'HERMES_PROFILE')
  const setCount = core.filter(c => c.set).length + other.length
  const row = (name, value, note, doc, tone) =>
    jsxs(
      'div',
      {
        className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem]',
        title: [name, doc].filter(Boolean).join('\n\n'),
        children: [
          jsx('span', { className: 'w-44 shrink-0 truncate font-mono text-(--ui-text-tertiary)', children: name }),
          jsx('span', { className: `min-w-0 flex-1 truncate font-mono ${tone || 'text-foreground'}`, children: value }),
          note ? jsx('span', { className: 'shrink-0 text-[0.625rem] text-(--ui-text-tertiary)', children: note }) : null
        ]
      },
      name
    )
  const heading = (text, key) => jsx('div', { className: 'mt-1.5 text-[0.6875rem] font-medium text-(--ui-text-tertiary)', children: text }, key)
  const more = []
  // what the next tool command of the focused chat receives
  more.push(heading(`${T('env.this_chat')} (${T('env.this_chat_note')})`, 'h-chat'))
  more.push(row('HERMES_SESSION_ID', storedId || '—', null, T('ui.env_session_doc')))
  more.push(row('TERMINAL_CWD', cwd || '—', null, T('ui.env_cwd_doc')))
  let group = null
  for (const c of core) {
    if (c.group === 'session') continue // per command, shown above
    if (c.group !== group) {
      group = c.group
      more.push(heading(L('env.group', group), `h-${group}`))
    }
    more.push(
      c.set
        ? row(c.name, c.value, T('ui.env_set'), c.doc)
        : c.resolved
          ? row(c.name, c.value, T('env.resolved'), c.doc)
          : row(c.name, T('env.unset_default', L('env.default', c.default_code || 'none')), null, c.doc, 'text-(--ui-text-tertiary)')
    )
  }
  if (other.length) {
    more.push(heading(`${T('env.other_set')} (${other.length})`, 'h-other'))
    for (const o of other) more.push(row(o.name, o.value, null, o.doc))
  }
  if (d?.hidden_secret_count) more.push(jsx('div', { className: 'mt-1 text-[0.625rem] text-(--ui-text-tertiary)', children: T('env.hidden_secrets', d.hidden_secret_count) }, 'hidden'))
  more.push(jsx('div', { className: 'mt-1 text-[0.625rem] text-(--ui-text-tertiary)', children: T('ui.env_note', d?.process || 'gateway') }, 'note'))
  return jsx(Section, {
    children: jsx(Field, {
      label: T('ui.variables'),
      more,
      children: jsx('span', {
        className: 'min-w-0 truncate text-[0.6875rem] text-foreground',
        children: query.isLoading
          ? '…'
          : query.isError
            ? errText(query.error)
            : [home ? tailPath(home.value || '', 28) : null, profile ? T('ui.profile_name', profile.value || profile.default_code) : null, T('ui.set_count', setCount)].filter(Boolean).join(' · ')
      })
    })
  })
}

// ── Disk Hermesa (read-only overview; nothing is deleted) ────────────────────

function gb(n) {
  n = n || 0
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`
  if (n >= 1024 ** 2) return `${Math.round(n / 1024 ** 2)} MB`
  return `${Math.round(n / 1024)} kB`
}

function ageDays(ts) {
  if (!ts) return '?'
  const d = (Date.now() / 1000 - ts) / 86400
  return d < 1 ? T('time.today') : T('time.days_short', Math.round(d))
}

function DiskSection() {
  const [refresh, setRefresh] = useState(0)
  const query = useQuery({
    queryKey: [PLUGIN_ID, 'disk', refresh],
    queryFn: () => api.rest(`/disk${refresh ? '?refresh=true' : ''}`, { timeoutMs: 20_000 }),
    enabled: Boolean(api),
    staleTime: 60_000,
    // poll while a background scan runs, otherwise rarely
    // React Query v5 passes the query, v4 the data — accept both
    refetchInterval: a => ((a?.state ? a.state.data : a)?.scanning ? 10_000 : 10 * 60_000)
  })
  const d = query.data
  const drive = d?.drive || {}
  const pct = drive.free_pct
  const tone = pct == null ? 'muted' : pct < 5 ? 'bad' : pct < 10 ? 'warn' : 'good'
  const scratch = d?.scratch || []
  const value = query.isLoading
    ? '…'
    : query.isError
      ? errText(query.error)
      : [
          drive.free != null ? T('ui.disk_free', gb(drive.free), pct) : null,
          d?.home_size ? `Hermes ${gb(d.home_size)}` : null,
          d?.scratch_size ? T('ui.disk_work', gb(d.scratch_size)) : null,
          d?.scanning && !d?.areas ? T('ui.computing') : null
        ]
          .filter(Boolean)
          .join(' · ')
  const more = [
    jsxs('div', {
      className: 'mb-1 flex items-center gap-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
      children: [
        jsx('span', { children: d?.scanned_at ? T('disk.scanned_at', clock(d.scanned_at)) + (d.duration ? ` (${d.duration} s)` : '') : T('disk.not_scanned') }),
        d?.scanning ? jsx('span', { className: 'text-amber-500', children: `· ${T('disk.rescanning')}…` }) : null,
        jsx('button', {
          type: 'button',
          disabled: d?.scanning,
          onClick: () => setRefresh(x => x + 1),
          className: 'ml-auto rounded border border-(--ui-border) px-1.5 py-0.5 text-foreground hover:bg-(--chrome-action-hover) disabled:opacity-40',
          children: T('ui.rescan')
        })
      ]
    }, 'head'),
    ...(d?.areas || []).slice(0, 8).map(a =>
      jsxs('div', {
        className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem]',
        children: [
          jsx('span', { className: 'w-16 shrink-0 text-right tabular-nums text-foreground', children: gb(a.size) }),
          jsx('span', { className: 'min-w-0 flex-1', children: jsx(FolderLink, { path: a.path, max: 30, title: `${a.path}\n${T('common.files_count', a.files)}` }) })
        ]
      }, a.path)
    ),
    scratch.length ? jsx('div', { className: 'mt-1.5 text-[0.6875rem] font-medium text-(--ui-text-tertiary)', children: T('ui.work_folders_count', scratch.length) }, 'sh') : null,
    ...scratch.slice(0, 40).map(s => {
      const who = s.sessions?.[0]
      const tip = [
        s.path,
        `${T('common.files_count', s.files)} · ${T('ui.last_change', s.newest ? new Date(s.newest * 1000).toLocaleString() : '?')}`,
        s.project_cwd ? T('ui.project_folder', s.project_cwd) : T('ui.origin_unknown'),
        ...(s.sessions || []).map(x => T('ui.session_writes', x.title || x.id, x.writes))
      ].join('\n')
      return jsxs('div', {
        className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem]',
        title: tip,
        children: [
          jsx('span', { className: 'w-16 shrink-0 text-right tabular-nums text-foreground', children: gb(s.size) }),
          jsx('span', { className: 'min-w-0 flex-1', children: jsx(FolderLink, { path: s.path, max: 32, title: tip }) }),
          jsx('span', { className: 'w-10 shrink-0 text-right text-(--ui-text-tertiary)', children: ageDays(s.newest) }),
          jsx('span', {
            className: 'w-24 shrink-0 truncate text-(--ui-text-tertiary)',
            children: s.project_cwd ? tailPath(s.project_cwd, 16) : who ? (who.title || '').slice(0, 16) : '—'
          })
        ]
      }, s.path)
    }),
    scratch.length > 40 ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `… ${T('common.and_more', scratch.length - 40)}` }, 'more') : null,
    jsx('div', { className: 'mt-1 text-[0.625rem] text-(--ui-text-tertiary)', children: T('ui.disk_note') }, 'note')
  ]
  return jsx(Section, {
    children: jsx(Field, {
      label: T('disk.title'),
      more,
      children: jsxs('span', {
        className: 'inline-flex min-w-0 max-w-full items-center gap-1.5 text-[0.6875rem]',
        children: [jsx(StatusDot, { tone }), jsx('span', { className: 'min-w-0 truncate text-foreground', children: value })]
      })
    })
  })
}

// ── Honcho Memory ───────────────────────────────────────────────────────────


function HonchoSection() {
  const [check, setCheck] = useState(false)
  const query = useQuery({
    queryKey: [PLUGIN_ID, 'honcho', check],
    queryFn: () => api.rest(`/honcho${check ? '?check=true' : ''}`, { timeoutMs: 20_000 }),
    enabled: Boolean(api),
    staleTime: 60_000,
    refetchInterval: 5 * 60_000
  })
  const d = query.data
  const cfg = d?.config || {}
  const conn = d?.connection
  const logs = d?.logs || {}
  const problems = logs.problems || []
  const line = (k, v) => jsxs('div', {
    className: 'flex min-w-0 gap-2 text-[0.6875rem]',
    children: [jsx('span', { className: 'w-28 shrink-0 text-(--ui-text-tertiary)', children: k }), jsx('span', { className: 'min-w-0 truncate font-mono text-foreground', title: String(v), children: String(v) })]
  }, k)
  let value
  let tone = 'muted'
  if (query.isLoading) value = '…'
  else if (query.isError) value = errText(query.error)
  else if (!d?.available) value = 'not available'
  else {
    tone = !cfg.active ? 'muted' : conn && !conn.ok ? 'bad' : problems.length ? 'warn' : 'good'
    value = cfg.active
      ? [cfg.settings?.workspace, cfg.settings?.sessionStrategy, cfg.settings?.recallMode].filter(Boolean).join(' · ') || T('honcho.active')
      : T('honcho.installed_other', cfg.provider || T('common.none'))
  }
  const more = !d?.available
    ? [jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: cfg.provider ? T('honcho.active_provider', cfg.provider) : T('honcho.no_provider') }, 'na')]
    : [
        ...Object.entries(cfg.settings || {}).map(([k, v]) => line(L('honcho.field', k), v)),
        line(T('honcho.api_key'), cfg.has_api_key ? T('honcho.api_key_set') : T('honcho.api_key_missing')),
        line(T('honcho.plugin'), cfg.plugin_enabled ? T('state.enabled') : cfg.plugin_installed ? T('state.disabled') : T('state.not_installed')),
        line(T('honcho.desktop_plugin'), cfg.desktop_plugin_enabled ? T('state.enabled') : cfg.desktop_plugin_installed ? T('state.disabled') : T('state.not_installed')),
        jsxs('div', {
          className: 'mt-1 flex items-center gap-1.5 text-[0.6875rem]',
          children: [
            jsx('button', {
              type: 'button',
              onClick: () => (check ? void query.refetch() : setCheck(true)),
              className: 'rounded border border-(--ui-border) px-1.5 py-0.5 text-foreground hover:bg-(--chrome-action-hover)',
              children: query.isFetching && check ? T('ui.checking') : T('ui.check_connection')
            }),
            conn
              ? jsx('span', {
                  className: conn.ok ? 'text-emerald-500' : 'text-destructive',
                  title: conn.ok ? T('ui.anonymous_check_tip') : conn.error,
                  children: conn.ok ? T('honcho.reachable', conn.status, conn.ms) : `${L('honcho.stage', conn.stage)}: ${conn.error}`
                })
              : null
          ]
        }, 'conn'),
        jsx('div', { className: 'mt-1 text-[0.6875rem] text-(--ui-text-tertiary)', children: T('honcho.log_counts', Object.entries(logs.counts || {}).map(([k, v]) => `${k} ${v}`).join(', ') || T('common.none')) }, 'counts'),
        ...(logs.session_keys || []).slice(0, 8).map(s => jsx('div', { className: 'truncate pl-2 font-mono text-[0.6875rem] text-foreground', title: s.key, children: `${s.key} · ${clock(s.last_ts)}` }, s.key)),
        ...problems.slice(0, 10).map((e, i) => jsx('div', { className: 'truncate text-[0.6875rem] text-amber-500', title: e.message, children: `⚠ ${clock(e.ts)} ${e.level}: ${e.message}` }, `p${i}`)),
        ...(logs.recent || []).slice(0, 6).map((e, i) => jsx('div', { className: 'truncate text-[0.625rem] text-(--ui-text-tertiary)', title: e.message, children: `${clock(e.ts)} ${e.message}` }, `r${i}`))
      ]
  return jsx(Section, {
    children: jsx(Field, {
      label: T('honcho.section'),
      more,
      children: jsxs('span', {
        className: 'inline-flex min-w-0 max-w-full items-center gap-1.5 text-[0.6875rem]',
        children: [jsx(StatusDot, { tone }), jsx('span', { className: `min-w-0 truncate ${d?.available ? 'text-foreground' : 'text-(--ui-text-tertiary)'}`, children: value })]
      })
    })
  })
}

// ── LLM communication (provider calls) ────────────────────────────────

function tok(n) {
  n = n || 0
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} k`
  return String(n)
}

function LlmProvider({ p }) {
  return jsx(Field, {
    label: p.provider,
    more: [
      ...p.models.map(m =>
        jsxs(
          'div',
          {
            className: 'flex min-w-0 items-center gap-1.5 text-[0.6875rem]',
            children: [
              jsx(StatusDot, { tone: m.errors ? 'warn' : 'good' }),
              jsx('span', { className: 'min-w-0 flex-1 truncate font-mono text-foreground', children: m.model }),
              jsx('span', {
                className: 'shrink-0 tabular-nums text-(--ui-text-tertiary)',
                children: `${m.calls}× · ${tok(m.input_tokens)}→${tok(m.output_tokens)}${m.avg_duration != null ? ` · ⌀${m.avg_duration}s` : ''}${m.errors ? ` · ❌ ${m.errors}` : ''}`
              })
            ]
          },
          m.model
        )
      ),
      p.aux?.length
        ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: T('llm.aux', p.aux.map(a => `${a.aux_task} ${a.calls}×`).join(', ')) }, 'aux')
        : null,
      ...(p.hosts || []).slice(0, 3).map(h => jsx('div', { className: 'truncate font-mono text-[0.6875rem] text-(--ui-text-tertiary)', title: h, children: h }, h))
    ],
    children: jsxs('span', {
      className: 'inline-flex min-w-0 max-w-full items-center gap-1.5 text-[0.6875rem]',
      children: [
        jsx(StatusDot, { tone: p.errors ? 'warn' : 'good' }),
        jsx('span', { className: 'shrink-0 tabular-nums text-foreground', children: `${p.calls}×` }),
        jsx('span', { className: 'min-w-0 truncate tabular-nums text-(--ui-text-tertiary)', children: `in ${tok(p.input_tokens)} · cache ${tok(p.cache_read_tokens)} · out ${tok(p.output_tokens)}` })
      ]
    })
  })
}

function LlmSection() {
  const sessionId = useValue(host.state.focusedStoredSessionId)
  const busy = useValue(host.state.busy)
  const [scope, setScope] = useState('session')
  const bySession = scope === 'session' && Boolean(sessionId)
  const hours = scope === '7d' ? 168 : 24
  const query = useQuery({
    queryKey: [PLUGIN_ID, 'llm', scope, sessionId],
    queryFn: () => api.rest(bySession ? `/llm?session=${q(sessionId)}` : `/llm?hours=${hours}`, { timeoutMs: 30_000 }),
    enabled: Boolean(api),
    refetchInterval: 30_000,
    staleTime: 10_000
  })
  const wasBusy = useRef(busy)
  useEffect(() => {
    if (wasBusy.current && !busy) void queryClient.invalidateQueries({ queryKey: [PLUGIN_ID, 'llm'] })
    wasBusy.current = busy
  }, [busy])
  const d = query.data
  const t = d?.total
  const errors = d?.errors || []
  return jsxs(Section, {
    title: T('llm.title'),
    children: [
      jsxs('div', {
        className: 'mb-1 flex items-center gap-1 text-[0.6875rem]',
        children: [
          ...[
            ['session', T('ui.this_chat')],
            ['24h', '24 h'],
            ['7d', T('ui.days_n', 7)]
          ].map(([value, label]) =>
            jsx(
              'button',
              {
                type: 'button',
                onClick: () => setScope(value),
                className: `rounded px-1.5 py-0.5 ${scope === value ? 'bg-(--chrome-action-hover) text-foreground' : 'text-(--ui-text-tertiary) hover:text-foreground'}`,
                children: label
              },
              value
            )
          ),
          d
            ? jsx(Tip, {
                label: d.capture_content ? T('llm.content_on') : T('ui.content_off_tip'),
                children: jsx('span', { className: 'ml-auto text-(--ui-text-tertiary)', children: d.capture_content ? `● ${T('ui.content')}` : `○ ${T('ui.metadata_only')}` })
              })
            : null
        ]
      }),
      query.isLoading
        ? jsx(Skeleton, { className: 'h-4' })
        : query.isError
          ? jsx('div', { className: 'text-xs text-amber-500', children: errText(query.error) })
          : !t?.calls
            ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: d?.enabled === false ? T('llm.disabled') : T('llm.empty') })
            : jsxs('div', {
                children: [
                  jsx(Field, {
                    label: T('ui.total'),
                    children: jsx('span', {
                      className: 'text-[0.6875rem] tabular-nums',
                      children: `${T('ui.calls_count', t.calls)} · in ${tok(t.input_tokens)} · cache ${tok(t.cache_read_tokens)} · out ${tok(t.output_tokens)}${t.reasoning_tokens ? ` · reasoning ${tok(t.reasoning_tokens)}` : ''}`
                    })
                  }),
                  ...(d.providers || []).map(p => jsx(LlmProvider, { p }, p.provider)),
                  errors.length
                    ? jsx(Field, {
                        label: T('ui.errors'),
                        more: errors.map(e =>
                          jsx(
                            'div',
                            {
                              className: 'truncate text-[0.6875rem] text-(--ui-text-tertiary)',
                              title: e.error_message || '',
                              children: `${clock(e.ts)} ${e.provider}/${e.model}${e.status_code ? ` ${e.status_code}` : ''}: ${e.error_message || e.error_type || ''}`
                            },
                            e.id
                          )
                        ),
                        children: jsx('span', { className: 'text-[0.6875rem] text-amber-500', children: `${errors.length}${errors.length >= 15 ? '+' : ''} · ${T('ui.last_error', clock(errors[0].ts))}` })
                      })
                    : null
                ]
              })
    ]
  })
}

// ── pane ────────────────────────────────────────────────────────────────────

function RepoPane() {
  usePluginI18n(PLUGIN_ID) // re-render the whole pane when the display language changes
  const r = useRepo()
  const { cwd, project, local, data, isRepo, remoteCheck, summary } = r

  const header = jsxs('div', {
    className: 'flex items-center gap-2 px-3 py-2',
    children: [
      jsx(StatusDot, { tone: summary?.tone || 'muted' }),
      jsxs('div', {
        className: 'min-w-0 flex-1',
        children: [
          jsxs('div', {
            className: 'truncate text-xs',
            children: [
              jsx('span', { className: 'text-(--ui-text-tertiary)', children: `${T('ui.project')}: ` }),
              jsx('span', { className: 'font-medium text-foreground', children: project?.name || (r.resolving ? '…' : T('ui.no_project')) })
            ]
          }),
          jsx('div', {
            className: 'truncate text-[0.6875rem] text-(--ui-text-tertiary)',
            children: !cwd
              ? r.resolving
                ? T('ui.finding_folder')
                : T('ui.no_working_folder')
              : !isRepo
                ? local.isLoading
                  ? T('ui.loading')
                  : T('common.outside_git')
                : summary.current
                  ? T('verdict.all_current')
                  : summary.reasons.map(x => L('reason', x)).join(' · ')
          })
        ]
      }),
      jsx(Tip, {
        label: T('ui.refresh_check'),
        children: jsx(Button, {
          variant: 'ghost',
          size: 'icon-xs',
          'aria-label': T('ui.refresh'),
          disabled: local.isFetching,
          onClick: () => void refreshAll(cwd, true),
          children: jsx(Codicon, { name: 'refresh', size: '0.875rem', className: local.isFetching ? 'animate-spin' : '' })
        })
      })
    ]
  })

  let body = null
  if (cwd && local.isLoading) {
    body = jsx('div', { className: 'space-y-2 px-3 py-3', children: [0, 1, 2].map(i => jsx(Skeleton, { className: 'h-4' }, i)) })
  } else if (cwd && local.isError) {
    body = jsx('div', {
      className: 'px-3 py-3 text-xs text-destructive',
      children: T('ui.backend_unavailable', errText(local.error))
    })
  } else if (isRepo) {
    body = jsxs(Section, {
      children: [jsx(RemoteSection, { data }), jsx(GitSection, { data, remoteCheck }), jsx(UncommittedSection, { data }), jsx(WorkFilesSection, { data })]
    })
  } else if (!cwd && !project) {
    body = jsx('div', { className: 'px-3 py-3 text-xs text-(--ui-text-tertiary)', children: T('ui.open_chat_hint') })
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      header,
      jsxs('div', {
        className: 'min-h-0 flex-1 overflow-y-auto',
        children: [
          jsx(ProjectSection, { project, gitByPath: r.gitByPath, repoRoot: isRepo ? data.repo.root : null, cwd, cwdSource: r.cwdSource }),
          body,
          cwd || project ? jsxs(Section, { children: [jsx(ExternalSection, { project }), jsx(SessionsSection, { project })] }) : null,
          cwd ? jsx(HermesChanges, { cwd }) : null,
          jsx(HermesSettings, {}),
          jsx(HonchoSection, {}),
          jsx(DiskSection, {}),
          jsx(EnvSection, { cwd }),
          jsx(LlmSection, {})
        ]
      })
    ]
  })
}

function RepoChip() {
  usePluginI18n(PLUGIN_ID)
  const { isRepo, data, summary, remoteCheck } = useRepo()
  const settings = useQuery({ queryKey: [PLUGIN_ID, 'settings'], queryFn: () => api.rest('/settings'), enabled: Boolean(api), staleTime: 60_000 })
  if (!isRepo || settings.data?.show_chip === false) return null
  const br = data.branch || {}
  const ch = data.changes || {}
  const ahead = remoteCheck?.ahead ?? br.ahead
  const behind = remoteCheck?.behind ?? br.behind
  const label = [br.detached ? 'detached' : br.head, ch.total ? `±${ch.total}` : null, ahead ? `↑${ahead}` : null, behind ? `↓${behind}` : null]
    .filter(Boolean)
    .join(' ')
  const tip = summary.current ? `Repo: ${T('verdict.all_current')}` : `Repo: ${summary.reasons.map(r => L('reason', r)).join(', ')} – ${T('ui.click_opens_diff')}`
  return jsx(Tip, {
    label: tip,
    children: jsxs('button', {
      type: 'button',
      'aria-label': tip,
      onClick: () => api?.runAction('view.toggleReview'),
      className:
        'inline-flex h-full items-center gap-1.5 rounded-none px-1.5 text-[0.6875rem] tabular-nums text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground',
      children: [jsx(StatusDot, { tone: summary.tone }), jsx(Codicon, { name: 'git-branch', size: '0.75rem' }), jsx('span', { children: label })]
    })
  })
}

export default {
  id: PLUGIN_ID,
  name: 'Repo Status',
  description: 'Project, folders, external repo, git state, uncommitted files, where Hermes wrote, settings, LLM calls, Honcho, disk and variables - for the focused chat.',
  register(ctx) {
    // Strings: English inline (EN), other Hermes languages from the backend catalog.
    ctx.i18n?.register?.({ en: toMessages(EN) })
    ctx
      .rest('/i18n', { timeoutMs: 15_000 })
      .then(catalogs => {
        const bundles = {}
        for (const [lang, flat] of Object.entries(catalogs?.catalogs || {})) {
          if (lang !== 'en') bundles[lang] = toMessages(flat)
        }
        if (Object.keys(bundles).length) ctx.i18n?.register?.(bundles)
      })
      .catch(() => {}) // older backend without /i18n: the panel stays English
    api = {
      t: ctx.i18n?.t ? (key, ...args) => ctx.i18n.t(key, ...args) : null,
      rest: (path, opts) => ctx.rest(path, opts),
      openExternal: url => ctx.os.openExternal(url),
      revealPath: path => ctx.os.revealPath(path),
      copy: text => ctx.os.writeClipboard(text),
      runAction: id => ctx.runAction?.(id)
    }
    ctx.onDispose(() => {
      api = null
    })
    ctx.onEvent(`plugin.${PLUGIN_ID}.settings.changed`, () => {
      void queryClient.invalidateQueries({ queryKey: [PLUGIN_ID] })
    })

    ctx.registerMany([
      {
        id: 'pane',
        area: PANES_AREA,
        title: 'Repo',
        data: { placement: 'right', width: '360px' },
        render: () => jsx(RepoPane, {})
      },
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 90,
        render: () => jsx(RepoChip, {})
      },
      {
        id: 'refresh',
        area: PALETTE_AREA,
        data: {
          id: 'repo-status.refresh',
          label: 'Repo Status: refresh and check server',
          keywords: ['git', 'repo', 'status', 'branch', 'vetva', 'projekt'],
          run: () => void refreshAll(host.state.cwd.get() || lastCwd, true)
        }
      }
    ])
  }
}

// @@EN-BEGIN (generated by scripts/sync_locales.py from locales/en.json - do not edit)
const EN = {
  "time.never": "never",
  "time.seconds_ago": "{0} s ago",
  "time.minutes_ago": "{0} min ago",
  "time.hours_ago": "{0} h ago",
  "time.days_ago": "{0} days ago",
  "time.today": "today",
  "time.days_short": "{0} d",
  "common.none": "none",
  "common.and_more": "and {0} more",
  "common.files_count": "files: {0}",
  "common.outside_git": "Outside any git repository",
  "common.outside_git_short": "Outside git",
  "state.enabled": "enabled",
  "state.disabled": "disabled",
  "state.not_installed": "not installed",
  "scope.this_session": "this session",
  "scope.last_24h_unknown_session": "last 24 h (session unknown)",
  "scope.all_sessions_window": "all sessions, last {0}",
  "scope.last_window": "last {0}",
  "error.not_a_repo": "not inside a git repository",
  "error.bare_repo": "a bare repository has no working tree",
  "error.git_error": "git error",
  "error.internal_error": "internal error",
  "verdict.all_current": "everything committed and up to date",
  "verdict.committed_unverified": "everything committed (server not checked)",
  "reason.uncommitted": "uncommitted changes",
  "reason.unpushed": "unpushed commits",
  "reason.server_newer": "the server has newer changes",
  "reason.not_on_remote": "branch not on the server",
  "reason.no_upstream": "no upstream",
  "reason.remote_unreachable": "server not checked",
  "reason.conflicts": "conflicts",
  "remote.up_to_date": "✅ up to date – the server has the same commit",
  "remote.local_ahead": "⬆️ local commits are not on the server (push needed)",
  "remote.behind": "⬇️ the server has newer commits",
  "remote.diverged": "⚠️ the branch diverged from the server",
  "remote.remote_newer": "⬇️ the server has a newer commit (count unknown without fetch)",
  "remote.not_on_remote": "⚠️ the branch does not exist on the server",
  "remote.no_upstream": "⚠️ the branch has no upstream",
  "remote.detached": "⚠️ detached HEAD",
  "remote.no_remote": "— the repository has no remote",
  "remote.skipped": "— server check switched off",
  "remote.error": "❌ the server could not be checked",
  "remote_short.up_to_date": "up to date",
  "remote_short.local_ahead": "push needed",
  "remote_short.behind": "server is newer",
  "remote_short.diverged": "diverged from server",
  "remote_short.remote_newer": "server is newer",
  "remote_short.not_on_remote": "branch not on server",
  "remote_short.no_upstream": "no upstream",
  "remote_short.detached": "detached HEAD",
  "remote_short.no_remote": "no remote",
  "remote_short.skipped": "not checked",
  "remote_short.not_a_repo": "—",
  "remote_short.error": "server not checked",
  "kind.github": "GitHub",
  "kind.gitlab": "GitLab",
  "kind.forgejo": "Forgejo/Gitea",
  "kind.bitbucket": "Bitbucket",
  "kind.azure": "Azure DevOps",
  "kind.local": "local folder",
  "kind.other": "other",
  "gitscope.local": "local (this repository only)",
  "gitscope.global": "global",
  "gitscope.system": "system",
  "gitscope.worktree": "worktree",
  "gitscope.command": "command line",
  "changes.staged": "staged",
  "changes.unstaged": "modified",
  "changes.untracked": "new",
  "changes.conflicted": "conflicts",
  "repo.local": "Local repository",
  "repo.external": "External repository",
  "repo.no_remote": "none (local only)",
  "repo.user_not_set": "not set (user.name / user.email)",
  "repo.branch": "Branch",
  "repo.detached_at": "detached HEAD at `{0}`",
  "repo.no_upstream": "no upstream",
  "repo.last_commit": "Last commit",
  "repo.no_commit": "no commit yet",
  "repo.checked": "checked {0}",
  "repo.last_fetch": "Last fetch",
  "repo.uncommitted": "Uncommitted files",
  "repo.binary": "binary",
  "repo.work_files": "Hermes work files",
  "repo.work_files_note": "not part of the verdict, not code changes",
  "repo.diffstat": "files: {0}, lines +{1} −{2} (new files are not in the diff)",
  "action.write": "write",
  "action.patch": "edit",
  "action.create": "new",
  "action.delete": "deleted",
  "action.command": "command",
  "action.code": "code",
  "touched.title": "Where Hermes wrote",
  "touched.empty": "The ledger has no writes yet. (It records from the moment the plugin is enabled.)",
  "touched.outside_count": "places outside the session's working folder: {0}",
  "touched.outside_flag": "outside cwd",
  "touched.uncommitted_now": "uncommitted now: {0}",
  "touched.still_uncommitted": "uncommitted",
  "touched.committed_or_reverted": "committed or reverted",
  "touched.path_guessed": "path guessed",
  "touched.probable_note": "probable – files unknown",
  "touched.legend": "✍️ documented writes (file tools) · ⌨️ commands – changed files cannot be determined",
  "env.title": "Hermes variables",
  "env.process": "process: {0}",
  "env.process_cli": "terminal (hermes CLI)",
  "env.this_chat": "This chat",
  "env.this_chat_note": "every tool command receives these",
  "env.resolved": "resolved",
  "env.unset_default": "not set → {0}",
  "env.other_set": "Other variables set",
  "env.hidden_secrets": "Hidden secrets: {0} (keys, tokens and passwords are never shown)",
  "env.group.base": "Base",
  "env.group.session": "Session",
  "env.group.kanban": "Kanban",
  "env.group.behavior": "Behavior",
  "env.default.terminal_cwd": "terminal.cwd in config.yaml / launch folder",
  "env.default.per_command": "set for every tool command",
  "env.default.worker_only": "only in a worker process",
  "env.default.off": "off",
  "env.default.from_config": "from config.yaml",
  "env.default.none": "—",
  "disk.title": "Hermes disk",
  "disk.free": "Free space: {0} of {1} ({2} %)",
  "disk.scanning_now": "Folder sizes are being computed in the background – try `/repo disk` again in a minute.",
  "disk.not_scanned": "not computed yet",
  "disk.summary": "Hermes: {0} · work folders (cache\\scratch): {1} in {2} folders",
  "disk.scanned_at": "computed {0}",
  "disk.rescanning": "recomputing",
  "disk.areas": "Areas",
  "disk.largest_work": "Largest work folders",
  "disk.readonly_note": "Overview only – the plugin deletes nothing. Open a folder in Explorer from the Desktop panel.",
  "honcho.title": "Honcho memory",
  "honcho.section": "Honcho Memory",
  "honcho.active": "active",
  "honcho.active_provider": "active memory provider: {0}",
  "honcho.no_provider": "no external memory provider",
  "honcho.installed_other": "installed, but the active provider is {0}",
  "honcho.api_key": "API key",
  "honcho.api_key_set": "set (hidden)",
  "honcho.api_key_missing": "missing",
  "honcho.aliases": "user aliases: {0}",
  "honcho.plugin": "Plugin honcho",
  "honcho.desktop_plugin": "Desktop plugin",
  "honcho.connection": "Connection",
  "honcho.reachable": "server answers (HTTP {0}, {1} ms)",
  "honcho.log_counts": "Log (7 days): {0}",
  "honcho.session_keys": "Honcho sessions in the log: {0}",
  "honcho.field.baseUrl": "Server",
  "honcho.field.environment": "Environment",
  "honcho.field.workspace": "Workspace",
  "honcho.field.peerName": "User (peer)",
  "honcho.field.aiPeer": "AI peer",
  "honcho.field.sessionStrategy": "Session strategy",
  "honcho.field.recallMode": "Recall mode",
  "honcho.field.dialecticDynamic": "Dynamic dialectic",
  "honcho.field.writeFrequency": "Write frequency",
  "honcho.field.contextTokens": "Context (tokens)",
  "honcho.field.dialecticReasoningLevel": "Reasoning level",
  "honcho.field.enabled": "Enabled",
  "honcho.stage.config": "configuration",
  "honcho.stage.dns": "DNS",
  "honcho.stage.tcp": "connection",
  "honcho.stage.tls": "TLS",
  "honcho.stage.timeout": "timeout",
  "honcho.stage.network": "network",
  "honcho.stage.error": "error",
  "llm.title": "LLM communication",
  "llm.disabled": "Recording is off (setting llm_log).",
  "llm.empty": "No calls recorded. (Recording runs from the moment it was enabled.)",
  "llm.total": "Calls: {0} · input {1} · cache {2} · output {3} · errors {4}",
  "llm.provider_line": "calls: {0}, input {1}, output {2}",
  "llm.aux": "auxiliary: {0}",
  "llm.recent_errors": "Recent errors",
  "llm.content_on": "Call content: stored (details: hermes repo-status --llm-call ID)",
  "llm.content_off": "Call content: not stored – metadata only (setting llm_capture_content)",
  "settings.title": "Hermes settings changes",
  "settings.section": "Hermes settings",
  "settings.empty": "No changes to skills, memory, cron, config, SOUL or plugins.",
  "settings.baseline_note": "(the first file scan just saved a baseline; further changes show from now on)",
  "settings.unknown_origin": "unknown origin",
  "settings.hermes_file": "Hermes (file)",
  "settings.skills_catalog": "(skills catalog)",
  "settings.legend": "Hermes = proven by a tool call · unknown origin = file changed without a tool call (a person or another program)",
  "category.skill": "Skills",
  "category.memory": "Memory",
  "category.cron": "Scheduled jobs",
  "category.config": "Configuration",
  "category.agent": "Agent (SOUL)",
  "category.plugin": "Plugins",
  "category.hook": "Hooks",
  "settings_action.create": "new",
  "settings_action.modify": "changed",
  "settings_action.delete": "deleted",
  "settings_action.patch": "edit",
  "settings_action.edit": "edit",
  "settings_action.write_file": "write",
  "settings_action.write": "write",
  "settings_action.add": "added",
  "settings_action.remove": "removed",
  "settings_action.replace": "replaced",
  "settings_action.update": "updated",
  "settings_action.pause": "paused",
  "settings_action.resume": "resumed",
  "settings_action.run": "run",
  "import.title": "History import into the ledger",
  "import.dry_run": "dry run, nothing written",
  "import.since": "Since: {0}",
  "import.profile": "Profile {0} – {1}",
  "import.untested_schema": "state.db schema version {0} was not tested with the plugin (tested: {1})",
  "import.counts": "tool calls: {0}, already in the ledger or duplicated in history: {1}, failed/without result: {2}",
  "import.new_rows": "new rows: {0}",
  "import.total": "Total new rows: {0}",
  "help.title": "git status of the session's working folder",
  "help.repo": "overview (repository, remote, git user, branch, server, uncommitted files, diff stat)",
  "help.path": "another folder",
  "help.check": "check the server again (no cache)",
  "help.local": "without checking the server",
  "help.touched": "where Hermes wrote: this session · all sessions 24 h · 7 days",
  "help.settings": "changes to skills, memory, cron, config, SOUL, plugins (default 7 d)",
  "help.llm": "LLM provider calls: models, tokens, timing, errors (default 24 h)",
  "help.honcho": "Honcho memory: active?, settings, connection, log events",
  "help.env": "Hermes variables (HERMES_*, TERMINAL_*) with descriptions, no secrets",
  "help.disk": "disk usage: Hermes and its work folders (cache\\scratch), overview only",
  "help.full_diff": "Full diff: `hermes repo-status --diff` in a terminal or the Review pane in Desktop.",
  "cli.disk_scanning": "Computing Hermes folder sizes (may take tens of seconds)…",
  "cli.llm_call_missing": "Call {0} does not exist.",
  "cli.llm_content_missing": "Content is not stored (setting llm_capture_content is off) or has expired.",
  "cli.not_stored": "(not stored)",
  "cli.no_diff": "(no changes against HEAD)",
  "cli.diff_truncated": "… diff truncated (256 KB limit)",
  "cli.excluded_patterns": "Excluded patterns: {0}",
  "source.cli": "Terminal",
  "source.tui": "Terminal",
  "source.cron": "Cron",
  "source.api": "API",
  "source.subagent": "Subagent",
  "source.delegate": "Subagent",
  "ui.server_check_failed": "Repo Status: the server could not be checked",
  "ui.backend_outdated": "The plugin backend is older than the panel – restart the Hermes gateway.",
  "ui.backend_unavailable": "Backend unavailable: {0}. Is the plugin enabled in config.yaml (plugins.enabled)?",
  "ui.collapse": "Collapse",
  "ui.expand": "Expand",
  "ui.open_in_explorer": "Open in Explorer: {0}",
  "ui.click_open_explorer": "Click: open in Explorer",
  "ui.primary_folder": "Primary folder",
  "ui.folder": "Folder",
  "ui.stored_folder": "stored folder",
  "ui.chat_git_repo": "Chat git repo",
  "ui.external_repo": "External repo",
  "ui.git_user": "Git user",
  "ui.not_set": "not set",
  "ui.checking": "checking…",
  "ui.checked_and_fetch": "checked {0} · last fetch {1}",
  "ui.work_files": "Hermes work",
  "ui.work_files_note": "Plans, sketches and notes Hermes keeps inside the repository. They are not code changes and do not count against \"everything committed\".",
  "ui.no_code_changes": "no code changes",
  "ui.tree_clean": "none – the working tree is clean",
  "ui.uncommitted": "Uncommitted",
  "ui.diff_in_review": "Diff in the Review pane",
  "ui.open_diff": "Open diff",
  "ui.lines_changed": "+{0} −{1} lines",
  "ui.outside_chat_folder": "Outside the chat's working folder",
  "ui.this_chat": "This chat",
  "ui.all_24h": "All 24 h",
  "ui.outside_cwd_count": "{0} outside cwd",
  "ui.touched_empty_chat": "Hermes has not written anything in this chat yet.",
  "ui.touched_empty_24h": "No writes in the last 24 h.",
  "ui.days_n": "{0} days",
  "ui.some_unattributed": "Some changes have no proven Hermes tool call",
  "ui.empty_folder": "empty folder",
  "ui.files": "Files",
  "ui.items_count": "items: {0}",
  "ui.capacity_unknown": "Capacity is recorded from LLM calls made after the plugin was enabled",
  "ui.capacity_tip": "Context: {0} / {1} tokens (latest call, {2})",
  "ui.click_switch_session": "Click: switch to this session",
  "ui.branch_lane": "branch {0}",
  "ui.scope_project": "Project",
  "ui.scope_all": "All",
  "ui.sessions": "Sessions",
  "ui.external_comm": "External comm.",
  "ui.chat_source": "This chat: {0}",
  "ui.unknown_source": "unknown source",
  "ui.originally": "originally {0}",
  "ui.copy_channel_id": "Copy channel ID",
  "ui.no_channel": "The project has no channel assigned (channel_prompts in config.yaml).",
  "ui.chat_providers": "LLM providers of this chat:",
  "ui.from_statistics": "statistics",
  "ui.channels_count": "channels: {0}",
  "ui.providers_count": "providers: {0}",
  "ui.env_session_doc": "Session ID of the current chat",
  "ui.env_cwd_doc": "Working folder of the chat",
  "ui.env_set": "set",
  "ui.env_note": "Values of the {0} process · descriptions from the Hermes documentation (tooltip)",
  "ui.variables": "Variables",
  "ui.profile_name": "profile {0}",
  "ui.set_count": "set: {0}",
  "ui.disk_free": "free {0} ({1} %)",
  "ui.disk_work": "work {0}",
  "ui.computing": "computing…",
  "ui.rescan": "Recompute",
  "ui.work_folders_count": "Work folders (cache\\scratch): {0}",
  "ui.last_change": "last change {0}",
  "ui.project_folder": "project: {0}",
  "ui.origin_unknown": "origin: not in the ledger",
  "ui.session_writes": "session: {0} (writes: {1})",
  "ui.disk_note": "Overview only – the plugin deletes nothing. Click opens the folder in Explorer.",
  "ui.check_connection": "Check connection",
  "ui.anonymous_check_tip": "Anonymous request without a key: HTTP 401/403 means the server runs and TLS is fine.",
  "ui.content_off_tip": "Metadata only; enable content in Settings → Plugins → repo-status",
  "ui.content": "content",
  "ui.metadata_only": "metadata only",
  "ui.total": "Total",
  "ui.calls_count": "calls: {0}",
  "ui.errors": "Errors",
  "ui.last_error": "latest {0}",
  "ui.project": "Project",
  "ui.no_project": "no project",
  "ui.finding_folder": "Finding the folder…",
  "ui.no_working_folder": "No working folder",
  "ui.loading": "Loading…",
  "ui.refresh_check": "Refresh and check the server",
  "ui.refresh": "Refresh",
  "ui.open_chat_hint": "Open a chat with a working folder or a project.",
  "ui.click_opens_diff": "click opens the diff"
}
// @@EN-END
