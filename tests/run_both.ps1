# Roda a suíte inteira em SQLite e em Postgres 16 local (porta 55432).
# Uso (na raiz do repo):  powershell -File tests\run_both.ps1 [args do pytest]
# Postgres: se $env:TEST_PG_URL estiver definido, usa esse (ex.: binários portáteis já rodando);
# senão sobe/usa o container Docker "union-pg". O conftest só aceita host local (os testes apagam tudo).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$pgUrl = $env:TEST_PG_URL

if (-not $pgUrl) {
    $pgUrl = "postgresql://postgres:test@localhost:55432/union_test"
    if (-not (docker ps -a --filter name=^union-pg$ --format "{{.Names}}")) {
        docker run -d --name union-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16 | Out-Null
    } elseif (-not (docker ps --filter name=^union-pg$ --format "{{.Names}}")) {
        docker start union-pg | Out-Null
    }
    for ($i = 0; $i -lt 60; $i++) {
        docker exec union-pg pg_isready -U postgres 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 1
    }
    if (-not (docker exec union-pg psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='union_test'")) {
        docker exec union-pg createdb -U postgres union_test
    }
}

Push-Location $root
try {
    Write-Host "=== SQLite ===" -ForegroundColor Cyan
    Remove-Item Env:TEST_DATABASE_URL -ErrorAction SilentlyContinue
    & $py -m pytest -q -p no:warnings @args
    $sqlite = $LASTEXITCODE

    Write-Host "=== Postgres 16 ($pgUrl) ===" -ForegroundColor Cyan
    $env:TEST_DATABASE_URL = $pgUrl
    & $py -m pytest -q -p no:warnings @args
    $pg = $LASTEXITCODE
    Remove-Item Env:TEST_DATABASE_URL
} finally { Pop-Location }

Write-Host "SQLite exit=$sqlite  Postgres exit=$pg"
exit [int]($sqlite -ne 0 -or $pg -ne 0)
