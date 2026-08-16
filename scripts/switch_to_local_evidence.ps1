param(
    [string]$SharedRoot = "E:\agent-service-toolkit-main\agent-service-toolkit_frame\agent-service-toolkit-main",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$TargetRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SharedRoot = (Resolve-Path $SharedRoot).Path
$SharedEnv = Join-Path $SharedRoot ".env"
$SharedCompose = Join-Path $SharedRoot "compose.yaml"
$TargetCompose = Join-Path $TargetRoot "compose.yaml"
$Project = "agent-service-toolkit-main"
$env:RATSNEST_SHARED_ENV_FILE = $SharedEnv

foreach ($required in @($SharedEnv, $SharedCompose, $TargetCompose)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required shared-stack file is missing: $required"
    }
}

function Invoke-Compose {
    param(
        [string]$ProjectDirectory,
        [string]$ComposeFile,
        [string[]]$Arguments
    )

    & docker compose `
        -p $Project `
        --project-directory $ProjectDirectory `
        --env-file $SharedEnv `
        -f $ComposeFile `
        @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed: $($Arguments -join ' ')"
    }
}

Write-Host "Ensuring the shared infrastructure is running..."
Invoke-Compose $SharedRoot $SharedCompose @(
    # Explicitly named services are allowed to run even when their profiles are
    # inactive. Do not activate `identity` here: oauth2_proxy depends on the
    # control-plane-only frontend, which is intentionally switched later.
    "up", "-d",
    "postgres", "redis", "kafka", "kafka_init", "temporal",
    "keycloak", "minio", "minio_init"
)

if (-not $SkipBuild) {
    Write-Host "Building only the local-evidence application tier..."
    Invoke-Compose $TargetRoot $TargetCompose @(
        "--profile", "control-plane",
        "build", "agent_service", "kafka_audit_relay", "control_plane", "frontend"
    )
}

Write-Host "Stopping the current application tier while keeping shared infrastructure alive..."
Invoke-Compose $SharedRoot $SharedCompose @(
    "stop", "agent_service", "temporal_worker", "kafka_audit_relay",
    "control_plane", "frontend", "oauth2_proxy"
)

Write-Host "Preparing the target workspace..."
Invoke-Compose $TargetRoot $TargetCompose @(
    "run", "--rm", "--no-deps", "ratsnest_workspace_init"
)

Write-Host "Starting the local-evidence Agent Runtime..."
Invoke-Compose $TargetRoot $TargetCompose @(
    "up", "-d", "--no-deps", "agent_service", "kafka_audit_relay"
)

$AgentContainer = (& docker compose `
    -p $Project `
    --project-directory $TargetRoot `
    --env-file $SharedEnv `
    -f $TargetCompose `
    ps -q agent_service | Select-Object -First 1).Trim()
if (-not $AgentContainer) {
    throw "The local-evidence Agent Runtime container was not created."
}

$deadline = [DateTime]::UtcNow.AddSeconds(75)
do {
    $health = (& docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $AgentContainer).Trim()
    if ($health -eq "healthy") { break }
    if ($health -eq "exited" -or $health -eq "dead") {
        throw "The local-evidence Agent Runtime stopped before becoming healthy."
    }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $deadline)

if ($health -ne "healthy") {
    throw "The local-evidence Agent Runtime did not become healthy within 75 seconds."
}

Write-Host "Starting the shared product shell against the local-evidence kernel..."
Invoke-Compose $TargetRoot $TargetCompose @(
    "--profile", "control-plane",
    "--profile", "identity",
    "up", "-d", "--no-deps", "control_plane", "frontend", "oauth2_proxy"
)

Write-Host "Local-evidence product shell is available at http://localhost:8088"
Write-Host "Shared secrets remain in $SharedEnv and were not copied."
