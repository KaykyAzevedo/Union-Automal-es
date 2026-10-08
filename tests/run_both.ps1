# Roda a suíte inteira em SQLite e em Postgres 16 local (porta 55432), SEM Docker/WSL/administrador.
# Uso (na raiz do repo):  powershell -ExecutionPolicy Bypass -File tests\run_both.ps1 [args do pytest]
#
# Postgres: binários portáteis do EDB em %LOCALAPPDATA%\union-pg (fora do repo; caminho sem acentos,
# que o initdb exige). Na 1ª vez o script baixa (~340 MB), extrai e cria o cluster; depois só liga.
# Outra pasta: $env:UNION_PG_HOME. Outro Postgres já rodando: $env:TEST_PG_URL (precisa ser local —
# o conftest recusa host remoto porque os testes apagam tudo).
# Parar o servidor:  & "$env:LOCALAPPDATA\union-pg\pgsql\bin\pg_ctl.exe" -D "$env:LOCALAPPDATA\union-pg\data" stop
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$pgUrl = $env:TEST_PG_URL
$pgPort = 55432
$pgZip = "https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64-binaries.zip"

if (-not $pgUrl) {
    $pgHome = if ($env:UNION_PG_HOME) { $env:UNION_PG_HOME } else { Join-Path $env:LOCALAPPDATA "union-pg" }
    $bin = Join-Path $pgHome "pgsql\bin"
    $data = Join-Path $pgHome "data"
    $pgUrl = "postgresql://postgres:test@localhost:$pgPort/union_test"   # auth trust: a senha é ignorada

    if (-not (Test-Path (Join-Path $bin "pg_ctl.exe"))) {
        Write-Host "Baixando PostgreSQL 16 portátil para $pgHome (só na 1ª vez)..." -ForegroundColor Cyan
        New-Item -ItemType Directory -Force $pgHome | Out-Null
        $zip = Join-Path $pgHome "pg16.zip"
        curl.exe -fL --retry 3 -sS -o $zip $pgZip
        if ($LASTEXITCODE -ne 0) { throw "download do PostgreSQL falhou ($pgZip)" }
        tar.exe -xf $zip -C $pgHome --exclude "pgsql/pgAdmin 4" --exclude "pgsql/doc" --exclude "pgsql/StackBuilder" --exclude "pgsql/include" --exclude "pgsql/symbols"
        if ($LASTEXITCODE -ne 0) { throw "extração do PostgreSQL falhou" }
        Remove-Item $zip
    }
    if (-not (Test-Path (Join-Path $data "PG_VERSION"))) {
        & (Join-Path $bin "initdb.exe") -D $data -U postgres -A trust -E UTF8 --locale=C | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "initdb falhou" }
    }
    & (Join-Path $bin "pg_isready.exe") -h localhost -p $pgPort | Out-Null
    if ($LASTEXITCODE -ne 0) {
        # Start-Process: o servidor não herda o console (pg_ctl chamado direto prende o terminal)
        Start-Process -WindowStyle Hidden -FilePath (Join-Path $bin "pg_ctl.exe") -ArgumentList @(
            "-D", "`"$data`"", "-l", "`"$(Join-Path $pgHome 'pg.log')`"",
            "-o", "`"-p $pgPort -c listen_addresses=localhost`"", "start")
        for ($i = 0; $i -lt 60; $i++) {
            & (Join-Path $bin "pg_isready.exe") -h localhost -p $pgPort | Out-Null
            if ($LASTEXITCODE -eq 0) { break }
            Start-Sleep -Seconds 1
        }
        if ($LASTEXITCODE -ne 0) { throw "Postgres não subiu na porta $pgPort (veja $pgHome\pg.log)" }
    }
    $exists = & (Join-Path $bin "psql.exe") -h localhost -p $pgPort -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='union_test'"
    if (-not $exists) { & (Join-Path $bin "createdb.exe") -h localhost -p $pgPort -U postgres union_test }
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
