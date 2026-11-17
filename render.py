"""Text rendering of a probe result for chat, Discord and the terminal (Slovak)."""

from __future__ import annotations

from typing import Any

SCOPE_LABELS = {"local": "lokálne (len toto repo)", "global": "globálne", "system": "systémovo", "worktree": "worktree", "command": "príkaz"}

REMOTE_STATUS = {
    "up_to_date": "✅ aktuálny – server má rovnaký commit",
    "local_ahead": "⬆️ lokálne commity nie sú na serveri (treba push)",
    "behind": "⬇️ na serveri sú novšie commity",
    "diverged": "⚠️ vetva sa rozišla so serverom",
    "remote_newer": "⬇️ na serveri je novší commit (počet neznámy bez fetch)",
    "not_on_remote": "⚠️ vetva na serveri neexistuje",
    "no_upstream": "⚠️ vetva nemá nastavený upstream",
    "detached": "⚠️ odpojený HEAD (detached)",
    "no_remote": "— repo nemá žiadny remote",
    "skipped": "— overenie servera vypnuté",
    "error": "❌ server sa nepodarilo overiť",
}

REASON_LABELS = {
    "uncommitted": "necommitnuté zmeny",
    "unpushed": "nepushnuté commity",
    "server_newer": "server má novšie zmeny",
    "not_on_remote": "vetva nie je na serveri",
    "no_upstream": "chýba upstream",
    "remote_unreachable": "server neoverený",
    "conflicts": "konflikty",
}


def ago(seconds: int | None) -> str:
    if seconds is None:
        return "nikdy"
    if seconds < 90:
        return f"pred {seconds} s"
    minutes = seconds // 60
    if minutes < 90:
        return f"pred {minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"pred {hours} h"
    return f"pred {hours // 24} dňami"


ACTION_LABELS = {"write": "zápis", "patch": "úprava", "create": "nový", "delete": "zmazaný", "command": "príkaz", "code": "kód"}


def _clock(ts: float) -> str:
    import time as _t

    lt = _t.localtime(ts)
    today = _t.localtime()
    return _t.strftime("%H:%M" if lt[:3] == today[:3] else "%d.%m. %H:%M", lt)


def render_touched(data: dict[str, Any], *, label: str = "", max_files: int = 25) -> str:
    groups = data.get("groups") or []
    head = f"**Kam zapisoval Hermes** – {label}" if label else "**Kam zapisoval Hermes**"
    if not groups:
        return head + "\n\nV ledgeri nie sú žiadne zápisy. (Ledger zapisuje od zapnutia pluginu F2.)"
    lines = [head, ""]
    outside = sum(1 for g in groups if g.get("outside_cwd"))
    if outside:
        lines.append(f"⚠️ {outside} miest mimo pracovného adresára session")
        lines.append("")
    for g in groups:
        flag = "⚠️ mimo cwd · " if g.get("outside_cwd") else ""
        if g.get("in_repo"):
            branch = f" (`{g['branch']}`)" if g.get("branch") else ""
            dirty = g.get("uncommitted_total")
            dirty_txt = f" · necommitnuté teraz: {dirty}" if dirty is not None else ""
            lines.append(f"{flag}**{g['root']}**{branch}{dirty_txt}")
        else:
            lines.append(f"{flag}**mimo git repa**")
        for f in g.get("files", [])[:max_files]:
            shown = f.get("rel_path") or f["path"]
            acts = "/".join(ACTION_LABELS.get(a, a) for a in f.get("actions", []))
            state = ""
            if f.get("still_uncommitted") is True:
                state = " · necommitnuté"
            elif f.get("still_uncommitted") is False:
                state = " · commitnuté alebo vrátené"
            guess = " · cesta odhadnutá" if f.get("path_guessed") else ""
            lines.append(f"  ✍️ `{shown}` – {acts}, {_clock(f['last_ts'])}{state}{guess}")
        more = (g.get("file_count") or 0) - min(len(g.get("files", [])), max_files)
        if more > 0:
            lines.append(f"  … a ďalších {more} súborov")
        for c in g.get("commands", [])[:5]:
            folder = f" v `{c['folder']}`" if c.get("folder") and not g.get("in_repo") else ""
            lines.append(f"  ⌨️ {c['tool']}{folder}: `{c.get('detail') or ''}` – {_clock(c['ts'])} (pravdepodobné, súbory neznáme)")
        lines.append("")
    lines.append("✍️ doložené zápisy (súborové nástroje) · ⌨️ príkazy – zmenené súbory nevieme určiť")
    return "\n".join(lines).rstrip()


