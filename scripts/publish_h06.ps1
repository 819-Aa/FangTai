[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Preflight", "StartStores", "Initialize", "StartApi", "Verify")]
    [string]$Action,

    [string]$Manifest,

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Script:H06Project = "food_agent_v2_h06"
$Script:H06Containers = @{
    mysql = "food_agent_v2_h06_mysql"
    qdrant = "food_agent_v2_h06_qdrant"
    redis = "food_agent_v2_h06_redis"
}
$Script:H05Containers = @{
    mysql = "food_agent_v2_h05_mysql"
    qdrant = "food_agent_v2_h05_qdrant"
    redis = "food_agent_v2_h05_redis"
}
$Script:PortMap = @{
    mysql = 3306
    qdrant_rest = 6333
    qdrant_grpc = 6334
    redis = 6379
    api = 8000
}
$Script:TestDockerState = $null
$Script:TestDockerSequence = $null
$Script:TestDockerSequenceIndex = 0
$Script:TestHttpState = $null
$Script:TestHttpSequence = $null
$Script:TestHttpSequenceIndex = @{}

function Write-FailureAndExit {
    param(
        [string]$Message,
        [int]$Code = 1
    )

    throw [System.InvalidOperationException]::new($Message)
}

function Get-RepoRoot {
    if ($env:H06_TEST_ROOT) {
        return [System.IO.Path]::GetFullPath($env:H06_TEST_ROOT)
    }
    return (Split-Path -Parent $PSScriptRoot)
}

function Get-MainRepoRoot {
    param([string]$RepoRoot)

    $parent = Split-Path -Parent $RepoRoot
    if ((Split-Path -Leaf $parent) -eq ".worktrees") {
        return (Split-Path -Parent $parent)
    }
    return $RepoRoot
}

function Import-DotEnvFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $key, $value = $trimmed.Split("=", 2)
        $cleanKey = $key.Trim()
        $cleanValue = $value.Trim().Trim("'`"")
        if (-not $cleanKey) {
            continue
        }
        if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($cleanKey))) {
            continue
        }
        [Environment]::SetEnvironmentVariable($cleanKey, $cleanValue)
    }
}

function Import-Environment {
    param([string]$RepoRoot)

    Import-DotEnvFile -Path (Join-Path $RepoRoot ".env")
    $mainRepoRoot = Get-MainRepoRoot -RepoRoot $RepoRoot
    if ($mainRepoRoot -ne $RepoRoot) {
        Import-DotEnvFile -Path (Join-Path $mainRepoRoot ".env")
    }
}

function Initialize-TestState {
    if ($env:H06_TEST_DOCKER_STATE) {
        $Script:TestDockerState = Get-Content -Raw -LiteralPath $env:H06_TEST_DOCKER_STATE | ConvertFrom-Json
    }
    if ($env:H06_TEST_DOCKER_SEQUENCE) {
        $Script:TestDockerSequence = Get-Content -Raw -LiteralPath $env:H06_TEST_DOCKER_SEQUENCE | ConvertFrom-Json
        $Script:TestDockerSequenceIndex = 0
    }
    if ($env:H06_TEST_HTTP_STATE) {
        $Script:TestHttpState = Get-Content -Raw -LiteralPath $env:H06_TEST_HTTP_STATE | ConvertFrom-Json
    }
    if ($env:H06_TEST_HTTP_SEQUENCE) {
        $Script:TestHttpSequence = Get-Content -Raw -LiteralPath $env:H06_TEST_HTTP_SEQUENCE | ConvertFrom-Json
        $Script:TestHttpSequenceIndex = @{}
    }
}

