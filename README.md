# Medik

This repository no longer contains the KuCoin spot-trading toolkit. The
`kucoin/` package, its examples, its tests and the CI workflow that ran
them were removed.

Nothing is lost - the code is still in this repository's history. Commit
`95b04f3` is the last one that contained it:

```bash
# browse the code as it was
git checkout 95b04f3

# or restore it onto a branch
git checkout -b restore-kucoin 95b04f3
```

No API credentials were ever committed here: `.env` was gitignored from
the first commit, and `.env.example` only ever held empty placeholders.
Removing the code therefore has no bearing on the safety of any KuCoin
key. If a key was used on a machine you have doubts about, rotate it at
<https://www.kucoin.com/account/api> - deleting source code does not
revoke a key that has already been issued.

## Installing the trader-dev MCP server

`scripts/Install-TraderDevMcp.ps1` registers the `trader-dev` SSE server with
Claude Code at **user scope**, which makes it available in all of your
projects. **PowerShell is the supported way to install this.** A bash script
is also included for Unix shells, but it is optional - see
[Optional: the bash installer](#optional-the-bash-installer).

## Windows, step by step

### Prerequisites

* **PowerShell.** Developed and tested against PowerShell 7.6 (`pwsh`). It
  avoids 7-only syntax and should work on the Windows-bundled 5.1, but that
  has not been verified - if you hit errors on 5.1, install 7 with
  `winget install Microsoft.PowerShell` and run `pwsh`. Check your version
  with `$PSVersionTable.PSVersion`.
* **Claude Code on your `PATH`.** `Get-Command claude` should resolve. The
  script stops with a clear message if it does not.
* **Git**, to clone this repository.

### 1. Get the files

```powershell
cd C:\Users\<you>\code          # wherever you keep repositories
git clone https://github.com/alcademe67/Medik.git
cd Medik
```

If you already have a clone, `git pull` instead.

### 2. Allow the script to run

Windows blocks unsigned scripts by default. Allow them for this session only,
which is undone as soon as you close the window:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

If you downloaded the repository as a ZIP rather than cloning it, Windows also
marks the files as coming from the internet. Clear that with
`Unblock-File .\scripts\Install-TraderDevMcp.ps1`.

### 3. Store your token (skip if the server needs no authentication)

Type the token at a prompt rather than putting it in a command, so it does not
land in your PowerShell history:

```powershell
$ss = Read-Host -Prompt 'trader-dev token' -AsSecureString
$plain = [System.Net.NetworkCredential]::new('', $ss).Password
Set-Content -Path ~\.trader-dev-token -Value $plain -NoNewline
Remove-Variable plain, ss
```

`Read-Host -AsSecureString` needs a real console window; it cannot be piped
from another script. The script also accepts `$env:TRADER_DEV_TOKEN`, but it
will refuse a token passed as a parameter, because that would leak into your
history and into the process list.

### 4. Install

```powershell
.\scripts\Install-TraderDevMcp.ps1 -TokenFile ~\.trader-dev-token
```

Without a token, run `.\scripts\Install-TraderDevMcp.ps1` on its own.
Re-running is safe: an entry that already matches is reported and left alone.
Use `-Force` to remove and re-add it.

### 5. Confirm it worked

Restart Claude Code, then run `/mcp`. The server should appear in the list.

### If it fails

Run the built-in probe before assuming the token is at fault:

```powershell
.\scripts\Install-TraderDevMcp.ps1 -Diagnose -TokenFile ~\.trader-dev-token
```

This matters because Claude Code blames the `Authorization` header for *any*
403 on the connection, so a VPN, corporate proxy, or CDN refusing the request
is reported as `Server rejected the configured Authorization header`. The probe
requests the endpoint with and without the header and tells you which side is
actually refusing. Pass `-TokenFile` here too - without it, only the
unauthenticated half of the check runs. It changes no configuration.

If the endpoint turns out to want a different scheme, or OAuth rather than a
header:

```powershell
# API-key style header instead of a bearer token
.\scripts\Install-TraderDevMcp.ps1 -HeaderName X-API-Key -AuthScheme '' -TokenFile ~\.trader-dev-token

# no header at all, then authorize through /mcp
.\scripts\Install-TraderDevMcp.ps1 -Force
```

Setting an `Authorization` header disables Claude Code's OAuth fallback, so if
the server expects a browser sign-in, install without a token.

### Removing it

```powershell
.\scripts\Install-TraderDevMcp.ps1 -Uninstall
Remove-Item ~\.trader-dev-token
```

Deleting the entry does not revoke the token at whoever issued it, and Claude
Code snapshots its config on every change - so a token installed earlier can
still be sitting in `~\.claude\backups\`. Check with:

```powershell
Select-String -Path ~\.claude\backups\* -Pattern '<your token prefix>'
```

## Optional: the bash installer

`scripts/install-trader-dev-mcp.sh` is an alternative for Linux, macOS, and
Git Bash. It is **not** required on Windows - prefer the PowerShell script
above, which avoids two Git Bash problems: `claude.cmd` is not always
resolvable from bash, and Git for Windows rewrites the script to CRLF on
checkout unless the `.gitattributes` in this repository is honoured, after
which bash fails with `$'\r': command not found`.

Behaviour matches the PowerShell version flag for flag:

```bash
./scripts/install-trader-dev-mcp.sh                              # install, no auth
./scripts/install-trader-dev-mcp.sh --token-file ~/.trader-dev-token
./scripts/install-trader-dev-mcp.sh --diagnose --token-file ~/.trader-dev-token
./scripts/install-trader-dev-mcp.sh --force
./scripts/install-trader-dev-mcp.sh --uninstall
./scripts/install-trader-dev-mcp.sh --help
```

## A note on the endpoint

`https://mcp.trader.dev/sse` was unreachable from the environment these
scripts were written in, so its authentication scheme is unverified.
`Authorization: Bearer` is the default because it is the common case, not
because it was confirmed. If `-Diagnose` reports that the server rejects the
header, try `X-API-Key` or install without a token and use OAuth.
