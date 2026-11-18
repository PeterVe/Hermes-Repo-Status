# Hermes Repo Status

An overview of where and what [Hermes Agent](https://github.com/NousResearch/hermes-agent) works on: the git repository of a chat, where Hermes wrote files, what it changed in its own settings, which LLM providers it talked to, the state of Honcho memory, disk usage and environment variables — as a Hermes Desktop panel, `/repo` chat commands (also on Discord, Telegram, …), a CLI and agent tools.

**Repository-safe:** the plugin never writes to user repositories (no fetch, pull, push, commit or checkout; git runs with `--no-optional-locks`). **Project export is the only write-enabled project operation:** it writes only inside the configured `export_dir`, removes only its own marked work directory after verification, and optionally prunes its own older project archives. Other panels remain read-only; metadata bookkeeping stays in the plugin's shared data folder. Secrets are hidden in the UI and excluded from exports by default.

**Languages:** English, 简体中文, 繁體中文, 日本語, العربية, Русский, Français, Deutsch, Español — following the Hermes display language.

Requires **Hermes Agent 0.22.5+**.

---

## Install

On every machine running Hermes:

```powershell
hermes plugins install PeterVe/Hermes-Repo-Status --enable
hermes gateway restart
```

Then in Hermes Desktop: **Capabilities → Plugins → Rescan** and switch **Repo Status** on (the Desktop part is off by default). The **Repo** panel appears on the right.

Optionally fill in the last 90 days of history (reads `state.db` read-only):

```powershell
hermes repo-status --import-history
```

### Without GitHub: from a local or shared git repository

Hermes can install from a git repository on disk (e.g. a shared network drive). Only the **committed** state is installed, never work in progress:

```powershell
hermes plugins install file:///X:/path/to/Hermes-Repo-Status --enable
```

### Update

```powershell
hermes plugins check-updates
hermes plugins update repo-status
hermes gateway restart        # waits for live sessions; --yes interrupts them
```

Then **Capabilities → Plugins → Rescan** in Desktop.

The panel header shows `Panel x.y.z · backend x.y.z`. The line turns red when the gateway runs an older backend, did not load it, or does not respond, and amber when a profile (`hermes profile create --clone`) keeps an older copy of the plugin. Click it for the exact commands to fix it.

### Data

Each machine keeps its own records in `HERMES_HOME\plugin-shared\repo-status\` (`ledger.db`, `llm.db`, `disk.json`). Nothing is sent anywhere.

---

## What it shows

| Panel section | Chat / terminal | Content |
|---|---|---|
| **Project, Primary folder, Files** | `/repo` · `hermes repo-status [path]` | the chat's project, its folders (git badge, click opens Explorer), folder contents |
| **External repo, Branch, Git user** | `/repo`, `/repo check` | GitHub/GitLab/Forgejo/other (credentials hidden), branch and upstream, server state via `git ls-remote` (no fetch, 60 s cache), last commit, git identity and its scope |
| **Server branches** | `/repo branches [check]` · `--branches` | every branch on the server (`git ls-remote`, no fetch): default, current, local copy, ahead/behind, last commit, link to the web, open pull requests via `gh` (GitHub); branches that exist only locally |
| **Uncommitted, Hermes work** | `/repo` · `--diff` | uncommitted code changes with +/−; Hermes work files in `.hermes/` shown apart (they never count against "everything committed") |
| **External comm., Sessions** | – | where the chat comes from (Desktop/Discord/…), the project's channels, sessions newest first (click switches), context fill used/max, providers |
| **Where Hermes wrote** | `/repo touched [all\|7d]` · `--touched` | documented writes (`write_file`, `patch`, MCP), probable commands (`terminal`, `execute_code`), places outside the working folder ⚠️ |
| **Hermes settings** | `/repo settings [24h\|7d\|30d]` · `--settings` | changes to skills, memory, cron, `config.yaml`, `SOUL.md`, plugins, hooks; "unknown origin" = changed outside Hermes tools |
| **Honcho Memory** | `/repo honcho` · `--honcho` | active memory provider ("not available" otherwise), settings without the key, anonymous connection check (DNS/TLS/timeout), log events |
| **Hermes disk** | `/repo disk [refresh]` · `--disk` | free space, size of Hermes areas and `cache\scratch` work folders (with the project and session that created them); overview only |
| **Export** | `/repo export [status\|dry]` · `--export [--dry-run]` | current project folders as a verified ZIP, size estimate, background progress, errors and recent archives (click reveals in Explorer); disabled until `export_dir` is set |
| **Variables** | `/repo env` · `--env` | `HERMES_*` / `TERMINAL_*` with descriptions from the Hermes docs; secrets never returned |
| **LLM communication** | `/repo llm [session\|24h\|7d]` · `--llm`, `--llm-call ID` | provider calls: models, tokens, timing, errors, endpoints; content only on request |

Agent tools: `repo_status`, `hermes_touched_paths`. The status-bar chip shows the branch and ±changes (click opens Review/diff). CLI output language: `--lang de` (default: Hermes display language).

### Evidence levels (Where Hermes wrote, Hermes settings)

| Level | Source |
|---|---|
| ✍️ documented | a file-tool call, or `skill_manage` / `memory` / `cronjob_manage` |
| ⌨️ probable | `terminal`, `execute_code` – command and folder known, files not |
| observed | git shows the file uncommitted, or a settings file changed |

### Privacy

- The ledger stores paths and short commands only (tokens and passwords masked), never file, skill or memory content.
- LLM recording keeps metadata by default. Call content (`llm_capture_content`) is off; when switched on it is kept 7 days and secrets are masked.
- Normal monitoring never reads or watches `.env`, `auth.json`, keys or lock files. Exports exclude secret-file patterns by default; `export_include_secrets: true` explicitly opts into including secret files. Treat such archives as sensitive. Environment snapshots remain masked.

---

## Settings

Desktop **Settings → Plugins → repo-status** or `plugins.entries.repo-status.settings` in `config.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `check_remote` | `true` | check the server with `git ls-remote` |
| `branches_show_prs` | `true` | open pull requests at server branches (GitHub CLI `gh`, when signed in) |
| `remote_cache_seconds`, `remote_timeout_seconds` | 60, 8 | server check cache and timeout |
| `file_list_cap` | 200 | max. files in a list |
| `host_kinds` | `{}` | own servers, e.g. `{"git.example.com": "forgejo"}` |
| `work_dirs` | `[".hermes/"]` | Hermes work folders inside a repository |
| `show_chip` | `true` | status-bar chip |
| `track_writes`, `ledger_retention_days` | `true`, 90 | ledger of writes |
| `llm_log`, `llm_capture_content`, `llm_content_retention_days` | `true`, `false`, 7 | LLM call recording |
| `export_dir` | `""` | absolute output folder; empty disables Export and Dry run |
| `export_include_secrets` | `false` | explicitly include secret files in exports; archives may contain credentials |
| `export_include_scratch` | `false` | include project-attributed `cache\scratch` contents; otherwise export only the size/path inventory |
| `export_scratch_cap_mb` | 500 | maximum scratch content to include, in MB |
| `export_keep_last` | 0 | retain this many archives per project; 0 never deletes old archives |

### Project export

Set `export_dir` in the plugin settings first. Hermes' generated `config_schema` settings form currently renders string settings as text inputs, not directory pickers. The Desktop SDK does offer `ctx.files.requestFolder()` for local plugin file-access grants, but that is not a backend settings picker and may refer to a different machine than a remote gateway. Enter the **gateway machine's absolute path** for `export_dir`.

The Desktop Export section follows the same resolved project and folders as the panel (via `projects.tree` / `projects.list`); without a project, the chat's working folder is used. **Dry run** estimates without writing export files. To avoid SQLite sidecar writes, dry-run reads checkpointed database state only and warns when an uncheckpointed WAL is present; its project/metadata estimate may therefore lag live changes. Real exports read private database/WAL snapshots inside the marked work directory and fail safely if the source changes during snapshot copying. **Export** starts one background job at a time, polls its progress, and refreshes the recent archives when it finishes. Errors preserve the work folder and provide a reveal link. The CLI runs in the foreground:

```powershell
hermes repo-status --export --dry-run
hermes repo-status --export
```

Archives are named `<project>_<YYYY-MM-DD_HHMM>_<COMPUTERNAME>.zip` (a collision suffix may be added). They contain project files, git bundles and uncommitted diffs, a manifest with sizes and SHA-256 hashes, restoration instructions, and Hermes metadata without conversation transcripts. With secret inclusion off, a bundle whose reachable history contains secret-file paths is **omitted in full**, with a manifest warning; bundles containing refs that peel to non-commit objects (trees/blobs) are also omitted because commit-history scanning cannot safely inspect them. Filtering only the working folder would otherwise leak historical credentials. Such exports preserve eligible working files but cannot restore that omitted git history. A server-unavailable warning does not prevent exporting local material. The backend checks free space, creates a `.zip.part`, verifies CRC/file counts/sizes/hashes and git bundles, and only then publishes the ZIP. Automatic cleanup is restricted to its own `.repo-status-export`-marked directory inside `export_dir\_work\`; failed work remains for inspection.

Desktop/backend contract: `POST /export` sends `{project, directories, dry_run, cwd}`; `GET /export/status` returns `enabled`, `export_dir` and the global job state/progress; `GET /export/list` returns `{exports: [{path, name, size, mtime}]}`. Explorer links use the Desktop SDK's `revealPath`; a remote gateway path must also be accessible from the desktop machine.

---

## Development

With a checkout on a local disk a link into `HERMES_HOME\plugins\repo-status` is enough. A checkout on a **network drive** cannot be linked (a Windows junction cannot point to the network; Hermes skips it with `WinError 4392`), so the script **copies** it:

```powershell
powershell -File scripts\sync-to-hermes.ps1     # after every change
hermes gateway restart                          # new backend / hooks
```

Texts live in `locales/<lang>.json` (flat keys, positional `{0}` placeholders). After editing `locales/en.json` run `python scripts/sync_locales.py` – the Desktop panel carries an inline English copy.

Tests and checks:

```powershell
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate .
```

What takes effect when: `desktop/plugin.js` after **Rescan** in Desktop; `dashboard/plugin_api.py`, hooks and tools after a **gateway restart**; `/repo` and the CLI immediately.

---

## License

[MIT](LICENSE) © 2026 Peter Vesely