function Set-H06Environment {
    [Environment]::SetEnvironmentVariable("MYSQL_CONTAINER_NAME", $Script:H06Containers.mysql)
    [Environment]::SetEnvironmentVariable("QDRANT_CONTAINER_NAME", $Script:H06Containers.qdrant)
    [Environment]::SetEnvironmentVariable("REDIS_CONTAINER_NAME", $Script:H06Containers.redis)
    [Environment]::SetEnvironmentVariable("MYSQL_HOST", "127.0.0.1")
    [Environment]::SetEnvironmentVariable("MYSQL_PORT", [string]$Script:PortMap.mysql)
    [Environment]::SetEnvironmentVariable("QDRANT_HOST", "127.0.0.1")
    [Environment]::SetEnvironmentVariable("QDRANT_REST_PORT", [string]$Script:PortMap.qdrant_rest)
    [Environment]::SetEnvironmentVariable("QDRANT_GRPC_PORT", [string]$Script:PortMap.qdrant_grpc)
    [Environment]::SetEnvironmentVariable("REDIS_HOST", "127.0.0.1")
    [Environment]::SetEnvironmentVariable("REDIS_PORT", [string]$Script:PortMap.redis)
    [Environment]::SetEnvironmentVariable("API_HOST", "127.0.0.1")
    [Environment]::SetEnvironmentVariable("API_PORT", [string]$Script:PortMap.api)
    [Environment]::SetEnvironmentVariable("RAG_WARMUP_ON_STARTUP", "true")
}

function Get-NormalizedPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path)
}

function Get-FileSha256 {
    param([string]$Path)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try {
            $hashBytes = $sha.ComputeHash($stream)
        }
        finally {
            $stream.Dispose()
        }
    }
    finally {
        $sha.Dispose()
    }
    return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
}

