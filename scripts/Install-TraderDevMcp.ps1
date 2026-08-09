<#
.SYNOPSIS
Install the trader-dev MCP server into Claude Code.

.DESCRIPTION
The PowerShell counterpart to install-trader-dev-mcp.sh, for Windows hosts
where Git Bash is awkward. User scope is the "global" one: the server becomes
available in all of your projects and is stored in ~/.claude.json.

Authentication is optional. Supply a token and the server is registered with
an "Authorization: Bearer <token>" header; omit it and the server is
registered unauthenticated. The token is read from the environment or from a
file, never from a parameter, which would leak it into PSReadLine history.

.EXAMPLE
.\Install-TraderDevMcp.ps1
Installs at user scope with no authentication.

.EXAMPLE
$env:TRADER_DEV_TOKEN = 'pk_...'; .\Install-TraderDevMcp.ps1
Installs with a bearer token taken from the environment.

.EXAMPLE
.\Install-TraderDevMcp.ps1 -TokenFile ~/.trader-dev-token

.EXAMPLE
.\Install-TraderDevMcp.ps1 -Diagnose -TokenFile ~/.trader-dev-token
Probes the endpoint and explains what a failure actually means, changing no
configuration. Exits 0 when the endpoint is usable as configured, 1 otherwise.
#>
[CmdletBinding()]
param(
    [string]   $Name        = 'trader-dev',
    [string]   $Url         = 'https://mcp.trader.dev/sse',
    [string]   $Transport   = 'sse',
    [ValidateSet('user', 'project', 'local')]
    [string]   $Scope       = 'user',
    [string]   $TokenFile,
    [string]   $HeaderName  = 'Authorization',
    [AllowEmptyString()]
    [string]   $AuthScheme  = 'Bearer',
    [int]      $TimeoutSec  = 20,
    [switch]   $Force,
    [switch]   $Uninstall,
    [switch]   $Diagnose,
    # Present only to reject it: a token passed as a parameter lands in
    # PSReadLine's history file. Use $env:TRADER_DEV_TOKEN or -TokenFile.
    [string]   $Token
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Note { param([string]$Message = '') Write-Host $Message }
function Stop-WithError {
    param([string]$Message)
    Write-Host "error: $Message" -ForegroundColor Red
    exit 1
}

if ($PSBoundParameters.ContainsKey('Token')) {
    Stop-WithError 'refusing -Token: pass the secret via $env:TRADER_DEV_TOKEN or -TokenFile instead'
}

# Read from the environment so the secret never appears in the command line.
$secret = if ($env:TRADER_DEV_TOKEN) { $env:TRADER_DEV_TOKEN } else { '' }

if ($TokenFile) {
    $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($TokenFile)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        Stop-WithError "token file not found: $TokenFile"
    }
    # Trim whitespace; a trailing newline would be sent as part of the header.
    $secret = ((Get-Content -LiteralPath $resolved -Raw) -replace '\s+$', '')
    if ([string]::IsNullOrWhiteSpace($secret)) {
        Stop-WithError "token file is empty: $TokenFile"
    }
}

$headerValue = if ($AuthScheme) { "$AuthScheme $secret" } else { $secret }

