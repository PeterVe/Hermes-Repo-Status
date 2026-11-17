"""Read-only git probe for the repo-status plugin.

Self-contained on purpose: no Hermes imports and no relative imports, so the
agent half (``__init__.py``) and the dashboard backend (``dashboard/plugin_api.py``,
loaded by file path) share exactly this module.

Every git call is read-only. Nothing here fetches, pushes, commits, checks out
or touches the index (``--no-optional-locks``). The only network call is
``git ls-remote`` in :func:`check_remote`, with interactive credential prompts
disabled and a timeout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_TIMEOUT = 5.0
DEFAULT_REMOTE_TIMEOUT = 8.0
DEFAULT_REMOTE_CACHE_SECONDS = 60
DEFAULT_FILE_CAP = 200
DEFAULT_WORK_DIRS = (".hermes/",)  # Hermes' working material inside a repo, reported apart
WORK_FILE_CAP = 100
DEFAULT_SECRET_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*secret*",
    "*credential*",
    "id_rsa*",
    "id_ed25519*",
)
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GitError(Exception):
    """A git command failed or timed out."""


@dataclass
class GitResult:
    code: int
    out: str
    err: str


# ── running git ──────────────────────────────────────────────────────────────


def _base_env(network: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never prompt on a terminal
    env["GIT_OPTIONAL_LOCKS"] = "0"  # status must not take index.lock
    env["LC_ALL"] = env.get("LC_ALL") or "C.UTF-8"
    if network:
        env["GCM_INTERACTIVE"] = "never"  # Git Credential Manager: no login window
        env["GIT_ASKPASS"] = "echo"  # an askpass that answers nothing
        env["SSH_ASKPASS"] = "echo"
        env.pop("DISPLAY", None)
    return env


def run_git(
    cwd: str | os.PathLike[str],
    *args: str,
    timeout: float = DEFAULT_TIMEOUT,
    network: bool = False,
    check: bool = False,
) -> GitResult:
    """Run one git command without a shell. Never prompts. Decodes UTF-8 leniently."""
    cmd = ["git", "--no-optional-locks", "-c", "core.quotepath=false", "-c", "color.ui=false", "-C", str(cwd), *args]
    env = _base_env(network)
    if network and "GIT_SSH_COMMAND" not in env:
        # Respect a user's core.sshCommand; otherwise force non-interactive ssh.
        ssh_cmd = _git_config_value(cwd, "core.sshCommand")
        if not ssh_cmd:
            env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=5"
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0] if args else ''} exceeded the {timeout:g} s limit") from exc
    result = GitResult(
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace").strip(),
    )
    if check and result.code != 0:
        raise GitError(result.err or f"git {' '.join(args)} exited with code {result.code}")
    return result


def _git_config_value(cwd: str | os.PathLike[str], key: str) -> str | None:
    try:
        res = subprocess.run(
            ["git", "-C", str(cwd), "config", "--get", key],
            capture_output=True,
            timeout=DEFAULT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = res.stdout.decode("utf-8", errors="replace").strip()
    return value or None


# ── remotes ──────────────────────────────────────────────────────────────────

_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>(?!//).+)$")


def redact_url(url: str) -> str:
    """Hide credentials in a remote URL (``https://token@host`` → ``https://***@host``)."""
    url = url.strip()
    if "://" in url:
        try:
            parts = urlsplit(url)
        except ValueError:
            return url
        if parts.username is None and parts.password is None:
            return url
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        # ssh://git@host is a login name, not a secret; http(s) userinfo usually is a token.
        if parts.scheme in ("ssh", "git+ssh", "ssh+git") and parts.password is None:
            return url
        return urlunsplit((parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment))
    return url


def parse_remote_url(url: str) -> dict[str, str | None]:
    """Split a remote URL into host and ``owner/repo`` path (best effort)."""
    url = url.strip()
    if "://" in url:
        try:
            parts = urlsplit(url)
        except ValueError:
            return {"host": None, "path": None, "scheme": None}
        if parts.scheme == "file":
            return {"host": None, "path": parts.path, "scheme": "file"}
        path = parts.path.strip("/")
        return {"host": (parts.hostname or "").lower() or None, "path": path or None, "scheme": parts.scheme}
    m = _SCP_LIKE.match(url)
    if m and not re.match(r"^[A-Za-z]:[\\/]", url):  # not a Windows drive path
        return {"host": m.group("host").lower(), "path": m.group("path").strip("/"), "scheme": "ssh"}
    return {"host": None, "path": url, "scheme": "file"}


KIND_LABELS = {
    "github": "GitHub",
    "gitlab": "GitLab",
    "forgejo": "Forgejo/Gitea",
    "bitbucket": "Bitbucket",
    "azure": "Azure DevOps",
    "local": "local folder",
    "other": "other",
}


def classify_host(host: str | None, host_kinds: dict[str, str] | None = None) -> str:
    """Map a hostname to a forge kind. Explicit ``host_kinds`` from settings win."""
    if not host:
        return "local"
    host = host.lower()
    for name, kind in (host_kinds or {}).items():
        name = str(name).lower().strip()
        if name and (host == name or host.endswith("." + name)):
            kind = str(kind).lower().strip()
            return kind if kind in KIND_LABELS else "other"
    if host in ("github.com", "ssh.github.com") or host.endswith(".github.com") or host.endswith(".ghe.com"):
        return "github"
    if host == "gitlab.com" or host.endswith(".gitlab.com") or "gitlab" in host:
        return "gitlab"
    if host in ("codeberg.org",) or "forgejo" in host or "gitea" in host:
        return "forgejo"
    if host in ("bitbucket.org",) or host.endswith(".bitbucket.org"):
        return "bitbucket"
    if host in ("dev.azure.com", "ssh.dev.azure.com") or host.endswith(".visualstudio.com"):
        return "azure"
    return "other"


def describe_remote(name: str, url: str, host_kinds: dict[str, str] | None = None) -> dict[str, Any]:
    parsed = parse_remote_url(url)
    kind = classify_host(parsed["host"], host_kinds)
    path = parsed["path"] or ""
    repo_path = path[:-4] if path.endswith(".git") else path
    web_url = None
    if parsed["host"] and kind != "local" and repo_path:
        web_url = f"https://{parsed['host'] if parsed['host'] != 'ssh.github.com' else 'github.com'}/{repo_path}"
    return {
        "name": name,
        "url": redact_url(url),
        "host": parsed["host"],
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "repo": repo_path or None,
        "web_url": web_url,
    }


def read_remotes(cwd: str, host_kinds: dict[str, str] | None = None) -> list[dict[str, Any]]:
    res = run_git(cwd, "remote", "-v")
    seen: dict[str, dict[str, Any]] = {}
    for line in res.out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1]
        role = parts[2].strip("()") if len(parts) > 2 else "fetch"
        entry = seen.get(name)
        if entry is None:
            entry = describe_remote(name, url, host_kinds)
            entry["push_url"] = None
            seen[name] = entry
        if role == "push" and redact_url(url) != entry["url"]:
            entry["push_url"] = redact_url(url)
    return list(seen.values())