function Test-PathWithinRoot {
    param(
        [string]$Path,
        [string]$Root
    )

    $normalizedPath = (Get-NormalizedPath -Path $Path).TrimEnd("\")
    $normalizedRoot = (Get-NormalizedPath -Path $Root).TrimEnd("\")
    $comparison = [System.StringComparison]::OrdinalIgnoreCase
    if ($normalizedPath.Equals($normalizedRoot, $comparison)) {
        return $true
    }
    return $normalizedPath.StartsWith("$normalizedRoot\", $comparison)
}

function Resolve-ManifestInfo {
    param([string]$ManifestPath)

    if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
        Write-FailureAndExit -Message "Manifest is required."
    }

    $repoRoot = Get-RepoRoot
    $candidatePath = $ManifestPath
    if (-not [System.IO.Path]::IsPathRooted($candidatePath)) {
        $candidatePath = Join-Path $repoRoot $candidatePath
    }
    $resolvedManifest = Get-NormalizedPath -Path $candidatePath
    if (-not (Test-Path -LiteralPath $resolvedManifest)) {
        Write-FailureAndExit -Message "Manifest not found: $resolvedManifest"
    }

    $allowedRoots = @(
        (Join-Path $repoRoot ".staging\h06-nutrition-complete"),
        (Join-Path $repoRoot ".staging\h06-nutrition-complete-v3")
    )
    if (-not ($allowedRoots | Where-Object { Test-PathWithinRoot -Path $resolvedManifest -Root $_ })) {
        Write-FailureAndExit -Message "Manifest must stay within .staging/h06-nutrition-complete or .staging/h06-nutrition-complete-v3."
    }

    $manifestJson = Get-Content -Raw -LiteralPath $resolvedManifest | ConvertFrom-Json
    if (-not $manifestJson.build_id) {
        Write-FailureAndExit -Message "Manifest build_id is required."
    }

    return [pscustomobject]@{
        repo_root = $repoRoot
        path = $resolvedManifest
        sha256 = Get-FileSha256 -Path $resolvedManifest
        build_id = [string]$manifestJson.build_id
        builder_version = [string]$manifestJson.builder_version
        raw = $manifestJson
    }
}

function Get-ContainerStatus {
    param(
        [string]$Name,
        $DockerState = $null
    )

    $state = $DockerState
    if (-not $state) {
        $state = $Script:TestDockerState
    }

    if ($state) {
        $container = $state.containers.PSObject.Properties[$Name]
        if (-not $container) {
            return $null
        }
        $value = $container.Value
        return [pscustomobject]@{
            name = $Name
            running = [bool]$value.running
            health = [string]$value.health
            ports = $value.ports
        }
    }

    $inspectOutput = docker inspect $Name 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $inspectOutput) {
        return $null
    }
    $inspect = $inspectOutput | ConvertFrom-Json
    if ($inspect -is [System.Array]) {
        $inspect = $inspect[0]
    }
    $portMap = @{}
    if ($inspect.NetworkSettings.Ports) {
        foreach ($property in $inspect.NetworkSettings.Ports.PSObject.Properties) {
            $binding = $property.Value
            if ($binding -and $binding.Count -gt 0 -and $binding[0].HostPort) {
                $portMap[$property.Name] = [int]$binding[0].HostPort
            }
        }
    }
    $health = ""
    if ($inspect.State.Health) {
        $health = [string]$inspect.State.Health.Status
    }
    return [pscustomobject]@{
        name = $Name
        running = [bool]$inspect.State.Running
        health = $health
        ports = $portMap
    }
}

function Get-DockerSequenceState {
    if (-not $Script:TestDockerSequence) {
        return $null
    }
    $index = [Math]::Min($Script:TestDockerSequenceIndex, $Script:TestDockerSequence.Count - 1)
    $state = $Script:TestDockerSequence[$index]
    if ($Script:TestDockerSequenceIndex -lt ($Script:TestDockerSequence.Count - 1)) {
        $Script:TestDockerSequenceIndex += 1
    }
    return $state
}

function Get-PortOccupant {
    param([int]$Port)

    if ($Script:TestDockerState) {
        $entry = $Script:TestDockerState.ports.PSObject.Properties[[string]$Port]
        if ($entry) {
            return $entry.Value
        }
        return $null
    }

    $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if (-not $connections -or $connections.Count -eq 0) {
        return $null
    }
    return [pscustomobject]@{
        kind = "process"
        name = "listener"
    }
}

function Assert-StorePortRestorable {
    param(
        [int]$Port,
        [string]$ExpectedContainerName,
        [string]$ExpectedContainerPort
    )

    $occupant = Get-PortOccupant -Port $Port
    if (-not $occupant) {
        return
    }

    $container = Get-ContainerStatus -Name $ExpectedContainerName
    if ($container -and $container.running -and $container.health -eq "healthy") {
        $mappedPort = $container.ports.$ExpectedContainerPort
        if ($mappedPort -and [int]$mappedPort -eq $Port) {
            return
        }
    }

    Write-FailureAndExit -Message "Port $Port is occupied by unexpected listener."
}

function Assert-ApiPortFree {
    $occupant = Get-PortOccupant -Port $Script:PortMap.api
    if ($occupant) {
        Write-FailureAndExit -Message "Port $($Script:PortMap.api) is occupied by unexpected listener."
    }
}

function Get-H05Status {
    $status = @{}
    foreach ($service in $Script:H05Containers.Keys) {
        $containerName = $Script:H05Containers[$service]
        $container = Get-ContainerStatus -Name $containerName
        if ($container) {
            $status[$containerName] = @{
                running = $container.running
                health = $container.health
            }
        }
        else {
            $status[$containerName] = @{
                running = $false
                health = "missing"
            }
        }
    }
    return $status
}

function Get-JsonPropertyValue {
    param(
        $Object,
        [string]$Name,
        $Default = $null
    )

    if (-not $Object) {
        return $Default
    }
    $property = $Object.PSObject.Properties[$Name]
    if (-not $property) {
        return $Default
    }
    return $property.Value
}

function Get-PollIntervalSeconds {
    $override = [Environment]::GetEnvironmentVariable("H06_TEST_POLL_INTERVAL_SECONDS")
    if (-not [string]::IsNullOrWhiteSpace($override)) {
        return [double]$override
    }
    return 2.0
}

function Get-TimeoutSeconds {
    param([ValidateSet("stores", "api")] [string]$Kind)

    if ($Kind -eq "stores") {
        $override = [Environment]::GetEnvironmentVariable("H06_TEST_STORE_TIMEOUT_SECONDS")
        if (-not [string]::IsNullOrWhiteSpace($override)) {
            return [double]$override
        }
        return 120.0
    }

    $apiOverride = [Environment]::GetEnvironmentVariable("H06_TEST_API_TIMEOUT_SECONDS")
    if (-not [string]::IsNullOrWhiteSpace($apiOverride)) {
        return [double]$apiOverride
    }
    return 90.0
}

function Get-WaitAttempts {
    param([ValidateSet("stores", "api")] [string]$Kind)

    $timeoutSeconds = Get-TimeoutSeconds -Kind $Kind
    $pollSeconds = Get-PollIntervalSeconds
    if ($pollSeconds -le 0) {
        return 1
    }
    return [int][Math]::Floor($timeoutSeconds / $pollSeconds) + 1
}

function Wait-NextAttempt {
    if (-not $env:H06_TEST_ROOT) {
        Start-Sleep -Seconds (Get-PollIntervalSeconds)
    }
}

function Wait-ForHealthyContainers {
    $attempts = Get-WaitAttempts -Kind "stores"
    for ($attempt = 0; $attempt -lt $attempts; $attempt += 1) {
        $dockerState = Get-DockerSequenceState
        if (-not $dockerState) {
            $dockerState = $Script:TestDockerState
        }

        $healthy = $true
        $containers = @{}
        foreach ($service in $Script:H06Containers.Keys) {
            $containerName = $Script:H06Containers[$service]
            $container = Get-ContainerStatus -Name $containerName -DockerState $dockerState
            if (-not $container -or -not $container.running -or $container.health -ne "healthy") {
                $healthy = $false
            }
            if ($container) {
                $containers[$containerName] = @{
                    running = $container.running
                    health = $container.health
                }
            }
        }

        if ($healthy) {
            return $containers
        }
        if ($attempt -lt ($attempts - 1)) {
            Wait-NextAttempt
        }
    }

    Write-FailureAndExit -Message "H06 stores did not become healthy before timeout."
}

function Invoke-Preflight {
    param(
        [pscustomobject]$ManifestInfo,
        [switch]$DryRunOnly
    )

    Assert-StorePortRestorable -Port $Script:PortMap.mysql -ExpectedContainerName $Script:H06Containers.mysql -ExpectedContainerPort "3306/tcp"
    Assert-StorePortRestorable -Port $Script:PortMap.qdrant_rest -ExpectedContainerName $Script:H06Containers.qdrant -ExpectedContainerPort "6333/tcp"
    Assert-StorePortRestorable -Port $Script:PortMap.qdrant_grpc -ExpectedContainerName $Script:H06Containers.qdrant -ExpectedContainerPort "6334/tcp"
    Assert-StorePortRestorable -Port $Script:PortMap.redis -ExpectedContainerName $Script:H06Containers.redis -ExpectedContainerPort "6379/tcp"
    Assert-ApiPortFree

    $payload = [ordered]@{
        action = "Preflight"
        dry_run = [bool]$DryRunOnly
        compose_project = $Script:H06Project
        manifest = @{
            path = $ManifestInfo.path
            sha256 = $ManifestInfo.sha256
            build_id = $ManifestInfo.build_id
        }
        ports = $Script:PortMap
        h05 = Get-H05Status
    }
    $payload | ConvertTo-Json -Depth 20
}

function Invoke-DockerCompose {
    param(
        [string]$RepoRoot,
        [string[]]$Arguments
    )

    $composeFile = Join-Path $RepoRoot "docker-compose.yml"
    $dockerArgs = @("compose", "-f", $composeFile) + $Arguments

    if ($env:H06_TEST_COMPOSE_LOG) {
        @{
            WorkingDirectory = $RepoRoot
            ComposeFile = $composeFile
            Arguments = $dockerArgs
        } | ConvertTo-Json -Depth 10 | ForEach-Object {
            [System.IO.File]::WriteAllText(
                $env:H06_TEST_COMPOSE_LOG,
                $_,
                (New-Object System.Text.UTF8Encoding($false))
            )
        }
        return
    }

    Push-Location $RepoRoot
    try {
        & docker @dockerArgs
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }
    finally {
        Pop-Location
    }
}

function Start-H06ApiProcess {
    param(
        [string]$RepoRoot,
        [string]$LogPath,
        [string]$ErrorPath
    )

    $argumentList = @(
        "run",
        "uvicorn",
        "food_agent_v2.api_app:app",
        "--host",
        "127.0.0.1",
        "--port",
        [string]$Script:PortMap.api
    )

    if ($env:H06_TEST_START_PROCESS_LOG) {
        $environmentSnapshot = @{
            API_HOST = $env:API_HOST
            API_PORT = $env:API_PORT
            MYSQL_PORT = $env:MYSQL_PORT
            QDRANT_REST_PORT = $env:QDRANT_REST_PORT
            QDRANT_GRPC_PORT = $env:QDRANT_GRPC_PORT
            REDIS_PORT = $env:REDIS_PORT
            RAG_WARMUP_ON_STARTUP = $env:RAG_WARMUP_ON_STARTUP
            LLM_API_KEY = $env:LLM_API_KEY
            SILICONFLOW_API_KEY = $env:SILICONFLOW_API_KEY
        }
        @{
            FilePath = "uv"
            ArgumentList = $argumentList
            WindowStyle = "Hidden"
            WorkingDirectory = $RepoRoot
            Environment = $environmentSnapshot
        } | ConvertTo-Json -Depth 10 | ForEach-Object {
            [System.IO.File]::WriteAllText(
                $env:H06_TEST_START_PROCESS_LOG,
                $_,
                (New-Object System.Text.UTF8Encoding($false))
            )
        }
        return [pscustomobject]@{ Id = 42424 }
    }

    return Start-Process -FilePath "uv" `
        -ArgumentList $argumentList `
        -WorkingDirectory $RepoRoot `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $LogPath `
        -RedirectStandardError $ErrorPath
}

function Stop-H06ApiProcess {
    param($Process)

    if (-not $Process) {
        return
    }

    if ($env:H06_TEST_STOP_PROCESS_LOG) {
        @{
            Id = $Process.Id
        } | ConvertTo-Json -Depth 10 | ForEach-Object {
            [System.IO.File]::WriteAllText(
                $env:H06_TEST_STOP_PROCESS_LOG,
                $_,
                (New-Object System.Text.UTF8Encoding($false))
            )
        }
        return
    }

    try {
        Stop-Process -Id $Process.Id -ErrorAction Stop
    }
    catch {
        # Keep original readiness failure as the load-bearing error.
    }
}

function Invoke-UvCommand {
    param(
        [string]$RepoRoot,
        [string[]]$ArgumentList
    )

    $environmentSnapshot = @{
        MYSQL_PORT = $env:MYSQL_PORT
        QDRANT_REST_PORT = $env:QDRANT_REST_PORT
        QDRANT_GRPC_PORT = $env:QDRANT_GRPC_PORT
        REDIS_PORT = $env:REDIS_PORT
        API_PORT = $env:API_PORT
    }

    if ($env:H06_TEST_COMMAND_LOG) {
        @{
            FilePath = "uv"
            ArgumentList = $ArgumentList
            WorkingDirectory = $RepoRoot
            Environment = $environmentSnapshot
        } | ConvertTo-Json -Depth 10 | ForEach-Object {
            [System.IO.File]::WriteAllText(
                $env:H06_TEST_COMMAND_LOG,
                $_,
                (New-Object System.Text.UTF8Encoding($false))
            )
        }
        return
    }

    Push-Location $RepoRoot
    try {
        & uv @ArgumentList
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }
    finally {
        Pop-Location
    }
}

function Invoke-H06HttpJson {
    param([string]$Uri)

    if ($Script:TestHttpSequence) {
        $sequenceProperty = $Script:TestHttpSequence.PSObject.Properties[$Uri]
        if (-not $sequenceProperty) {
            Write-FailureAndExit -Message "Missing mocked HTTP sequence for $Uri."
        }
        if (-not $Script:TestHttpSequenceIndex.ContainsKey($Uri)) {
            $Script:TestHttpSequenceIndex[$Uri] = 0
        }
        $responses = $sequenceProperty.Value
        $index = [Math]::Min($Script:TestHttpSequenceIndex[$Uri], $responses.Count - 1)
        $payload = $responses[$index]
        if ($Script:TestHttpSequenceIndex[$Uri] -lt ($responses.Count - 1)) {
            $Script:TestHttpSequenceIndex[$Uri] += 1
        }
        $errorProperty = $payload.PSObject.Properties["error"]
        if ($errorProperty -and $errorProperty.Value) {
            return [pscustomobject]@{
                status_code = 0
                json = $null
                error = [string]$errorProperty.Value
            }
        }
        return [pscustomobject]@{
            status_code = [int]$payload.status_code
            json = $payload.json
            error = $null
        }
    }

    if ($Script:TestHttpState) {
        $entry = $Script:TestHttpState.PSObject.Properties[$Uri]
        if (-not $entry) {
            Write-FailureAndExit -Message "Missing mocked HTTP response for $Uri."
        }
        $payload = $entry.Value
        return [pscustomobject]@{
            status_code = [int]$payload.status_code
            json = $payload.json
            error = $null
        }
    }

    try {
        $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -ErrorAction Stop
        $json = $response.Content | ConvertFrom-Json
        return [pscustomobject]@{
            status_code = [int]$response.StatusCode
            json = $json
            error = $null
        }
    }
    catch {
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
            $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
            $content = $reader.ReadToEnd()
            $json = $null
            if ($content) {
                try {
                    $json = $content | ConvertFrom-Json
                }
                catch {
                    $json = $null
                }
            }
            return [pscustomobject]@{
                status_code = $statusCode
                json = $json
                error = $null
            }
        }
        return [pscustomobject]@{
            status_code = 0
            json = $null
            error = "connection-refused"
        }
    }
}

