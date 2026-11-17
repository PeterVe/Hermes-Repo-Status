# Hermes Repo Status

An overview of where and what [Hermes Agent](https://github.com/NousResearch/hermes-agent) works on: the git repository of a chat, where Hermes wrote files, what it changed in its own settings, which LLM providers it talked to, the state of Honcho memory, disk usage and environment variables — as a Hermes Desktop panel, `/repo` chat commands (also on Discord, Telegram, …), a CLI and agent tools.

**Read-only:** the plugin never writes to repositories (no fetch, push, commit or checkout; `git status` runs with `--no-optional-locks`) and deletes nothing. Secrets (API keys, tokens, passwords) are never shown.

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

### Data

Each machine keeps its own records in `HERMES_HOME\plugin-shared\repo-status\` (`ledger.db`, `llm.db`, `disk.json`). Nothing is sent anywhere.

---

## What it shows

| Panel section | Chat / terminal | Content |
|---|---|---|
| **Project, Primary folder, Files** | `/repo` · `hermes repo-status [path]` | the chat's project, its folders (git badge, click opens Explorer), folder contents |
| **External repo, Branch, Git user** | `/repo`, `/repo check` | GitHub/GitLab/Forgejo/other (credentials hidden), branch and upstream, server state via `git ls-remote` (no fetch, 60 s cache), last commit, git identity and its scope |
| **Uncommitted, Hermes work** | `/repo` · `--diff` | uncommitted code changes with +/−; Hermes work files in `.hermes/` shown apart (they never count against "everything committed") |
| **External comm., Sessions** | – | where the chat comes from (Desktop/Discord/…), the project's channels, sessions newest first (click switches), context fill used/max, providers |
| **Where Hermes wrote** | `/repo touched [all\|7d]` · `--touched` | documented writes (`write_file`, `patch`, MCP), probable commands (`terminal`, `execute_code`), places outside the working folder ⚠️ |
| **Hermes settings** | `/repo settings [24h\|7d\|30d]` · `--settings` | changes to skills, memory, cron, `config.yaml`, `SOUL.md`, plugins, hooks; "unknown origin" = changed outside Hermes tools |
| **Honcho Memory** | `/repo honcho` · `--honcho` | active memory provider ("not available" otherwise), settings without the key, anonymous connection check (DNS/TLS/timeout), log events |
| **Hermes disk** | `/repo disk [refresh]` · `--disk` | free space, size of Hermes areas and `cache\scratch` work folders (with the project and session that created them); overview only |
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
- `.env`, `auth.json`, keys and lock files are never read or watched.

---

## Settings

Desktop **Settings → Plugins → repo-status** or `plugins.entries.repo-status.settings` in `config.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `check_remote` | `true` | check the server with `git ls-remote` |
| `remote_cache_seconds`, `remote_timeout_seconds` | 60, 8 | server check cache and timeout |
| `file_list_cap` | 200 | max. files in a list |
| `host_kinds` | `{}` | own servers, e.g. `{"git.example.com": "forgejo"}` |
| `work_dirs` | `[".hermes/"]` | Hermes work folders inside a repository |
| `show_chip` | `true` | status-bar chip |
| `track_writes`, `ledger_retention_days` | `true`, 90 | ledger of writes |
| `llm_log`, `llm_capture_content`, `llm_content_retention_days` | `true`, `false`, 7 | LLM call recording |

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