# --------------------------------------------------------------------------
# Diagnose: probe the endpoint directly and change no configuration.
# --------------------------------------------------------------------------
if ($Diagnose) {
    Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue

    function Get-RespHeader {
        param($Response, [string]$HeaderKey)
        $values = $null
        if ($Response.Headers.TryGetValues($HeaderKey, [ref]$values)) { return ($values -join ', ') }
        if ($Response.Content -and $Response.Content.Headers.TryGetValues($HeaderKey, [ref]$values)) {
            return ($values -join ', ')
        }
        return ''
    }

    function Invoke-Probe {
        param([string]$AuthHeader)
        $outcome = [pscustomobject]@{ Code = 0; Server = ''; CfRay = ''; Failure = '' }
        $handler = [System.Net.Http.HttpClientHandler]::new()
        $handler.AllowAutoRedirect = $false
        $client = [System.Net.Http.HttpClient]::new($handler)
        try {
            $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
            $request = [System.Net.Http.HttpRequestMessage]::new(
                [System.Net.Http.HttpMethod]::Get, $Url)
            # Both types, matching what an MCP client negotiates. Sending only
            # text/event-stream makes strict servers answer 415, which would
            # look like a fault in the endpoint rather than a probe artifact.
            $request.Headers.TryAddWithoutValidation(
                'Accept', 'application/json, text/event-stream') | Out-Null
            if ($AuthHeader) {
                $request.Headers.TryAddWithoutValidation($HeaderName, $AuthHeader) | Out-Null
            }
            # ResponseHeadersRead returns as soon as the headers arrive. A
            # healthy SSE endpoint answers 200 and then holds the stream open,
            # so reading the body would block until the timeout.
            $response = $client.SendAsync(
                $request,
                [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
            ).GetAwaiter().GetResult()
            $outcome.Code   = [int]$response.StatusCode
            $outcome.Server = Get-RespHeader $response 'Server'
            $outcome.CfRay  = Get-RespHeader $response 'CF-Ray'
            $response.Dispose()
        } catch {
            $inner = $_.Exception
            while ($inner.InnerException) { $inner = $inner.InnerException }
            $outcome.Failure = $inner.Message
        } finally {
            $client.Dispose()
            $handler.Dispose()
        }
        return $outcome
    }

    # A CDN or WAF in front of the origin blocks with the same status codes the
    # origin would use, so the response headers are the only way to tell "the
    # edge refused you" from "the MCP server refused you".
    function Show-EdgeMarker {
        param([int]$Observed, $Probe)
        if (-not $Probe.Server -and -not $Probe.CfRay) { return }
        $detail = if ($Probe.Server) { " (server: $($Probe.Server))" } else { '' }
        if ($Probe.CfRay) { $detail += ", cf-ray: $($Probe.CfRay)" }
        Write-Note ''
        Write-Note "Served via an edge/CDN$detail."
        if ($Observed -in 403, 429, 503) {
            Write-Note "A $Observed is a status the edge itself can return, so this may be CDN"
            Write-Note 'bot protection refusing a non-browser client, with the MCP server'
            Write-Note 'behind it never seeing the request.'
        } else {
            Write-Note "A $Observed is normally relayed from the origin rather than produced by"
            Write-Note 'the edge, so the CDN is probably not the cause here.'
        }
    }

    Write-Note "Probing $Url (timeout ${TimeoutSec}s)"
    foreach ($proxyVar in 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy') {
        if (Get-Item -Path "env:$proxyVar" -ErrorAction SilentlyContinue) {
            Write-Note "  warning: $proxyVar is set, so a proxy sits between you and the"
            Write-Note "           endpoint. A proxy's own refusal is indistinguishable"
            Write-Note "           from the server's here - weigh the verdict accordingly."
            break
        }
    }

    $bare = Invoke-Probe -AuthHeader ''
    Write-Note ("  without auth header:      HTTP {0}" -f $(if ($bare.Code) { $bare.Code } else { '000' }))

    $auth = $null
    if ($secret) {
        $auth = Invoke-Probe -AuthHeader $headerValue
        Write-Note ("  with $HeaderName header: HTTP {0}" -f $(if ($auth.Code) { $auth.Code } else { '000' }))
    } else {
        Write-Note '  with auth header:         skipped (no token supplied)'
    }

    Write-Note ''
    if ($bare.Code -eq 0 -and (-not $secret -or $auth.Code -eq 0)) {
        Write-Note "VERDICT: no HTTP response at all ($($bare.Failure))."
        Write-Note 'This is not an authentication problem - the connection never completed,'
        Write-Note 'so no credential was ever sent. Check DNS, TLS interception, VPN, or a'
        Write-Note 'proxy denying CONNECT.'
        exit 1
    }

    if (-not $secret) {
        if ($bare.Code -ge 200 -and $bare.Code -lt 300) {
            Write-Note 'VERDICT: endpoint is reachable and serves requests without auth.'
            exit 0
        }
        if ($bare.Code -eq 401) {
            Write-Note 'VERDICT: endpoint is reachable and requires authentication.'
            Write-Note 'Supply a token with -TokenFile, or install without one and let'
            Write-Note 'OAuth run via /mcp.'
            exit 1
        }
        Write-Note "VERDICT: endpoint returned $($bare.Code) to an unauthenticated request."
        Write-Note 'Re-run with -TokenFile to test an authenticated one.'
        exit 1
    }

    if ($auth.Code -ge 200 -and $auth.Code -lt 300) {
        Write-Note "VERDICT: the endpoint accepted the $HeaderName header (HTTP $($auth.Code))."
        Write-Note 'If Claude Code still reports a failure, the problem is its transport'
        Write-Note 'setting rather than the credential - try -Transport http.'
        exit 0
    }
    if ($auth.Code -eq 401) {
        # Checked before the identical-response case below: 401 both with and
        # without the header still means the credential failed to satisfy the
        # server, the opposite of what "identical" would imply.
        Write-Note 'VERDICT: the server returned 401 to the authenticated request.'
        Write-Note 'The credential was sent and did not satisfy it - the token is wrong,'
        Write-Note 'expired, revoked, or carries the wrong scheme.'
        Show-EdgeMarker -Observed $auth.Code -Probe $auth
        exit 1
    }
    if ($bare.Code -eq $auth.Code) {
        Write-Note "VERDICT: identical response ($($auth.Code)) with and without the header."
        Write-Note 'The rejection is therefore NOT about your token - the request is'
        Write-Note 'refused either way. Suspect a proxy or VPN in the path, an IP-level'
        Write-Note 'block, or simply the wrong URL.'
        Show-EdgeMarker -Observed $auth.Code -Probe $auth
        exit 1
    }
    if ($bare.Code -ge 200 -and $bare.Code -lt 300) {
        Write-Note "VERDICT: works WITHOUT the header ($($bare.Code)), fails WITH it ($($auth.Code))."
        Write-Note 'The header is breaking an endpoint that does not want one.'
        Write-Note 'Reinstall without a token:  .\Install-TraderDevMcp.ps1 -Force'
        exit 1
    }

    Write-Note "VERDICT: the server sees the header and rejects it (HTTP $($auth.Code),"
    Write-Note "versus $($bare.Code) unauthenticated). The credential or the scheme is wrong."
    Show-EdgeMarker -Observed $auth.Code -Probe $auth
    exit 1
}

# --------------------------------------------------------------------------
# Install / uninstall
# --------------------------------------------------------------------------
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Stop-WithError "the 'claude' CLI is not on PATH. Install Claude Code first: https://claude.com/claude-code"
}

