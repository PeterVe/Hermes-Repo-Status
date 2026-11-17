"""Text output for chat (Desktop, Discord, Telegram …) and the terminal, in the user's Hermes
language. All wording lives in ``locales/<lang>.json`` (see i18n.py)."""

from __future__ import annotations

import time as _t
from typing import Any

try:
    from .i18n import tr  # type: ignore[import-not-found]
except ImportError:  # flat layout (tests)
    from i18n import tr  # type: ignore[no-redef]

SETTINGS_CATEGORIES = ("skill", "memory", "cron", "config", "agent", "plugin", "hook")


def label(prefix: str, key: str) -> str:
    """Translated label for a code (``remote.up_to_date``); unknown codes show as themselves."""
    text = tr(f"{prefix}.{key}")
    return key if text == f"{prefix}.{key}" else text


def ago(seconds: int | None) -> str:
    if seconds is None:
        return tr("time.never")
    if seconds < 90:
        return tr("time.seconds_ago", seconds)
    minutes = seconds // 60
    if minutes < 90:
        return tr("time.minutes_ago", minutes)
    hours = minutes // 60
    if hours < 48:
        return tr("time.hours_ago", hours)
    return tr("time.days_ago", hours // 24)


def _clock(ts: float) -> str:
    lt = _t.localtime(ts)
    today = _t.localtime()
    return _t.strftime("%H:%M" if lt[:3] == today[:3] else "%d.%m. %H:%M", lt)


def gb(n: int | None) -> str:
    n = n or 0
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} MB"
    return f"{n / 1024:.0f} kB"


def tokens(n: int | None) -> str:
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} M"
    if n >= 1_000:
        return f"{n / 1_000:.1f} k"
    return str(n)


def _age(ts: float | None) -> str:
    if not ts:
        return "?"
    days = (_t.time() - ts) / 86400
    return tr("time.today") if days < 1 else tr("time.days_short", f"{days:.0f}")


def _head(title_key: str, scope: str = "") -> str:
    title = f"**{tr(title_key)}**"
    return f"{title} – {scope}" if scope else title


# ── touched ──────────────────────────────────────────────────────────────────


def render_touched(data: dict[str, Any], *, label_text: str = "", max_files: int = 25) -> str:
    groups = data.get("groups") or []
    head = _head("touched.title", label_text)
    if not groups:
        return head + "\n\n" + tr("touched.empty")
    lines = [head, ""]
    outside = sum(1 for g in groups if g.get("outside_cwd"))
    if outside:
        lines.append("⚠️ " + tr("touched.outside_count", outside))
        lines.append("")
    for g in groups:
        flag = "⚠️ " + tr("touched.outside_flag") + " · " if g.get("outside_cwd") else ""
        if g.get("in_repo"):
            branch = f" (`{g['branch']}`)" if g.get("branch") else ""
            dirty = g.get("uncommitted_total")
            dirty_txt = " · " + tr("touched.uncommitted_now", dirty) if dirty is not None else ""
            lines.append(f"{flag}**{g['root']}**{branch}{dirty_txt}")
        else:
            lines.append(f"{flag}**{tr('common.outside_git')}**")
        for f in g.get("files", [])[:max_files]:
            shown = f.get("rel_path") or f["path"]
            acts = "/".join(label("action", a) for a in f.get("actions", []))
            state = ""
            if f.get("still_uncommitted") is True:
                state = " · " + tr("touched.still_uncommitted")
            elif f.get("still_uncommitted") is False:
                state = " · " + tr("touched.committed_or_reverted")
            guess = " · " + tr("touched.path_guessed") if f.get("path_guessed") else ""
            lines.append(f"  ✍️ `{shown}` – {acts}, {_clock(f['last_ts'])}{state}{guess}")
        more = (g.get("file_count") or 0) - min(len(g.get("files", [])), max_files)
        if more > 0:
            lines.append("  … " + tr("common.and_more", more))
        for c in g.get("commands", [])[:5]:
            folder = f" (`{c['folder']}`)" if c.get("folder") and not g.get("in_repo") else ""
            lines.append(f"  ⌨️ {c['tool']}{folder}: `{c.get('detail') or ''}` – {_clock(c['ts'])} ({tr('touched.probable_note')})")
        lines.append("")
    lines.append(tr("touched.legend"))
    return "\n".join(lines).rstrip()


# ── env ──────────────────────────────────────────────────────────────────────