function Wait-ForH06Readiness {
    param([pscustomobject]$ManifestInfo)

    $attempts = Get-WaitAttempts -Kind "api"
    $lastHealth = $null
    $lastReady = $null

    for ($attempt = 0; $attempt -lt $attempts; $attempt += 1) {
        $health = Invoke-H06HttpJson -Uri "http://127.0.0.1:$($Script:PortMap.api)/health"
        $ready = Invoke-H06HttpJson -Uri "http://127.0.0.1:$($Script:PortMap.api)/ready"
        $lastHealth = $health
        $lastReady = $ready

        $healthStatus = Get-JsonPropertyValue -Object $health.json -Name "status"
        $readyStatus = Get-JsonPropertyValue -Object $ready.json -Name "status"
        $readyBuildId = [string](Get-JsonPropertyValue -Object $ready.json -Name "build_id" -Default "")

        $healthOk = $health.status_code -eq 200 -and $health.json -and $healthStatus -eq "ok"
        $readyOk = $ready.status_code -eq 200 -and $ready.json -and $readyStatus -eq "ready"
        $buildOk = $readyOk -and ($readyBuildId -eq $ManifestInfo.build_id)

        if ($healthOk -and $buildOk) {
            return [pscustomobject]@{
                health = $health.json
                ready = $ready.json
            }
        }

        if ($attempt -lt ($attempts - 1)) {
            Wait-NextAttempt
        }
    }

    $healthStatus = if ($lastHealth -and $lastHealth.json) { [string](Get-JsonPropertyValue -Object $lastHealth.json -Name "status" -Default "unknown") } elseif ($lastHealth) { [string]$lastHealth.error } else { "unknown" }
    $readyBuild = if ($lastReady -and $lastReady.json) { [string](Get-JsonPropertyValue -Object $lastReady.json -Name "build_id" -Default "") } elseif ($lastReady) { [string]$lastReady.error } else { "unknown" }
    Write-FailureAndExit -Message "H06 did not report manifest build ID before timeout. health=$healthStatus ready_build=$readyBuild"
}