def render_env(data: dict[str, Any], *, with_docs: bool = False) -> str:
    lines = [f"**Premenné Hermesa** – proces: {data.get('process', '?')}", ""]
    chat = data.get("chat")
    if chat:
        lines.append("**Tento chat** (dostane ich každý príkaz nástroja)")
        for k, v in chat.items():
            lines.append(f"  `{k}` = `{v or '—'}`")
        lines.append("")
    group = None
    for item in data.get("core") or []:
        if item["group"] != group:
            group = item["group"]
            lines.append(f"**{group.capitalize()}**")
        if item.get("set"):
            val = f"`{item['value']}`"
        elif item.get("resolved"):
            val = f"`{item['value']}` (dopočítané)"
        else:
            val = f"nenastavená → {item.get('default') or '—'}"
        lines.append(f"  `{item['name']}` = {val}")
        if with_docs and item.get("doc"):
            lines.append(f"      {item['doc'][:160]}")
    other = data.get("other") or []
    if other:
        lines.append("")
        lines.append(f"**Ďalšie nastavené** ({len(other)})")
        for item in other:
            lines.append(f"  `{item['name']}` = `{item['value']}`")
    if data.get("hidden_secret_count"):
        lines.append("")
        lines.append(f"Skryté (názov vyzerá ako tajomstvo): {data['hidden_secret_count']}")
    return "\n".join(lines)


def gb(n: int | None) -> str:
    n = n or 0
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} MB"
    return f"{n / 1024:.0f} kB"


def _age(ts: float | None) -> str:
    if not ts:
        return "?"
    import time as _t

    days = (_t.time() - ts) / 86400
    return "dnes" if days < 1 else f"{days:.0f} d"


def render_disk(data: dict[str, Any], *, max_scratch: int = 15) -> str:
    d = data.get("drive") or {}
    head = "**Disk Hermesa**"
    pct = d.get("free_pct")
    warn = " ⚠️" if pct is not None and pct < 10 else ""
    lines = [head, ""]
    if d:
        lines.append(f"Voľné miesto: {gb(d.get('free'))} z {gb(d.get('total'))} ({pct} %){warn}")
    if not data.get("areas"):
        lines.append("Veľkosti priečinkov sa práve počítajú na pozadí – skúste o minútu `/repo disk`." if data.get("scanning") else "Ešte nespočítané.")
        return "\n".join(lines)
    import time as _t

    lines.append(f"Hermes: {gb(data.get('home_size'))} · pracovné priečinky (cache\\scratch): {gb(data.get('scratch_size'))} v {len(data.get('scratch') or [])} priečinkoch")
    lines.append(f"Spočítané: {_t.strftime('%d.%m. %H:%M', _t.localtime(data.get('scanned_at') or 0))}" + (" (prepočítava sa)" if data.get("scanning") else ""))
    lines.append("")
    lines.append("**Oblasti**")
    for a in (data.get("areas") or [])[:8]:
        lines.append(f"  {gb(a['size']):>9}  {a['name']}  ({a['files']} súborov)")
    lines.append("")
    lines.append("**Najväčšie pracovné priečinky**")
    for s in (data.get("scratch") or [])[:max_scratch]:
        who = ""
        if s.get("sessions"):
            t = s["sessions"][0].get("title") or s["sessions"][0]["id"]
            who = f" · {t[:50]}"
        proj = f" · {s['project_cwd']}" if s.get("project_cwd") else ""
        lines.append(f"  {gb(s['size']):>9}  `{s['name']}` · {_age(s.get('newest'))}{proj}{who}")
    lines.append("")
    lines.append("Len prehľad – plugin nič nemaže. Priečinok otvoríte v Prieskumníkovi z Desktop panelu.")
    return "\n".join(lines)


