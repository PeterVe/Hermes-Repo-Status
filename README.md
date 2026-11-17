# repo-status – Hermes plugin

Prehľad toho, kde a čo Hermes robí: git repo chatu, kam zapisoval, čo zmenil v nastaveniach, s akými LLM providermi komunikoval, stav Honcho pamäte, miesto na disku a premenné prostredia. Panel v Hermes Desktop, príkazy v chate (aj Discord/Telegram), v termináli a nástroje pre model.

**Read-only:** plugin nič nezapisuje do repozitárov (žiadny fetch, push, commit, checkout; `git status` beží s `--no-optional-locks`) a nič nemaže. Tajomstvá (API kľúče, tokeny, heslá) nikdy nezobrazuje.

Vyžaduje **Hermes Agent 0.22.5+**.

---

## Inštalácia

Na každom PC s Hermesom:

```powershell
hermes plugins install PeterVe/Hermes-Repo-Status --enable
hermes gateway restart
```

V Hermes Desktop potom **Capabilities → Plugins → Rescan** a zapnúť **Repo Status** (desktopová časť je predvolene vypnutá). Panel **Repo** sa objaví vpravo.

Voliteľne doplniť históriu za posledných 90 dní (číta `state.db` len na čítanie):

```powershell
hermes repo-status --import-history
```

### Bez GitHubu: inštalácia z lokálneho alebo zdieľaného git repa

Hermes vie inštalovať aj z git repa na disku (napr. zdieľanom sieťovom disku). Nainštaluje sa len **commitnutý** stav, nie rozpracované súbory:

```powershell
hermes plugins install file:///X:/cesta/k/Hermes-Repo-Status --enable
```

### Aktualizácia

```powershell
hermes plugins check-updates          # je nová verzia?
hermes plugins update repo-status
hermes gateway restart                # beží živá session? počká, alebo: hermes gateway restart --yes (preruší ju)
```

V Desktope potom **Capabilities → Plugins → Rescan**.

### Dáta

Každé PC si vedie vlastné záznamy v `HERMES_HOME\plugin-shared\repo-status\` (`ledger.db`, `llm.db`, `disk.json`) – nič sa neposiela inam a záznamy rôznych PC sa nemiešajú.

---

## Čo plugin ukazuje

| Sekcia panelu | Chat / terminál | Obsah |
|---|---|---|
| **Project, Primary folder, Súbory** | `/repo` · `hermes repo-status [cesta]` | projekt chatu, jeho priečinky (git značka, klik otvorí Prieskumník), obsah priečinka |
| **External repo, Vetva, Git user** | `/repo`, `/repo check` | GitHub/GitLab/Forgejo/iné (credentials skryté), vetva a upstream, server cez `git ls-remote` (bez fetch, cache 60 s), posledný commit, git identita a jej scope |
| **Uncommitted, Pracovné Hermes** | `/repo` · `--diff` | necommitnuté zmeny kódu s +/−; pracovné súbory Hermesa v `.hermes/` zvlášť (nepočítajú sa do „všetko commitnuté“) |
| **External comm., Sessions** | – | odkiaľ je chat (Desktop/Discord/…), kanály projektu, sessions od najnovšej (klik prepne), kapacita kontextu aktuálne/max, provideri |
| **Kam zapisoval Hermes** | `/repo touched [all\|7d]` · `--touched` | doložené zápisy (`write_file`, `patch`, MCP), pravdepodobné príkazy (`terminal`, `execute_code`), miesta mimo pracovného adresára ⚠️ |
| **Nastavenia Hermesa** | `/repo settings [24h\|7d\|30d]` · `--settings` | zmeny skills, pamäte, cron, `config.yaml`, `SOUL.md`, pluginov, hookov; „pôvod neznámy“ = zmena mimo nástrojov Hermesa |
| **Honcho Memory** | `/repo honcho` · `--honcho` | aktívny memory provider („not available“ ak nie), nastavenia bez kľúča, anonymné overenie spojenia (DNS/TLS/timeout), udalosti z logu |
| **Disk Hermesa** | `/repo disk [refresh]` · `--disk` | voľné miesto, veľkosť oblastí Hermesa a pracovných priečinkov `cache\scratch` (s projektom a session, ktorá ich vytvorila); len prehľad, nič nemaže |
| **Premenné** | `/repo env` · `--env` | `HERMES_*` / `TERMINAL_*` s popisom z dokumentácie Hermesa; tajomstvá sa nevracajú |
| **LLM komunikácia** | `/repo llm [session\|24h\|7d]` · `--llm`, `--llm-call ID` | volania providerov: modely, tokeny, časy, chyby, endpointy; obsah len voliteľne |

Nástroje pre model: `repo_status`, `hermes_touched_paths`. Čip v status bare ukazuje vetvu a ±zmeny (klik otvorí Review/diff).

### Úrovne dôkazu (Kam zapisoval Hermes, Nastavenia)

| Úroveň | Zdroj |
|---|---|
| ✍️ doložené | volanie súborového nástroja alebo `skill_manage` / `memory` / `cronjob_manage` |
| ⌨️ pravdepodobné | `terminal`, `execute_code` – známy príkaz a adresár, nie súbory |
| pozorované | git ukazuje súbor necommitnutý, alebo sa zmenil súbor nastavení |

### Ochrana údajov

- Ledger ukladá len cesty a skrátené príkazy (tokeny a heslá maskované), nikdy obsah súborov, skillov ani pamäte.
- Záznam LLM ukladá predvolene len metadáta. Obsah volaní (`llm_capture_content`) je vypnutý; keď ho zapnete, uchováva sa 7 dní a tajomstvá sa maskujú.
- `.env`, `auth.json`, kľúče a lock súbory sa nečítajú ani nesledujú.

---

## Nastavenia

Desktop **Settings → Plugins → repo-status** alebo `plugins.entries.repo-status.settings` v `config.yaml`:

| Kľúč | Predvolené | Význam |
|---|---|---|
| `check_remote` | `true` | overovať server cez `git ls-remote` |
| `remote_cache_seconds`, `remote_timeout_seconds` | 60, 8 | cache a timeout overenia servera |
| `file_list_cap` | 200 | max. súborov v zozname |
| `host_kinds` | `{}` | vlastné servery, napr. `{"git.firma.sk": "forgejo"}` |
| `work_dirs` | `[".hermes/"]` | pracovné priečinky Hermesa v repe |
| `show_chip` | `true` | čip v status bare |
| `track_writes`, `ledger_retention_days` | `true`, 90 | ledger zápisov |
| `llm_log`, `llm_capture_content`, `llm_content_retention_days` | `true`, `false`, 7 | záznam LLM volaní |

---

## Vývoj

Pri vývoji z checkoutu na lokálnom disku stačí odkaz do `HERMES_HOME\plugins\repo-status`. Ak je checkout na **sieťovom disku**, odkaz nefunguje (Windows junction nemôže ukazovať na sieť; Hermes ho preskočí s `WinError 4392`), preto ho skript **skopíruje**:

```powershell
powershell -File scripts\sync-to-hermes.ps1     # po každej zmene kódu
hermes gateway restart                          # nový backend / hooky
```

Na ostatných PC použite inštaláciu vyššie, nie tento skript.

Testy a kontroly:

```powershell
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate .
```

Čo sa kedy prejaví: zmena v `desktop/plugin.js` po **Rescan** v Desktope; `dashboard/plugin_api.py`, hooky a nástroje po **reštarte gatewayu**; `/repo` a CLI hneď.

---

## Licencia

[MIT](LICENSE) © 2026 Peter Vesely
