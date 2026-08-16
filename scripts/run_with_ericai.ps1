# Launch helper for running this project against EricAI's LLM.
#
# EricAI authenticates with SSO device codes and issues short-lived bearer
# tokens, so a static key in .env goes stale. This refreshes the token, exports
# the proxy-bypass and corporate CA settings that the OpenAI-compatible client
# needs as real process environment variables, then runs the command you pass.
#
#   .\scripts\run_with_ericai.ps1 python src/run_service.py
#   .\scripts\run_with_ericai.ps1 python scripts/run_ratsnest_case.py _case_stm32.txt

param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Command)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    # The interpreter that has ``ericai`` installed, probed rather than assumed.
    # The SSO client is a corporate package outside this repo's lock file, so it
    # lives in whichever environment was provisioned with corporate access — not
    # necessarily the one that runs the tests. Hard-coding ".venv" failed with a
    # ModuleNotFoundError raised from inside the token refresh, which reads like
    # an auth problem and is not one. Set RATSNESTPRO_VENV to override.
    $candidates = @()
    if ($env:RATSNESTPRO_VENV) {
        $candidates += (Join-Path $env:RATSNESTPRO_VENV "Scripts\python.exe")
    }
    $candidates += (Join-Path $root ".venv\Scripts\python.exe")
    $candidates += (Join-Path $HOME ".venvs\rn-generality\Scripts\python.exe")

    $venvPython = $null
    # A failing probe writes a traceback to stderr, which "Stop" would turn into
    # a terminating error for a native command. Probing is allowed to fail.
    $probePreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    foreach ($candidate in $candidates) {
        if (-not (Test-Path $candidate)) { continue }
        & $candidate -c "import ericai" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $venvPython = $candidate; break }
    }
    $ErrorActionPreference = $probePreference
    if (-not $venvPython) {
        throw ("No interpreter with 'ericai' installed. Tried:`n  " +
               ($candidates -join "`n  ") +
               "`nSet RATSNESTPRO_VENV to the environment that has it.")
    }
    Write-Host "Interpreter: $venvPython" -ForegroundColor DarkGray

    # The CA bundle ships inside the ericai package, so it follows the
    # interpreter rather than the repository.
    $caBundle = Join-Path (Split-Path -Parent (Split-Path -Parent $venvPython)) `
        "Lib\site-packages\ericai\egad-certifi-combined.pemfile"

    # EricAI gateway must bypass the corporate proxy and trust the corporate CA.
    $env:NO_PROXY = ".gic.ericsson.se,.sero.gic.ericsson.se,localhost,127.0.0.1"
    $env:no_proxy = $env:NO_PROXY
    $env:SSL_CERT_FILE = $caBundle
    $env:REQUESTS_CA_BUNDLE = $caBundle
    $env:PYTHONPATH = "$root\src;$root\src\RatsNestPro-main\RatsNestPro-main\src"
    $env:PYTHONUTF8 = "1"

    # EricAI resolves its device-code AuthRecord relative to the CWD, so seed it
    # from the stable per-user copy; otherwise a login done elsewhere is invisible
    # here and the non-interactive refresh fails.
    $record = Join-Path $root ".ericai_authrecord"
    $canonical = Join-Path $HOME ".ericai_authrecord"
    if (-not (Test-Path $record) -and (Test-Path $canonical)) {
        Copy-Item $canonical $record -Force
        Write-Host "Seeded .ericai_authrecord from $canonical" -ForegroundColor DarkGray
    }

    Write-Host "Refreshing EricAI SSO token..." -ForegroundColor Cyan
    $token = & $venvPython -c "from ericai.client import fastToken, LastToken; print(fastToken(LastToken()) or '')"
    if (-not $token) { throw "Could not obtain an EricAI token. Run 'ericai --ericsson-test-connectivity' once to log in." }
    if (Test-Path $record) { Copy-Item $record $canonical -Force }

    # Refresh only the credential line so the rest of .env is preserved.
    $envPath = Join-Path $root ".env"
    $lines = Get-Content $envPath | Where-Object { $_ -notmatch "^COMPATIBLE_API_KEY=" }
    $lines += "COMPATIBLE_API_KEY=$($token.Trim())"
    Set-Content -Path $envPath -Value $lines -Encoding UTF8
    Write-Host "Token refreshed (length $($token.Trim().Length)); .env updated." -ForegroundColor Green

    if (-not $Command) { Write-Host "Environment ready. Pass a command to run."; return }

    $exe = $Command[0]
    if ($exe -eq "python") { $exe = $venvPython }
    # Assign outside the ``if`` so the value stays an array. A single trailing
    # argument makes the range operator yield one element, an ``if`` expression
    # unrolls that to a bare [string], and splatting a string forwards it one
    # character at a time: "python src/run_service.py" reached Python as argv
    # ['s','r','c',...] and it reported "can't open file '<cwd>\s'".
    $rest = [string[]]@()
    if ($Command.Count -gt 1) { $rest = [string[]]$Command[1..($Command.Count - 1)] }
    # A tolerated warning on stderr (e.g. one web-search retry) must not abort the
    # whole run, which "Stop" would do for any native-command stderr output.
    $ErrorActionPreference = "Continue"
    & $exe @rest
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