HONCHO_LABELS = {
    "baseUrl": "Server", "environment": "Prostredie", "workspace": "Workspace", "peerName": "Používateľ (peer)",
    "aiPeer": "AI peer", "sessionStrategy": "Stratégia sessions", "recallMode": "Recall mód",
    "dialecticDynamic": "Dynamická dialektika", "writeFrequency": "Zápis", "contextTokens": "Kontext (tokeny)",
    "dialecticReasoningLevel": "Úroveň uvažovania", "enabled": "Zapnuté",
}


def render_honcho(data: dict[str, Any]) -> str:
    cfg = data.get("config") or {}
    head = "**Honcho pamäť**"
    if not data.get("available"):
        prov = cfg.get("provider")
        return head + " – not available" + (f" (aktívny memory provider: {prov})" if prov else " (žiadny externý memory provider)")
    state = "aktívna" if cfg.get("active") else f"nainštalovaná, ale aktívny provider je {cfg.get('provider') or 'žiadny'}"
    lines = [f"{head} – {state}", ""]
    for k, v in (cfg.get("settings") or {}).items():
        lines.append(f"{HONCHO_LABELS.get(k, k)}: `{v}`")
    lines.append(f"API kľúč: {'nastavený (skrytý)' if cfg.get('has_api_key') else 'chýba'} · aliasy používateľa: {cfg.get('alias_count', 0)}")
    lines.append(f"Plugin honcho: {'zapnutý' if cfg.get('plugin_enabled') else 'vypnutý' if cfg.get('plugin_installed') else 'nenainštalovaný'}"
                 f" · Desktop plugin: {'zapnutý' if cfg.get('desktop_plugin_enabled') else 'vypnutý' if cfg.get('desktop_plugin_installed') else 'nenainštalovaný'}")
    conn = data.get("connection")
    if conn:
        if conn.get("ok"):
            lines.append(f"Spojenie: ✅ server odpovedá (HTTP {conn.get('status')}, {conn.get('ms')} ms)")
        else:
            lines.append(f"Spojenie: ❌ {conn.get('error')}")
    logs = data.get("logs") or {}
    counts = logs.get("counts") or {}
    if counts:
        lines.append("Log (7 dní): " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    keys = logs.get("session_keys") or []
    if keys:
        lines.append("Honcho sessions z logu: " + ", ".join(f"`{k['key']}`" for k in keys[:6]))
    for e in (logs.get("problems") or [])[:6]:
        lines.append(f"  ⚠️ {_clock(e['ts'])} {e['level']} {e['message'][:140]}")
    return "\n".join(lines)


def _plural(n: int, one: str, few: str, many: str) -> str:
    return one if n == 1 else few if 2 <= n <= 4 else many


def tokens(n: int | None) -> str:
    n = n or 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} M"
    if n >= 1_000:
        return f"{n / 1_000:.1f} k"
    return str(n)


def render_llm(data: dict[str, Any], *, label: str = "", max_models: int = 6) -> str:
    head = f"**LLM komunikácia** – {label}" if label else "**LLM komunikácia**"
    if not data.get("enabled", True):
        return head + "\n\nZáznam je vypnutý (nastavenie llm_log)."
    t = data.get("total") or {}
    if not t.get("calls"):
        return head + "\n\nŽiadne zaznamenané volania. (Záznam beží od zapnutia F4.)"
    lines = [
        head,
        "",
        f"Spolu {t['calls']} {_plural(t['calls'], 'volanie', 'volania', 'volaní')} · vstup {tokens(t['input_tokens'])} · cache {tokens(t['cache_read_tokens'])} · "
        f"výstup {tokens(t['output_tokens'])} · chyby {t['errors']}",
        "",
    ]
    for p in data.get("providers") or []:
        dur = f" · ⌀ {p['avg_duration']} s" if p.get("avg_duration") is not None else ""
        lines.append(f"**{p['provider']}** – {p['calls']} {_plural(p['calls'], 'volanie', 'volania', 'volaní')}, vstup {tokens(p['input_tokens'])}, výstup {tokens(p['output_tokens'])}{dur}"
                     + (f", ❌ {p['errors']}" if p["errors"] else ""))
        for m in p["models"][:max_models]:
            lines.append(f"  • `{m['model']}` – {m['calls']}× · {tokens(m['input_tokens'])} → {tokens(m['output_tokens'])}"
                         + (f" · ❌ {m['errors']}" if m["errors"] else ""))
        if p.get("aux"):
            lines.append("  pomocné: " + ", ".join(f"{a['aux_task']} {a['calls']}×" for a in p["aux"][:6]))
        for h in p.get("hosts", [])[:3]:
            lines.append(f"  endpoint: {h}")
        lines.append("")
    errs = data.get("errors") or []
    if errs:
        lines.append("**Posledné chyby**")
        for e in errs[:8]:
            code = f" {e['status_code']}" if e.get("status_code") else ""
            lines.append(f"  ❌ {_clock(e['ts'])} {e['provider']}/{e['model']}{code}: {(e.get('error_message') or e.get('error_type') or '')[:120]}")
        lines.append("")
    lines.append("Obsah volaní: " + ("ukladá sa (detail: hermes repo-status --llm-call ID)" if data.get("capture_content") else "neukladá sa – len metadáta (nastavenie llm_capture_content)"))
    return "\n".join(lines).rstrip()


