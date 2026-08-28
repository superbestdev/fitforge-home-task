<#
.SYNOPSIS
    FitForge task runner for Windows.

.DESCRIPTION
    Same targets as the Makefile, for hosts without `make` installed.
    Everything runs in Docker, so nothing needs installing beyond Docker itself.

.EXAMPLE
    .\run.ps1 up
    .\run.ps1 seed
    .\run.ps1 demo
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Task = 'help',

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$Compose = @('docker', 'compose')
$RunApi  = @('docker', 'compose', 'run', '--rm', '--no-deps', 'api')

function Invoke-Cmd {
    param([string[]]$Command)
    Write-Host "> $($Command -join ' ')" -ForegroundColor DarkGray
    & $Command[0] $Command[1..($Command.Length - 1)]
    if ($LASTEXITCODE -ne 0) { throw "command failed with exit code $LASTEXITCODE" }
}

function Ensure-Env {
    if (-not (Test-Path '.env')) {
        Copy-Item '.env.example' '.env'
        Write-Host 'created .env from .env.example' -ForegroundColor Green
    }
}

function Wait-ForApi {
    Write-Host 'waiting for the api to become healthy...' -ForegroundColor DarkGray
    for ($i = 0; $i -lt 90; $i++) {
        try {
            Invoke-RestMethod -Uri 'http://localhost:8000/health' -TimeoutSec 3 | Out-Null
            Write-Host 'api is healthy' -ForegroundColor Green
            return
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    throw 'the api did not become healthy in time — check: .\run.ps1 logs'
}

switch ($Task.ToLower()) {

    'help' {
        Write-Host ''
        Write-Host 'FitForge task runner' -ForegroundColor Cyan
        Write-Host ''
        @(
            @('up',       'Start the whole stack'),
            @('models',   'Pull the LLM + embedding models into Ollama (several GB)'),
            @('seed',     'Generate the catalog, customers, orders and manual PDFs'),
            @('ingest',   'Ingest manuals: classify -> OCR -> chunk -> embed -> index'),
            @('reingest', 'Rebuild the whole index from scratch'),
            @('demo',     'Scripted multi-issue session, end to end'),
            @('sample',   'Generate a realistic sample manual PDF for upload testing'),
            @('demo-reset','Reset the demo (unback the bike, clear its sessions) for a re-take'),
            @('test',     'Run the test suite'),
            @('eval',     'Replay the golden sessions'),
            @('e2e',      'Browser tests: drives both UIs in real Chromium'),
            @('metrics',  'Print the production signals'),
            @('coverage', 'Show what documentation the agent actually has'),
            @('health',   'Deep health check across every dependency'),
            @('logs',     'Tail the api logs'),
            @('ps',       'Show container status'),
            @('psql',     'Open a database shell'),
            @('obs',      'Start self-hosted Langfuse tracing'),
            @('down',     'Stop everything'),
            @('clean',    'Stop everything and delete all data (destructive)')
        ) | ForEach-Object {
            Write-Host ('  {0,-10} {1}' -f $_[0], $_[1])
        }
        Write-Host ''
        Write-Host 'First run:  .\run.ps1 up; .\run.ps1 models; .\run.ps1 seed; .\run.ps1 ingest' -ForegroundColor Yellow
        Write-Host ''
    }

    'up' {
        Ensure-Env
        Invoke-Cmd ($Compose + @('up', '-d', '--build'))
        Wait-ForApi
        Write-Host ''
        Write-Host '  chat      http://localhost:5173' -ForegroundColor Cyan
        Write-Host '  console   http://localhost:5173/console' -ForegroundColor Cyan
        Write-Host '  docs      http://localhost:5173/docs' -ForegroundColor Cyan
        Write-Host '  api docs  http://localhost:8000/docs' -ForegroundColor Cyan
        Write-Host ''
        Write-Host 'Next: .\run.ps1 models; .\run.ps1 seed; .\run.ps1 ingest' -ForegroundColor Yellow
    }

    'models' {
        Ensure-Env
        Invoke-Cmd ($Compose + @('up', '-d', 'ollama'))
        Start-Sleep -Seconds 5
        Invoke-Cmd ($Compose + @('run', '--rm', 'ollama-pull'))
    }

    'seed' {
        Invoke-Cmd ($RunApi + @('python', '-m', 'seed.generate_catalog'))
        Invoke-Cmd ($RunApi + @('python', '-m', 'seed.generate_manuals'))
    }

    'ingest'   { Invoke-Cmd ($RunApi + @('python', '-m', 'services.ingest.pipeline')) }
    'reingest' { Invoke-Cmd ($RunApi + @('python', '-m', 'services.ingest.pipeline', '--reingest')) }
    'demo'     { Invoke-Cmd ($RunApi + @('python', '-m', 'evals.demo_multi_issue')) }
    'sample'   {
        Invoke-Cmd ($RunApi + @('python', '-m', 'seed.generate_sample_manual'))
        Write-Host ''
        Write-Host 'Drop it into the agent console -> Manuals:' -ForegroundColor Cyan
        Write-Host '  data\sample\FitForge_Sample_Service_Manual.pdf' -ForegroundColor Cyan
    }
    'demo-reset' {
        Invoke-Cmd ($RunApi + @('python', '-m', 'seed.demo_reset'))
        Write-Host ''
        Write-Host 'Ready for another take. Upload this again when you get to Act 2:' -ForegroundColor Cyan
        Write-Host '  data\sample\FitForge_Sample_Bike_Manual.pdf' -ForegroundColor Cyan
    }
    'test'     { Invoke-Cmd ($RunApi + @('python', '-m', 'pytest', 'tests/', '-q')) }
    'eval'     { Invoke-Cmd ($RunApi + @('python', '-m', 'evals.run_golden')) }
    'e2e' {
        # Playwright drives a real browser on the host, so it runs outside Docker.
        Push-Location 'web/e2e'
        try {
            if (-not (Test-Path 'node_modules')) {
                Invoke-Cmd @('npm', 'install')
                Invoke-Cmd @('npx', 'playwright', 'install', 'chromium')
            }
            Invoke-Cmd @('npm', 'run', 'all')
        } finally { Pop-Location }
    }

    'metrics'  { Invoke-RestMethod 'http://localhost:8000/api/metrics'  | ConvertTo-Json -Depth 6 }
    'health'   { Invoke-RestMethod 'http://localhost:8000/health/deep'  | ConvertTo-Json -Depth 4 }
    'coverage' { Invoke-RestMethod 'http://localhost:8000/api/coverage' | ConvertTo-Json -Depth 4 }

    'logs' { Invoke-Cmd ($Compose + @('logs', '-f', 'api')) }
    'ps'   { Invoke-Cmd ($Compose + @('ps')) }
    'psql' { Invoke-Cmd ($Compose + @('exec', 'postgres', 'psql', '-U', 'fitforge', '-d', 'fitforge')) }

    'obs' {
        Invoke-Cmd ($Compose + @('--profile', 'obs', 'up', '-d'))
        Write-Host 'Langfuse: http://localhost:3000 — create a project, put the keys' -ForegroundColor Cyan
        Write-Host 'in .env, then set LANGFUSE_ENABLED=true' -ForegroundColor Cyan
    }

    'down' { Invoke-Cmd ($Compose + @('down')) }

    'clean' {
        Write-Host 'This deletes the database volume and every generated manual.' -ForegroundColor Yellow
        $answer = Read-Host 'Type "yes" to continue'
        if ($answer -ne 'yes') { Write-Host 'cancelled'; break }
        Invoke-Cmd ($Compose + @('down', '-v'))
        Remove-Item 'data/manuals/*.pdf', 'data/ocr_cache/*.pdf' -ErrorAction SilentlyContinue
    }

    default {
        Write-Host "unknown task: $Task" -ForegroundColor Red
        Write-Host 'run .\run.ps1 help for the list'
        exit 1
    }
}