function Invoke-Verify {
    param([pscustomobject]$ManifestInfo)

    $readiness = Wait-ForH06Readiness -ManifestInfo $ManifestInfo

    $containers = @{}
    foreach ($service in $Script:H06Containers.Keys) {
        $containerName = $Script:H06Containers[$service]
        $container = Get-ContainerStatus -Name $containerName
        if (-not $container -or -not $container.running -or $container.health -ne "healthy") {
            Write-FailureAndExit -Message "H06 container unhealthy: $containerName"
        }
        $containers[$containerName] = @{
            running = $container.running
            health = $container.health
        }
    }

    [ordered]@{
        action = "Verify"
        build_id = $ManifestInfo.build_id
        health = $readiness.health
        ready = $readiness.ready
        containers = $containers
    } | ConvertTo-Json -Depth 20
}

Initialize-TestState
try {
    $manifestInfo = Resolve-ManifestInfo -ManifestPath $Manifest
    Import-Environment -RepoRoot $manifestInfo.repo_root
    Set-H06Environment

    switch ($Action) {
        "Preflight" {
            Invoke-Preflight -ManifestInfo $manifestInfo -DryRunOnly:$DryRun
        }
        "StartStores" {
            [void](Invoke-Preflight -ManifestInfo $manifestInfo)
            Invoke-DockerCompose -RepoRoot $manifestInfo.repo_root -Arguments @("-p", $Script:H06Project, "up", "-d", "mysql", "qdrant", "redis")
            $containers = Wait-ForHealthyContainers
            [ordered]@{
                action = "StartStores"
                compose_project = $Script:H06Project
                containers = $containers
            } | ConvertTo-Json -Depth 10
        }
        "Initialize" {
            Invoke-UvCommand -RepoRoot $manifestInfo.repo_root -ArgumentList @(
                "run",
                "food-agent-v2",
                "data-initialize",
                "--manifest",
                $manifestInfo.path,
                "--confirm-empty-v2"
            )
        }
        "StartApi" {
            [void](Invoke-Preflight -ManifestInfo $manifestInfo)
            $stagingDir = Join-Path $manifestInfo.repo_root ".staging"
            New-Item -ItemType Directory -Path $stagingDir -Force | Out-Null
            $logPath = Join-Path $stagingDir "h06-api.log"
            $errorPath = Join-Path $stagingDir "h06-api.err"
            $process = Start-H06ApiProcess -RepoRoot $manifestInfo.repo_root -LogPath $logPath -ErrorPath $errorPath
            try {
                $readiness = Wait-ForH06Readiness -ManifestInfo $manifestInfo
            }
            catch {
                Stop-H06ApiProcess -Process $process
                throw
            }
            [ordered]@{
                action = "StartApi"
                build_id = $manifestInfo.build_id
                api_base = "http://127.0.0.1:$($Script:PortMap.api)"
                pid = $process.Id
                log_path = $logPath
                err_path = $errorPath
                health = $readiness.health
                ready = $readiness.ready
            } | ConvertTo-Json -Depth 10
        }
        "Verify" {
            Invoke-Verify -ManifestInfo $manifestInfo
        }
    }
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