# `claude mcp get` exits non-zero when the server is not configured.
$existingOutput = (& claude mcp get $Name 2>&1 | Out-String)
$exists = ($LASTEXITCODE -eq 0)

if ($Uninstall) {
    if (-not $exists) {
        Write-Note "Nothing to do: '$Name' is not configured at any scope."
        exit 0
    }
    & claude mcp remove $Name -s $Scope
    Write-Note "Removed '$Name' from $Scope config."
    Write-Note 'Any token stored with it is gone from the config, but is still valid'
    Write-Note 'at the issuer until you revoke it there.'
    Write-Note ''
    Write-Note 'Claude Code snapshots the config on every change, so a token installed'
    Write-Note 'earlier can outlive this removal in a backup. Check for stray copies:'
    Write-Note '  Select-String -Path ~/.claude/backups/* -Pattern ''<your token prefix>'''
    exit 0
}

if ($exists) {
    if ($secret) {
        # The stored header is never read back, so a matching URL says nothing
        # about whether the stored credential matches this one. Always replace.
        Write-Note "Replacing existing '$Name' entry (stored credentials cannot be verified)..."
    } elseif (-not $Force -and $existingOutput.Contains($Url)) {
        Write-Note "'$Name' is already configured with $Url - nothing to do."
        Write-Note 'Re-run with -Force to remove and re-add it.'
        exit 0
    } else {
        Write-Note "Replacing existing '$Name' entry..."
    }
    # Best-effort: the entry may live at a different scope than the one we are
    # about to write to, in which case this is a no-op.
    & claude mcp remove $Name -s $Scope *> $null
}

# The positional name/URL must precede the flags: `claude mcp add` declares
# --header as variadic, so it swallows any positional argument that follows it.
$addArgs = @('mcp', 'add', $Name, $Url, '--transport', $Transport, '--scope', $Scope)
if ($secret) {
    $addArgs += @('--header', "${HeaderName}: $headerValue")
    Write-Note "Using auth header '$HeaderName' (token supplied, $($secret.Length) chars)."
} else {
    Write-Note 'No token supplied - registering without an auth header.'
    Write-Note 'Set $env:TRADER_DEV_TOKEN or pass -TokenFile to add one.'
}

& claude @addArgs
if ($LASTEXITCODE -ne 0) { Stop-WithError "claude mcp add failed with exit code $LASTEXITCODE" }

Write-Note ''
Write-Note 'Configured entry:'
# `claude mcp get` prints configured header values in the clear, so redact
# anything credential-shaped before it reaches the terminal or a CI log.
$entry = (& claude mcp get $Name 2>&1 | Out-String) -split "`n"
foreach ($line in $entry) {
    $line.TrimEnd() -replace '^(\s*[A-Za-z-]+:\s*(?:Bearer\s+)?)[A-Za-z0-9._~+/-]{12,}=*$', '$1<redacted>'
}

if ($secret) {
    Write-Note ''
    Write-Note 'Note: Claude Code stores this header in plain text in its config file'
    Write-Note '(~/.claude.json). Keep that file readable only by your account.'
}

Write-Note ''
Write-Note 'Next steps:'
Write-Note '  1. Restart Claude Code so it picks up the new server.'
Write-Note '  2. Run /mcp inside Claude Code to check its connection status.'
Write-Note ''
Write-Note 'If the status looks wrong, confirm the host is reachable before assuming'
Write-Note 'it is an auth problem:'
Write-Note "  .\Install-TraderDevMcp.ps1 -Diagnose"
Write-Note ''
Write-Note "To undo: .\Install-TraderDevMcp.ps1 -Uninstall -Scope $Scope"