# ── identity ─────────────────────────────────────────────────────────────────


def read_identity(cwd: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("user.name", "user.email"):
        res = run_git(cwd, "config", "--show-origin", "--show-scope", "--get", key)
        if res.code != 0 or not res.out.strip():
            out[key.split(".")[1]] = None
            continue
        line = res.out.strip().splitlines()[-1]
        bits = line.split("\t", 2)
        if len(bits) == 3:
            scope, origin, value = bits
        else:
            scope, origin, value = "unknown", "", line
        if origin.startswith("file:"):
            origin = origin[5:]
        out[key.split(".")[1]] = {"value": value, "scope": scope, "origin": origin}
    return out


# ── status ───────────────────────────────────────────────────────────────────


def parse_status_v2(raw: str, file_cap: int = DEFAULT_FILE_CAP, work_prefixes: tuple[str, ...] = ()) -> dict[str, Any]:
    """Parse ``git status --porcelain=v2 --branch -z`` output."""
    branch: dict[str, Any] = {
        "head": None,
        "detached": False,
        "oid": None,
        "upstream": None,
        "ahead": None,
        "behind": None,
    }
    counts = {"staged": 0, "unstaged": 0, "untracked": 0, "conflicted": 0}
    files: list[dict[str, Any]] = []
    total = 0
    work_total = 0
    work_by_dir: dict[str, int] = {}
    work_files: list[dict[str, Any]] = []
    work_prefixes = tuple(p.replace("\\", "/").rstrip("/") + "/" for p in work_prefixes if p and p.strip("/\\"))
    tokens = raw.split("\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        if tok.startswith("# "):
            key, _, value = tok[2:].partition(" ")
            if key == "branch.oid":
                branch["oid"] = None if value == "(initial)" else value
            elif key == "branch.head":
                if value == "(detached)":
                    branch["detached"] = True
                else:
                    branch["head"] = value
            elif key == "branch.upstream":
                branch["upstream"] = value
            elif key == "branch.ab":
                m = re.match(r"\+(\d+) -(\d+)", value)
                if m:
                    branch["ahead"], branch["behind"] = int(m.group(1)), int(m.group(2))
            continue
        kind = tok[0]
        entry: dict[str, Any] | None = None
        if kind == "1":
            f = tok.split(" ", 8)
            if len(f) == 9:
                entry = _entry(f[1], f[8])
        elif kind == "2":
            f = tok.split(" ", 9)
            orig = tokens[i] if i < len(tokens) else None
            i += 1
            if len(f) == 10:
                entry = _entry(f[1], f[9])
                entry["orig_path"] = orig
        elif kind == "u":
            f = tok.split(" ", 10)
            if len(f) == 11:
                entry = {"path": f[10], "xy": f[1], "staged": False, "unstaged": False, "untracked": False, "conflicted": True}
        elif kind == "?":
            entry = {"path": tok[2:], "xy": "??", "staged": False, "unstaged": False, "untracked": True, "conflicted": False}
        elif kind == "!":
            continue  # ignored files are never listed
        if entry is None:
            continue
        if work_prefixes and entry["path"].startswith(work_prefixes):
            # Hermes' own working material inside the repo (plans, sketches, notes): reported
            # apart so it never hides — or masquerades as — real source changes.
            work_total += 1
            prefix = next(p for p in work_prefixes if entry["path"].startswith(p))
            work_by_dir[prefix] = work_by_dir.get(prefix, 0) + 1
            if len(work_files) < WORK_FILE_CAP:
                work_files.append(entry)
            continue
        total += 1
        for name in counts:
            if entry[name]:
                counts[name] += 1
        if len(files) < file_cap:
            files.append(entry)
    return {
        "branch": branch,
        "changes": {**counts, "total": total, "files": files, "truncated": max(0, total - len(files))},
        "work_files": {"total": work_total, "dirs": work_by_dir, "files": work_files, "truncated": max(0, work_total - len(work_files))},
    }


def _entry(xy: str, path: str) -> dict[str, Any]:
    return {
        "path": path,
        "xy": xy,
        "staged": xy[0] != ".",
        "unstaged": len(xy) > 1 and xy[1] != ".",
        "untracked": False,
        "conflicted": False,
    }


def parse_numstat_z(raw: str, file_cap: int = DEFAULT_FILE_CAP) -> dict[str, Any]:
    """Parse ``git diff --numstat -z`` output into per-file +/- counts."""
    files: list[dict[str, Any]] = []
    added_total = removed_total = 0
    count = 0
    tokens = raw.split("\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok:
            continue
        parts = tok.split("\t", 2)
        if len(parts) < 3:
            continue
        a, r, path = parts
        orig = None
        if path == "":  # rename/copy: next two tokens are old and new path
            orig = tokens[i] if i < len(tokens) else None
            path = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 2
        binary = a == "-" or r == "-"
        added = 0 if binary else int(a)
        removed = 0 if binary else int(r)
        added_total += added
        removed_total += removed
        count += 1
        if len(files) < file_cap:
            item: dict[str, Any] = {"path": path, "added": added, "removed": removed, "binary": binary}
            if orig:
                item["orig_path"] = orig
            files.append(item)
    return {"files": files, "added": added_total, "removed": removed_total, "count": count, "truncated": max(0, count - len(files))}


# ── worktrees ────────────────────────────────────────────────────────────────


def parse_worktrees(raw: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in raw.splitlines():
        if line.startswith("worktree "):
            if cur:
                out.append(cur)
            cur = {"path": _norm(line[9:]), "branch": None, "head": None, "detached": False, "locked": False, "prunable": False, "bare": False}
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            ref = line[7:]
            cur["branch"] = ref[11:] if ref.startswith("refs/heads/") else ref
        elif line == "detached":
            cur["detached"] = True
        elif line == "bare":
            cur["bare"] = True
        elif line.startswith("locked"):
            cur["locked"] = True
        elif line.startswith("prunable"):
            cur["prunable"] = True
    if cur:
        out.append(cur)
    for idx, wt in enumerate(out):
        wt["is_main"] = idx == 0
    return out


def _norm(p: str) -> str:
    return str(Path(p)) if p else p


# ── remote freshness (network) ───────────────────────────────────────────────

_remote_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_remote_cache_lock = threading.Lock()


def clear_remote_cache() -> None:
    with _remote_cache_lock:
        _remote_cache.clear()
    _branches_cache.clear()


def check_remote(
    root: str,
    branch: dict[str, Any],
    *,
    timeout: float = DEFAULT_REMOTE_TIMEOUT,
    cache_seconds: float = DEFAULT_REMOTE_CACHE_SECONDS,
    fresh: bool = False,
) -> dict[str, Any]:
    """Compare the local branch with the server using ``git ls-remote`` (read-only).

    Status values:
      up_to_date      server == local HEAD
      local_ahead     server is an ancestor of HEAD (unpushed commits)
      behind          server has commits HEAD lacks (count known)
      diverged        both sides have unique commits (counts known)
      remote_newer    server moved to a commit we don't have locally (count unknown without fetch)
      not_on_remote   the upstream branch does not exist on the server
      no_upstream     the branch has no upstream configured
      detached        HEAD is detached
      error           the server could not be reached
    """
    now = time.time()
    head = branch.get("head")
    if branch.get("detached") or not head:
        return {"status": "detached", "checked_at": None}
    remote = _git_config_value(root, f"branch.{head}.remote")
    merge_ref = _git_config_value(root, f"branch.{head}.merge")
    if not remote or not merge_ref or remote == ".":
        return {"status": "no_upstream", "checked_at": None}

    key = (str(root), remote, merge_ref)
    if not fresh and cache_seconds > 0:
        with _remote_cache_lock:
            hit = _remote_cache.get(key)
        if hit and now - hit[0] < cache_seconds:
            return {**hit[1], "cached": True, "age_seconds": int(now - hit[0])}

    result: dict[str, Any] = {"remote": remote, "ref": merge_ref, "cached": False, "age_seconds": 0}
    try:
        res = run_git(root, "ls-remote", "--", remote, merge_ref, timeout=timeout, network=True)
    except GitError as exc:
        result.update(status="error", error=str(exc), checked_at=_iso(now))
        return result  # errors are not cached: the next view retries
    if res.code != 0:
        result.update(status="error", error=_short_err(res.err), checked_at=_iso(now))
        return result

    remote_oid = None
    for line in res.out.splitlines():
        oid, _, ref = line.partition("\t")
        if ref.strip() == merge_ref:
            remote_oid = oid.strip()
            break
    local_oid = branch.get("oid")
    result["remote_oid"] = remote_oid
    result["local_oid"] = local_oid
    result["checked_at"] = _iso(now)

    if remote_oid is None:
        result["status"] = "not_on_remote"
    elif local_oid and remote_oid == local_oid:
        result.update(status="up_to_date", ahead=0, behind=0)
    elif local_oid and _has_commit(root, remote_oid):
        counts = run_git(root, "rev-list", "--left-right", "--count", f"{local_oid}...{remote_oid}")
        try:
            ahead, behind = (int(x) for x in counts.out.split())
        except ValueError:
            ahead = behind = None
        result.update(ahead=ahead, behind=behind)
        if ahead and behind:
            result["status"] = "diverged"
        elif behind:
            result["status"] = "behind"
        elif ahead:
            result["status"] = "local_ahead"
        else:
            result["status"] = "up_to_date"
    else:
        result.update(status="remote_newer", behind=None)

    with _remote_cache_lock:
        _remote_cache[key] = (now, {k: v for k, v in result.items() if k not in ("cached", "age_seconds")})
    return result


def _has_commit(root: str, oid: str) -> bool:
    return run_git(root, "cat-file", "-e", f"{oid}^{{commit}}").code == 0


def _short_err(err: str) -> str:
    lines = [ln for ln in err.splitlines() if ln.strip()]
    msg = lines[-1] if lines else "unknown error"
    low = err.lower()
    if "not found" in low or "could not read username" in low or "authentication failed" in low:
        msg += " (repository missing, or no access / sign-in)"
    elif "could not resolve host" in low or "timed out" in low or "unable to access" in low:
        msg += " (server unreachable)"
    return redact_text(msg)[:300]


_URL_CRED = re.compile(r"(https?://)[^/@\s]+@")


def redact_text(text: str) -> str:
    return _URL_CRED.sub(r"\1***@", text)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


# ── the probe ────────────────────────────────────────────────────────────────


def find_root(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    p = Path(path)
    if not p.exists():
        raise GitError(f"path does not exist: {p}")
    if p.is_file():
        p = p.parent
    res = run_git(p, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir", "--is-bare-repository")
    if res.code != 0:
        return None
    lines = res.out.splitlines()
    if len(lines) < 4:
        return None
    top, git_dir, common, bare = lines[:4]
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (p / common_path).resolve()
    git_dir_p = Path(git_dir)
    return {
        "root": _user_root(p, top),
        "root_git": _norm(top),
        "git_dir": _norm(git_dir),
        "common_dir": str(common_path),
        "is_worktree": git_dir_p.resolve() != common_path.resolve(),
        "bare": bare.strip() == "true",
    }


def _user_root(start: Path, git_top: str) -> str:
    """The repo root in the caller's notation (a mapped drive ``R:\\`` stays ``R:\\``).

    git reports the resolved path (``\\\\server\\share\\…`` for a mapped drive), which the
    user never typed. Walk up from the given path to the folder holding ``.git`` and
    use it when its name matches git's toplevel.
    """
    top_name = Path(git_top).name.lower()
    try:
        cur = start.absolute()
    except OSError:
        return _norm(git_top)
    for cand in (cur, *cur.parents):
        if (cand / ".git").exists():
            return str(cand) if cand.name.lower() == top_name else _norm(git_top)
    return _norm(git_top)


def probe(
    path: str | os.PathLike[str],
    *,
    check_remote_enabled: bool = True,
    fresh: bool = False,
    host_kinds: dict[str, str] | None = None,
    file_cap: int = DEFAULT_FILE_CAP,
    remote_timeout: float = DEFAULT_REMOTE_TIMEOUT,
    remote_cache_seconds: float = DEFAULT_REMOTE_CACHE_SECONDS,
    work_dirs: tuple[str, ...] | list[str] = DEFAULT_WORK_DIRS,
) -> dict[str, Any]:
    """Collect everything the plugin shows for one path. Never raises."""
    started = time.time()
    out: dict[str, Any] = {"ok": False, "path": str(path), "probed_at": _iso(started)}
    try:
        repo = find_root(path)
        if repo is None:
            out["error"] = "not_a_repo"
            out["message"] = f"{path} is not inside a git repository"
            return out
        if repo["bare"]:
            out["error"] = "bare_repo"
            out["message"] = "a bare repository has no working tree"
            out["repo"] = repo
            return out
        root = repo["root"]
        out["repo"] = repo

        wt = run_git(root, "worktree", "list", "--porcelain")
        repo["worktrees"] = parse_worktrees(wt.out) if wt.code == 0 else []

        out["remotes"] = read_remotes(root, host_kinds)
        out["identity"] = read_identity(root)

        st = run_git(root, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all", check=True)
        work = tuple(work_dirs or ())
        parsed = parse_status_v2(st.out, file_cap, work)
        out["branch"] = parsed["branch"]
        out["changes"] = parsed["changes"]
        out["work_files"] = parsed["work_files"]

        out["last_commit"] = _last_commit(root) if parsed["branch"]["oid"] else None

        base = "HEAD" if parsed["branch"]["oid"] else EMPTY_TREE
        excludes = [f":(exclude){d.replace(chr(92), '/').strip('/')}" for d in work if d.strip("/\\")]
        ns = run_git(root, "diff", "--numstat", "-z", "-M", base, "--", ".", *excludes)
        out["diffstat"] = parse_numstat_z(ns.out, file_cap) if ns.code == 0 else None

        out["fetch"] = _fetch_info(repo["common_dir"], started)

        if check_remote_enabled and out["remotes"]:
            out["remote_check"] = check_remote(
                root, parsed["branch"], timeout=remote_timeout, cache_seconds=remote_cache_seconds, fresh=fresh
            )
        elif not out["remotes"]:
            out["remote_check"] = {"status": "no_remote", "checked_at": None}
        else:
            out["remote_check"] = {"status": "skipped", "checked_at": None}

        out["summary"] = summarize(out)
        out["ok"] = True
    except GitError as exc:
        out["error"] = "git_error"
        out["message"] = redact_text(str(exc))
    except Exception as exc:  # the probe must never take a tool call down
        out["error"] = "internal_error"
        out["message"] = f"{type(exc).__name__}: {exc}"
    finally:
        out["duration_ms"] = int((time.time() - started) * 1000)
    return out


def probe_remote(
    path: str | os.PathLike[str],
    *,
    fresh: bool = False,
    remote_timeout: float = DEFAULT_REMOTE_TIMEOUT,
    remote_cache_seconds: float = DEFAULT_REMOTE_CACHE_SECONDS,
) -> dict[str, Any]:
    """Only the server comparison (for UIs that show the fast local part first). Never raises."""
    try:
        repo = find_root(path)
        if repo is None or repo["bare"]:
            return {"status": "not_a_repo", "checked_at": None}
        root = repo["root"]
        if not run_git(root, "remote").out.strip():
            return {"status": "no_remote", "checked_at": None}
        st = run_git(root, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=no", check=True)
        branch = parse_status_v2(st.out, 0)["branch"]
        return check_remote(root, branch, timeout=remote_timeout, cache_seconds=remote_cache_seconds, fresh=fresh)
    except GitError as exc:
        return {"status": "error", "error": redact_text(str(exc)), "checked_at": _iso(time.time())}
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "checked_at": _iso(time.time())}


# ── all branches on the server (network, read-only) ──────────────────────────

_branches_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _pick_remote(root: str, head: str | None) -> str | None:
    if head:
        remote = _git_config_value(root, f"branch.{head}.remote")
        if remote and remote != ".":
            return remote
    names = run_git(root, "remote").out.split()
    return "origin" if "origin" in names else (names[0] if names else None)


def parse_ls_remote(raw: str) -> tuple[dict[str, str], str | None]:
    """``git ls-remote --symref`` output → ({branch: sha}, default branch)."""
    heads: dict[str, str] = {}
    default = None
    for line in raw.splitlines():
        left, _, ref = line.partition("\t")
        ref = ref.strip()
        if left.startswith("ref: ") and ref == "HEAD":
            target = left[5:].strip()
            if target.startswith("refs/heads/"):
                default = target[len("refs/heads/"):]
        elif ref.startswith("refs/heads/"):
            heads[ref[len("refs/heads/"):]] = left.strip()
    return heads, default


def _commit_info(root: str, shas: list[str]) -> dict[str, dict[str, Any]]:
    """Subject/author/date for the commits that exist locally (one git call)."""
    if not shas:
        return {}
    out: dict[str, dict[str, Any]] = {}
    proc = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        input=("\n".join(shas) + "\n").encode(), capture_output=True, timeout=DEFAULT_TIMEOUT,
        env=_base_env(), creationflags=_CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        return out
    known = [ln.split()[0] for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln.endswith(" commit")]
    if not known:
        return out
    res = run_git(root, "show", "-s", "--format=%H%x00%h%x00%s%x00%an%x00%aI", *known)
    for line in res.out.splitlines():
        parts = line.split("\0")
        if len(parts) >= 5:
            out[parts[0]] = {"short": parts[1], "subject": parts[2], "author": parts[3], "date": parts[4]}
    return out


def _open_prs(remote_url_repo: str | None, kind: str, timeout: float) -> dict[str, Any]:
    """Open pull requests per head branch via the GitHub CLI (optional; never required)."""
    if kind != "github" or not remote_url_repo:
        return {"status": "unsupported"}
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--repo", remote_url_repo, "--state", "open", "--limit", "100",
             "--json", "number,headRefName,title,isDraft,url"],
            capture_output=True, timeout=timeout, env=_base_env(network=True), stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return {"status": "no_gh"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "gh timed out"}
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return {"status": "error", "error": redact_text(lines[-1] if lines else "gh failed")[:200]}
    try:
        items = json.loads(proc.stdout.decode("utf-8", "replace") or "[]")
    except ValueError:
        return {"status": "error", "error": "unexpected gh output"}
    by_branch: dict[str, list[dict[str, Any]]] = {}
    for pr in items if isinstance(items, list) else []:
        by_branch.setdefault(str(pr.get("headRefName")), []).append(
            {"number": pr.get("number"), "title": pr.get("title"), "draft": bool(pr.get("isDraft")), "url": pr.get("url")})
    return {"status": "ok", "by_branch": by_branch, "count": len(items) if isinstance(items, list) else 0}


def remote_branches(
    path: str | os.PathLike[str],
    *,
    remote: str | None = None,
    fresh: bool = False,
    timeout: float = DEFAULT_REMOTE_TIMEOUT,
    cache_seconds: float = DEFAULT_REMOTE_CACHE_SECONDS,
    host_kinds: dict[str, str] | None = None,
    with_prs: bool = True,
) -> dict[str, Any]:
    """Every branch on the server (``git ls-remote``, never fetch) compared with the local
    branches: default, current, local copy, ahead/behind when the commit is local, last commit,
    open pull requests (GitHub via ``gh``). Never raises."""
    now = time.time()
    try:
        repo = find_root(path)
        if repo is None or repo["bare"]:
            return {"ok": False, "error": "not_a_repo"}
        root = repo["root"]
        st = run_git(root, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=no")
        head = parse_status_v2(st.out, 0)["branch"].get("head")
        remote = remote or _pick_remote(root, head)
        if not remote:
            return {"ok": True, "remote": None, "status": "no_remote", "branches": []}
        key = (str(root), remote)
        if not fresh and cache_seconds > 0:
            hit = _branches_cache.get(key)
            if hit and now - hit[0] < cache_seconds:
                return {**hit[1], "cached": True, "age_seconds": int(now - hit[0])}

        url_info = next((r for r in read_remotes(root, host_kinds) if r["name"] == remote), None)
        res = run_git(root, "ls-remote", "--symref", "--", remote, "HEAD", "refs/heads/*", timeout=timeout, network=True)
        if res.code != 0:
            return {"ok": True, "remote": remote, "status": "error", "error": _short_err(res.err), "branches": [], "checked_at": _iso(now)}
        heads, default = parse_ls_remote(res.out)

        local: dict[str, dict[str, str]] = {}
        fe = run_git(root, "for-each-ref", "refs/heads", "--format=%(refname:short)%00%(objectname)%00%(upstream:short)")
        for line in fe.out.splitlines():
            parts = line.split("\0")
            if len(parts) == 3:
                local[parts[0]] = {"sha": parts[1], "upstream": parts[2]}
        tracking = {}
        tr_ = run_git(root, "for-each-ref", f"refs/remotes/{remote}", "--format=%(refname)%00%(objectname)")
        for line in tr_.out.splitlines():
            ref, _, sha = line.partition("\0")
            name = ref[len(f"refs/remotes/{remote}/"):]
            if name != "HEAD":
                tracking[name] = sha

        info = _commit_info(root, sorted(set(heads.values())))
        branches = []
        for name, sha in heads.items():
            loc = next((b for b, v in local.items() if v["upstream"] == f"{remote}/{name}"), None) or (name if name in local else None)
            item: dict[str, Any] = {
                "name": name, "sha": sha, "short": sha[:7], "default": name == default, "current": loc is not None and loc == head,
                "local": loc, "commit": info.get(sha), "fetched": tracking.get(name) == sha,
            }
            if loc:
                lsha = local[loc]["sha"]
                if lsha == sha:
                    item["sync"] = "same"
                elif sha in info:
                    counts = run_git(root, "rev-list", "--left-right", "--count", f"{lsha}...{sha}").out.split()
                    if len(counts) == 2:
                        a, b = int(counts[0]), int(counts[1])
                        item.update(ahead=a, behind=b, sync="diverged" if a and b else "ahead" if a else "behind" if b else "same")
                else:
                    item["sync"] = "server_newer"  # the server commit is not local: fetch would bring it
            else:
                item["sync"] = "server_only"
            branches.append(item)
        local_only = [b for b in local if not any(x.get("local") == b for x in branches)]

        prs = _open_prs(url_info.get("repo") if url_info else None, url_info.get("kind") if url_info else "", timeout) if with_prs else {"status": "off"}
        for item in branches:
            item["prs"] = (prs.get("by_branch") or {}).get(item["name"], [])

        def order(b: dict[str, Any]) -> tuple:
            return (not b["current"], not b["default"], -(_ts(b["commit"]["date"]) if b.get("commit") else 0), b["name"])

        branches.sort(key=order)
        result = {
            "ok": True, "remote": remote, "status": "ok", "default": default, "branches": branches, "count": len(branches),
            "local_only": local_only, "prs": {k: v for k, v in prs.items() if k != "by_branch"},
            "web_url": url_info.get("web_url") if url_info else None, "kind": url_info.get("kind") if url_info else None,
            "checked_at": _iso(now), "cached": False, "age_seconds": 0,
        }
        _branches_cache[key] = (now, {k: v for k, v in result.items() if k not in ("cached", "age_seconds")})
        return result
    except GitError as exc:
        return {"ok": False, "error": "git_error", "message": redact_text(str(exc))}
    except Exception as exc:
        return {"ok": False, "error": "internal_error", "message": f"{type(exc).__name__}: {exc}"}


def _ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def _last_commit(root: str) -> dict[str, Any] | None:
    res = run_git(root, "log", "-1", "--format=%H%x00%h%x00%s%x00%an%x00%ae%x00%aI")
    if res.code != 0 or not res.out.strip():
        return None
    parts = res.out.rstrip("\n").split("\0")
    if len(parts) < 6:
        return None
    return {"oid": parts[0], "short": parts[1], "subject": parts[2], "author": parts[3], "email": parts[4], "date": parts[5]}


def _fetch_info(common_dir: str, now: float) -> dict[str, Any]:
    fh = Path(common_dir) / "FETCH_HEAD"
    try:
        mtime = fh.stat().st_mtime
    except OSError:
        return {"last_fetch": None, "age_seconds": None}
    return {"last_fetch": _iso(mtime), "age_seconds": int(now - mtime)}


def summarize(p: dict[str, Any]) -> dict[str, Any]:
    """One overall verdict for 'is this commit current?' plus a tone for UIs."""
    ch = p.get("changes") or {}
    br = p.get("branch") or {}
    rc = p.get("remote_check") or {}
    clean = (ch.get("total") or 0) == 0
    status = rc.get("status")
    reasons: list[str] = []
    if not clean:
        reasons.append("uncommitted")
    if status in ("local_ahead", "diverged") or (status in ("skipped", "error", "remote_newer") and (br.get("ahead") or 0) > 0):
        reasons.append("unpushed")
    if status in ("behind", "diverged", "remote_newer") or (status in ("skipped", "error") and (br.get("behind") or 0) > 0):
        reasons.append("server_newer")
    if status == "not_on_remote":
        reasons.append("not_on_remote")
    if status == "no_upstream":
        reasons.append("no_upstream")
    if status == "error":
        reasons.append("remote_unreachable")
    if (ch.get("conflicted") or 0) > 0:
        reasons.append("conflicts")
    if "conflicts" in reasons or "server_newer" in reasons:
        tone = "bad"
    elif reasons:
        tone = "warn"
    else:
        tone = "good"
    return {"current": not reasons, "clean": clean, "reasons": reasons, "tone": tone}


# ── full diff (terminal only) ────────────────────────────────────────────────


def full_diff(
    path: str | os.PathLike[str],
    *,
    secret_globs: tuple[str, ...] | list[str] = DEFAULT_SECRET_GLOBS,
    max_bytes: int = 256 * 1024,
    file: str | None = None,
) -> dict[str, Any]:
    """Unified diff vs HEAD, with secret-looking files excluded and a size cap."""
    repo = find_root(path)
    if repo is None:
        return {"ok": False, "message": f"{path} is not inside a git repository"}
    root = repo["root"]
    has_head = run_git(root, "rev-parse", "--verify", "-q", "HEAD").code == 0
    args = ["diff", "-M", "HEAD" if has_head else EMPTY_TREE, "--"]
    args.append(file if file else ".")
    args += [f":(exclude,glob)**/{g}" for g in secret_globs]
    res = run_git(root, *args, timeout=15)
    if res.code != 0:
        return {"ok": False, "message": redact_text(res.err)}
    text = res.out
    raw = text.encode("utf-8")
    truncated = len(raw) > max_bytes
    if truncated:
        text = raw[:max_bytes].decode("utf-8", errors="ignore")
    return {"ok": True, "root": root, "diff": text, "truncated": truncated, "excluded_globs": list(secret_globs)}