def render_env(data: dict[str, Any], *, with_docs: bool = False) -> str:
    lines = [f"**{tr('env.title')}** – {tr('env.process', data.get('process', '?'))}", ""]
    chat = data.get("chat")
    if chat:
        lines.append(f"**{tr('env.this_chat')}** ({tr('env.this_chat_note')})")
        for k, v in chat.items():
            lines.append(f"  `{k}` = `{v or '—'}`")
        lines.append("")
    group = None
    for item in data.get("core") or []:
        if item["group"] != group:
            group = item["group"]
            lines.append(f"**{label('env.group', group)}**")
        if item.get("set"):
            val = f"`{item['value']}`"
        elif item.get("resolved"):
            val = f"`{item['value']}` ({tr('env.resolved')})"
        else:
            val = tr("env.unset_default", label("env.default", item.get("default_code") or "none"))
        lines.append(f"  `{item['name']}` = {val}")
        if with_docs and item.get("doc"):
            lines.append(f"      {item['doc'][:160]}")
    other = data.get("other") or []
    if other:
        lines.append("")
        lines.append(f"**{tr('env.other_set')}** ({len(other)})")
        for item in other:
            lines.append(f"  `{item['name']}` = `{item['value']}`")
    if data.get("hidden_secret_count"):
        lines.append("")
        lines.append(tr("env.hidden_secrets", data["hidden_secret_count"]))
    return "\n".join(lines)


# ── disk ─────────────────────────────────────────────────────────────────────


def render_disk(data: dict[str, Any], *, max_scratch: int = 15) -> str:
    d = data.get("drive") or {}
    pct = d.get("free_pct")
    warn = " ⚠️" if pct is not None and pct < 10 else ""
    lines = [f"**{tr('disk.title')}**", ""]
    if d:
        lines.append(tr("disk.free", gb(d.get("free")), gb(d.get("total")), pct) + warn)
    if not data.get("areas"):
        lines.append(tr("disk.scanning_now") if data.get("scanning") else tr("disk.not_scanned"))
        return "\n".join(lines)
    lines.append(tr("disk.summary", gb(data.get("home_size")), gb(data.get("scratch_size")), len(data.get("scratch") or [])))
    lines.append(tr("disk.scanned_at", _t.strftime("%d.%m. %H:%M", _t.localtime(data.get("scanned_at") or 0)))
                 + (f" ({tr('disk.rescanning')})" if data.get("scanning") else ""))
    lines.append("")
    lines.append(f"**{tr('disk.areas')}**")
    for a in (data.get("areas") or [])[:8]:
        lines.append(f"  {gb(a['size']):>9}  {a['name']}  ({tr('common.files_count', a['files'])})")
    lines.append("")
    lines.append(f"**{tr('disk.largest_work')}**")
    for s in (data.get("scratch") or [])[:max_scratch]:
        who = ""
        if s.get("sessions"):
            t = s["sessions"][0].get("title") or s["sessions"][0]["id"]
            who = f" · {t[:50]}"
        proj = f" · {s['project_cwd']}" if s.get("project_cwd") else ""
        lines.append(f"  {gb(s['size']):>9}  `{s['name']}` · {_age(s.get('newest'))}{proj}{who}")
    lines.append("")
    lines.append(tr("disk.readonly_note"))
    return "\n".join(lines)


# ── honcho ───────────────────────────────────────────────────────────────────


def _plugin_state(enabled: bool, installed: bool) -> str:
    return tr("state.enabled") if enabled else tr("state.disabled") if installed else tr("state.not_installed")


def render_honcho(data: dict[str, Any]) -> str:
    cfg = data.get("config") or {}
    head = f"**{tr('honcho.title')}**"
    if not data.get("available"):
        prov = cfg.get("provider")
        return f"{head} – not available (" + (tr("honcho.active_provider", prov) if prov else tr("honcho.no_provider")) + ")"
    state = tr("honcho.active") if cfg.get("active") else tr("honcho.installed_other", cfg.get("provider") or tr("common.none"))
    lines = [f"{head} – {state}", ""]
    for k, v in (cfg.get("settings") or {}).items():
        lines.append(f"{label('honcho.field', k)}: `{v}`")
    key_state = tr("honcho.api_key_set") if cfg.get("has_api_key") else tr("honcho.api_key_missing")
    lines.append(f"{tr('honcho.api_key')}: {key_state} · {tr('honcho.aliases', cfg.get('alias_count', 0))}")
    lines.append(f"{tr('honcho.plugin')}: {_plugin_state(cfg.get('plugin_enabled'), cfg.get('plugin_installed'))}"
                 f" · {tr('honcho.desktop_plugin')}: {_plugin_state(cfg.get('desktop_plugin_enabled'), cfg.get('desktop_plugin_installed'))}")
    conn = data.get("connection")
    if conn:
        if conn.get("ok"):
            lines.append(f"{tr('honcho.connection')}: ✅ " + tr("honcho.reachable", conn.get("status"), conn.get("ms")))
        else:
            lines.append(f"{tr('honcho.connection')}: ❌ {label('honcho.stage', conn.get('stage') or 'error')}: {conn.get('error')}")
    logs = data.get("logs") or {}
    counts = logs.get("counts") or {}
    if counts:
        lines.append(tr("honcho.log_counts", ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))))
    keys = logs.get("session_keys") or []
    if keys:
        lines.append(tr("honcho.session_keys", ", ".join(f"`{k['key']}`" for k in keys[:6])))
    for e in (logs.get("problems") or [])[:6]:
        lines.append(f"  ⚠️ {_clock(e['ts'])} {e['level']} {e['message'][:140]}")
    return "\n".join(lines)


