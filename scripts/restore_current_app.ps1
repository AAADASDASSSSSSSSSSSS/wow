param(
    [string]$SharedRoot = "E:\agent-service-toolkit-main\agent-service-toolkit_frame\agent-service-toolkit-main",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$SharedRoot = (Resolve-Path $SharedRoot).Path
$SharedEnv = Join-Path $SharedRoot ".env"
$SharedCompose = Join-Path $SharedRoot "compose.yaml"
$Project = "agent-service-toolkit-main"

if (-not (Test-Path -LiteralPath $SharedEnv)) {
    throw "Shared environment file is missing: $SharedEnv"
}

$base = @(
    "compose", "-p", $Project,
    "--project-directory", $SharedRoot,
    "--env-file", $SharedEnv,
    "-f", $SharedCompose
)

if (-not $SkipBuild) {
    & docker @base --profile control-plane build agent_service control_plane frontend
    if ($LASTEXITCODE -ne 0) { throw "Current application build failed." }
}

& docker @base --profile control-plane --profile identity up -d `
    agent_service temporal_worker kafka_audit_relay control_plane frontend oauth2_proxy
if ($LASTEXITCODE -ne 0) { throw "Current application restore failed." }

Write-Host "Current product kernel has been restored at http://localhost:8088"