SETTINGS_ACTIONS = {"create": "nový", "modify": "zmenený", "delete": "zmazaný", "patch": "úprava", "edit": "úprava",
                    "write_file": "zápis", "add": "pridané", "remove": "odstránené", "replace": "nahradené",
                    "update": "zmena", "pause": "pozastavené", "resume": "obnovené", "run": "spustené", "write": "zápis"}


def render_settings(data: dict[str, Any], *, label: str = "", max_items: int = 15) -> str:
    cats = data.get("categories") or []
    head = f"**Zmeny nastavení Hermesa** – {label}" if label else "**Zmeny nastavení Hermesa**"
    if not cats:
        note = ""
        scan = data.get("scan") or {}
        if scan and not scan.get("changes") and not scan.get("skipped"):
            note = " (prvé sledovanie súborov si teraz uložilo východzí stav; ďalšie zmeny sa ukážu odteraz)"
        return head + "\n\nŽiadne zmeny skills, pamäte, cron, config, SOUL ani pluginov." + note
    lines = [head, ""]
    for c in cats:
        lines.append(f"**{c['label']}** – {c['item_count']}")
        for it in c["items"][:max_items]:
            acts = "/".join(SETTINGS_ACTIONS.get(a, a) for a in it["actions"])
            if it["by_hermes"]:
                who = "Hermes"
            elif it["unattributed"]:
                who = "pôvod neznámy"
            else:
                who = "Hermes (súbor)"
            lines.append(f"  • `{it['item']}` – {acts}, {_clock(it['last_ts'])} · {who}")
        more = c["item_count"] - min(len(c["items"]), max_items)
        if more > 0:
            lines.append(f"  … a ďalších {more}")
        lines.append("")
    lines.append("Hermes = doložené volaním nástroja · pôvod neznámy = zmena súboru bez volania nástroja (človek, iný program)")
    return "\n".join(lines).rstrip()


def _file_marker(f: dict[str, Any]) -> str:
    if f.get("conflicted"):
        return "‼"
    if f.get("untracked"):
        return "?"
    xy = f.get("xy", "..")
    return (xy[0] if xy[0] != "." else xy[1]) or "M"