# ── llm ──────────────────────────────────────────────────────────────────────


def render_llm(data: dict[str, Any], *, label_text: str = "", max_models: int = 6) -> str:
    head = _head("llm.title", label_text)
    if not data.get("enabled", True):
        return head + "\n\n" + tr("llm.disabled")
    t = data.get("total") or {}
    if not t.get("calls"):
        return head + "\n\n" + tr("llm.empty")
    lines = [
        head,
        "",
        tr("llm.total", t["calls"], tokens(t["input_tokens"]), tokens(t["cache_read_tokens"]), tokens(t["output_tokens"]), t["errors"]),
        "",
    ]
    for p in data.get("providers") or []:
        dur = f" · ⌀ {p['avg_duration']} s" if p.get("avg_duration") is not None else ""
        err = f" · ❌ {p['errors']}" if p["errors"] else ""
        lines.append(f"**{p['provider']}** – " + tr("llm.provider_line", p["calls"], tokens(p["input_tokens"]), tokens(p["output_tokens"])) + dur + err)
        for m in p["models"][:max_models]:
            lines.append(f"  • `{m['model']}` – {m['calls']}× · {tokens(m['input_tokens'])} → {tokens(m['output_tokens'])}"
                         + (f" · ❌ {m['errors']}" if m["errors"] else ""))
        if p.get("aux"):
            lines.append("  " + tr("llm.aux", ", ".join(f"{a['aux_task']} {a['calls']}×" for a in p["aux"][:6])))
        for h in p.get("hosts", [])[:3]:
            lines.append(f"  endpoint: {h}")
        lines.append("")
    errs = data.get("errors") or []
    if errs:
        lines.append(f"**{tr('llm.recent_errors')}**")
        for e in errs[:8]:
            code = f" {e['status_code']}" if e.get("status_code") else ""
            lines.append(f"  ❌ {_clock(e['ts'])} {e['provider']}/{e['model']}{code}: {(e.get('error_message') or e.get('error_type') or '')[:120]}")
        lines.append("")
    lines.append(tr("llm.content_on") if data.get("capture_content") else tr("llm.content_off"))
    return "\n".join(lines).rstrip()


# ── settings ─────────────────────────────────────────────────────────────────


def render_settings(data: dict[str, Any], *, label_text: str = "", max_items: int = 15) -> str:
    cats = data.get("categories") or []
    head = _head("settings.title", label_text)
    if not cats:
        note = ""
        scan = data.get("scan") or {}
        if scan and not scan.get("changes") and not scan.get("skipped"):
            note = " " + tr("settings.baseline_note")
        return head + "\n\n" + tr("settings.empty") + note
    lines = [head, ""]
    for c in cats:
        lines.append(f"**{label('category', c['category'])}** – {c['item_count']}")
        for it in c["items"][:max_items]:
            acts = "/".join(label("settings_action", a) for a in it["actions"])
            if it["by_hermes"]:
                who = "Hermes"
            elif it["unattributed"]:
                who = tr("settings.unknown_origin")
            else:
                who = tr("settings.hermes_file")
            name = tr("settings.skills_catalog") if it["item"] == "(skills-catalog)" else it["item"]
            lines.append(f"  • `{name}` – {acts}, {_clock(it['last_ts'])} · {who}")
        more = c["item_count"] - min(len(c["items"]), max_items)
        if more > 0:
            lines.append("  … " + tr("common.and_more", more))
        lines.append("")
    lines.append(tr("settings.legend"))
    return "\n".join(lines).rstrip()


# ── repo status ──────────────────────────────────────────────────────────────


def _file_marker(f: dict[str, Any]) -> str:
    if f.get("conflicted"):
        return "‼"
    if f.get("untracked"):
        return "?"
    xy = f.get("xy", "..")
    return (xy[0] if xy[0] != "." else xy[1]) or "M"


