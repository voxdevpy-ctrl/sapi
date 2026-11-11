# Instalador do SAPI (Windows). Confere os pré-requisitos, cria o ambiente Python e instala as dependências.
# Não precisa de administrador. Não baixa nada além das dependências listadas em requirements.txt
# e, se você concordar, os modelos locais de transcrição.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$faltando = @()

function Confere($nome, $comando, $ajuda, [switch]$opcional) {
    $c = Get-Command $comando -ErrorAction SilentlyContinue
    if ($c) { Write-Host "  [ok] ${nome}" -ForegroundColor Green; return $c }
    if ($opcional) { Write-Host "  [--] ${nome} (opcional): ${ajuda}" -ForegroundColor Yellow }
    else { Write-Host "  [falta] ${nome}: ${ajuda}" -ForegroundColor Red; $script:faltando += $nome }
    return $null
}

Write-Host "SAPI - Sistema Autônomo de Produção para a Internet" -ForegroundColor Cyan
Write-Host "Conferindo pré-requisitos..."
$py = Confere "Python 3.12 ou mais novo" "python" "instale em https://www.python.org/downloads/ (marque 'Add to PATH')"
if ($py) {
    $v = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
    if ([version]$v -lt [version]"3.12") { Write-Host "  [falta] Python $v é antigo: precisa de 3.12 ou mais novo" -ForegroundColor Red; $faltando += "Python" }
}
Confere "FFmpeg" "ffmpeg" "rode: winget install Gyan.FFmpeg" | Out-Null
Confere "Node.js (o yt-dlp usa para o YouTube)" "node" "rode: winget install OpenJS.NodeJS.LTS" | Out-Null
Confere "Claude Code (agentes de IA)" "claude" "instale em https://claude.com/product/claude-code e faça login com a SUA conta" -opcional | Out-Null

if ($faltando.Count) {
    Write-Host "`nInstale o que falta e rode o instalar.bat de novo." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "`nCriando o ambiente Python (.venv)..."
    & python -m venv .venv
}
Write-Host "Instalando dependências (pode levar alguns minutos)..."
& .venv\Scripts\python.exe -m pip install --disable-pip-version-check -q --upgrade pip
& .venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt
if ($LASTEXITCODE) { Write-Host "Falha ao instalar as dependências." -ForegroundColor Red; exit 1 }

$r = Read-Host "`nBaixar agora os modelos locais de transcrição (~500 MB, uma vez só)? [S/n]"
if ($r -notmatch "^[nN]") { & .venv\Scripts\python.exe -m cortes modelos }

Write-Host "`nPronto! Abra o SAPI com o iniciar.bat." -ForegroundColor Green
Write-Host "Seus dados ficam na pasta PRODUCAO (ou em SAPI_DADOS, se você definir no arquivo .env)."