def render(p: dict[str, Any], *, max_files: int = 30, show_diffstat: bool = True) -> str:
    if not p.get("ok"):
        return f"**Repo status:** {p.get('message') or p.get('error') or 'neznáma chyba'}"

    repo = p["repo"]
    br = p.get("branch") or {}
    ch = p.get("changes") or {}
    rc = p.get("remote_check") or {}
    summary = p.get("summary") or {}
    lines: list[str] = []

    if summary.get("current") and rc.get("status") == "skipped":
        verdict = "✅ všetko commitnuté (server neoverený)"
    elif summary.get("current"):
        verdict = "✅ všetko commitnuté a aktuálne"
    else:
        verdict = "⚠️ " + ", ".join(REASON_LABELS.get(r, r) for r in summary.get("reasons", []))
    lines.append(f"**Repo status** – {verdict}")
    lines.append("")

    # 1. local repo
    wt_note = " (git worktree)" if repo.get("is_worktree") else ""
    lines.append(f"**Lokálne repo:** `{repo['root']}`{wt_note}")
    wts = repo.get("worktrees") or []
    if len(wts) > 1:
        lines.append(f"  worktrees: {len(wts)} – " + ", ".join(f"`{w.get('branch') or w.get('head', '')[:7]}`" for w in wts[:6]))

    # 2. remotes
    remotes = p.get("remotes") or []
    if not remotes:
        lines.append("**Externé repo:** žiadne (len lokálne)")
    for r in remotes:
        where = r.get("web_url") or r.get("url")
        lines.append(f"**Externé repo ({r['name']}):** {r['kind_label']} – {where}")

    # 3. identity
    ident = p.get("identity") or {}
    name, email = ident.get("name"), ident.get("email")
    if name or email:
        scope = (email or name or {}).get("scope", "")
        who = f"{(name or {}).get('value', '?')} <{(email or {}).get('value', '?')}>"
        lines.append(f"**Git user:** {who} – {SCOPE_LABELS.get(scope, scope)}")
    else:
        lines.append("**Git user:** ⚠️ nenastavený (user.name / user.email)")

    # 4. branch
    if br.get("detached"):
        lines.append(f"**Vetva:** odpojený HEAD na `{(br.get('oid') or '')[:7]}`")
    else:
        up = f" → `{br['upstream']}`" if br.get("upstream") else " (bez upstreamu)"
        lines.append(f"**Vetva:** `{br.get('head')}`{up}")

    # 5. is the commit current?
    lc = p.get("last_commit")
    if lc:
        lines.append(f"**Posledný commit:** `{lc['short']}` {lc['subject']} – {lc['author']}, {lc['date'][:16].replace('T', ' ')}")
    else:
        lines.append("**Posledný commit:** zatiaľ žiadny")
    rs = rc.get("status")
    if rs:
        text = REMOTE_STATUS.get(rs, rs)
        extra = []
        if rs in ("local_ahead", "diverged") and rc.get("ahead"):
            extra.append(f"↑{rc['ahead']}")
        if rs in ("behind", "diverged") and rc.get("behind"):
            extra.append(f"↓{rc['behind']}")
        if rs == "error" and rc.get("error"):
            extra.append(rc["error"])
        if rc.get("cached"):
            extra.append(f"overené {ago(rc.get('age_seconds'))}")
        lines.append(f"**Server:** {text}" + (f" ({'; '.join(extra)})" if extra else ""))
    fetch = p.get("fetch") or {}
    if remotes:
        lines.append(f"**Posledný fetch:** {ago(fetch.get('age_seconds'))}")

    # 6. uncommitted files
    total = ch.get("total") or 0
    if total == 0:
        lines.append("**Necommitnuté súbory:** žiadne")
    else:
        parts = []
        for key, label in (("staged", "staged"), ("unstaged", "zmenené"), ("untracked", "nové"), ("conflicted", "konflikty")):
            if ch.get(key):
                parts.append(f"{ch[key]} {label}")
        lines.append(f"**Necommitnuté súbory:** {total} ({', '.join(parts)})")
        stat_by_path = {f["path"]: f for f in ((p.get("diffstat") or {}).get("files") or [])}
        files = ch.get("files") or []
        for f in files[:max_files]:
            st = stat_by_path.get(f["path"]) if show_diffstat else None
            delta = ""
            if st:
                delta = " (binárny)" if st.get("binary") else f" +{st['added']} −{st['removed']}"
            lines.append(f"  `{_file_marker(f)}` {f['path']}{delta}")
        hidden = total - min(len(files), max_files)
        if hidden > 0:
            lines.append(f"  … a ďalších {hidden}")

    wf = p.get("work_files") or {}
    if wf.get("total"):
        dirs = ", ".join(f"{d} {n}" for d, n in (wf.get("dirs") or {}).items())
        lines.append(f"**Pracovné súbory Hermesa:** {wf['total']} ({dirs}) – mimo verdiktu, nie sú to zmeny kódu")

    # 7. diff stat
    ds = p.get("diffstat") or {}
    if show_diffstat and ds.get("count"):
        lines.append(f"**Diff:** {ds['count']} súborov, +{ds['added']} −{ds['removed']} riadkov (nové súbory nie sú v diffe)")

    return "\n".join(lines)
