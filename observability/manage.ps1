param(
    [ValidateSet("start", "stop", "status", "config")]
    [string]$Action = "start",
    [ValidateSet("direct", "logstash")]
    [string]$LogPipeline = "direct"
)

$ErrorActionPreference = "Stop"
$ComposeArguments = @(
    "compose",
    "--env-file", (Join-Path $PSScriptRoot ".env"),
    "-f", (Join-Path $PSScriptRoot "docker-compose.observability.yml")
)

if (-not (Test-Path (Join-Path $PSScriptRoot ".env"))) {
    throw "Missing observability/.env. Copy observability/.env.example and set local credentials."
}

switch ($Action) {
    "start" {
        & docker @ComposeArguments --profile $LogPipeline up -d
    }
    "stop" {
        & docker @ComposeArguments --profile direct --profile logstash down
    }
    "status" {
        & docker @ComposeArguments --profile direct --profile logstash ps
    }
    "config" {
        & docker @ComposeArguments --profile $LogPipeline config --quiet
    }
}

if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose action '$Action' failed with exit code $LASTEXITCODE."
}