def render(p: dict[str, Any], *, max_files: int = 30, show_diffstat: bool = True) -> str:
    if not p.get("ok"):
        err = p.get("error") or "internal_error"
        msg = label("error", err)
        detail = p.get("message") if err in ("git_error", "internal_error") else None
        return f"**Repo status:** {msg}" + (f" – {detail}" if detail else "") + (f" (`{p.get('path')}`)" if err == "not_a_repo" else "")

    repo = p["repo"]
    br = p.get("branch") or {}
    ch = p.get("changes") or {}
    rc = p.get("remote_check") or {}
    summary = p.get("summary") or {}
    lines: list[str] = []

    if summary.get("current") and rc.get("status") == "skipped":
        verdict = "✅ " + tr("verdict.committed_unverified")
    elif summary.get("current"):
        verdict = "✅ " + tr("verdict.all_current")
    else:
        verdict = "⚠️ " + ", ".join(label("reason", r) for r in summary.get("reasons", []))
    lines.append(f"**Repo status** – {verdict}")
    lines.append("")

    wt_note = " (git worktree)" if repo.get("is_worktree") else ""
    lines.append(f"**{tr('repo.local')}:** `{repo['root']}`{wt_note}")
    wts = repo.get("worktrees") or []
    if len(wts) > 1:
        lines.append(f"  worktrees: {len(wts)} – " + ", ".join(f"`{w.get('branch') or w.get('head', '')[:7]}`" for w in wts[:6]))

    remotes = p.get("remotes") or []
    if not remotes:
        lines.append(f"**{tr('repo.external')}:** {tr('repo.no_remote')}")
    for r in remotes:
        where = r.get("web_url") or r.get("url")
        lines.append(f"**{tr('repo.external')} ({r['name']}):** {label('kind', r['kind'])} – {where}")

    ident = p.get("identity") or {}
    name, email = ident.get("name"), ident.get("email")
    if name or email:
        scope = (email or name or {}).get("scope", "")
        who = f"{(name or {}).get('value', '?')} <{(email or {}).get('value', '?')}>"
        lines.append(f"**Git user:** {who} – {label('gitscope', scope)}")
    else:
        lines.append(f"**Git user:** ⚠️ {tr('repo.user_not_set')}")

    if br.get("detached"):
        lines.append(f"**{tr('repo.branch')}:** " + tr("repo.detached_at", (br.get("oid") or "")[:7]))
    else:
        up = f" → `{br['upstream']}`" if br.get("upstream") else f" ({tr('repo.no_upstream')})"
        lines.append(f"**{tr('repo.branch')}:** `{br.get('head')}`{up}")

    lc = p.get("last_commit")
    if lc:
        lines.append(f"**{tr('repo.last_commit')}:** `{lc['short']}` {lc['subject']} – {lc['author']}, {lc['date'][:16].replace('T', ' ')}")
    else:
        lines.append(f"**{tr('repo.last_commit')}:** {tr('repo.no_commit')}")
    rs = rc.get("status")
    if rs:
        extra = []
        if rs in ("local_ahead", "diverged") and rc.get("ahead"):
            extra.append(f"↑{rc['ahead']}")
        if rs in ("behind", "diverged") and rc.get("behind"):
            extra.append(f"↓{rc['behind']}")
        if rs == "error" and rc.get("error"):
            extra.append(rc["error"])
        if rc.get("cached"):
            extra.append(tr("repo.checked", ago(rc.get("age_seconds"))))
        lines.append(f"**Server:** {label('remote', rs)}" + (f" ({'; '.join(extra)})" if extra else ""))
    fetch = p.get("fetch") or {}
    if remotes:
        lines.append(f"**{tr('repo.last_fetch')}:** {ago(fetch.get('age_seconds'))}")

    total = ch.get("total") or 0
    if total == 0:
        lines.append(f"**{tr('repo.uncommitted')}:** {tr('common.none')}")
    else:
        parts = [f"{ch[key]} {tr('changes.' + key)}" for key in ("staged", "unstaged", "untracked", "conflicted") if ch.get(key)]
        lines.append(f"**{tr('repo.uncommitted')}:** {total} ({', '.join(parts)})")
        stat_by_path = {f["path"]: f for f in ((p.get("diffstat") or {}).get("files") or [])}
        files = ch.get("files") or []
        for f in files[:max_files]:
            st = stat_by_path.get(f["path"]) if show_diffstat else None
            delta = ""
            if st:
                delta = f" ({tr('repo.binary')})" if st.get("binary") else f" +{st['added']} −{st['removed']}"
            lines.append(f"  `{_file_marker(f)}` {f['path']}{delta}")
        hidden = total - min(len(files), max_files)
        if hidden > 0:
            lines.append("  … " + tr("common.and_more", hidden))

    wf = p.get("work_files") or {}
    if wf.get("total"):
        dirs = ", ".join(f"{d} {n}" for d, n in (wf.get("dirs") or {}).items())
        lines.append(f"**{tr('repo.work_files')}:** {wf['total']} ({dirs}) – {tr('repo.work_files_note')}")

    ds = p.get("diffstat") or {}
    if show_diffstat and ds.get("count"):
        lines.append(f"**Diff:** " + tr("repo.diffstat", ds["count"], ds["added"], ds["removed"]))

    return "\n".join(lines)
