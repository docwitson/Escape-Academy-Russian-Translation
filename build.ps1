$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$EntryPoint = Join-Path $ProjectRoot "src\installer_app.py"
$DistDir = Join-Path $ProjectRoot "dist"
$WorkDir = Join-Path $ProjectRoot "build"
$SpecDir = Join-Path $ProjectRoot "packaging"
$VersionFile = Join-Path $ProjectRoot "installer_version.txt"

New-Item -ItemType Directory -Force -Path $DistDir, $WorkDir, $SpecDir | Out-Null

$Arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--noupx",
    "--name", "EscapeAcademyRussianInstaller",
    "--distpath", $DistDir,
    "--workpath", $WorkDir,
    "--specpath", $SpecDir,
    "--paths", $ProjectRoot,
    "--version-file", $VersionFile,
    "--collect-data", "UnityPy",
    "--hidden-import", "lz4.block",
    "--hidden-import", "brotli",
    "--hidden-import", "fsspec.implementations.local",
    "--exclude-module", "pandas",
    "--exclude-module", "scipy",
    "--exclude-module", "matplotlib",
    "--exclude-module", "pygame",
    "--exclude-module", "numba",
    "--exclude-module", "llvmlite",
    "--exclude-module", "sqlalchemy",
    "--exclude-module", "openpyxl",
    "--exclude-module", "lxml",
    "--exclude-module", "numpy",
    "--add-data", "$(Join-Path $ProjectRoot 'manifests');manifests",
    "--add-data", "$(Join-Path $ProjectRoot 'ollama_pipeline\output');ollama_pipeline/output",
    $EntryPoint
)

& python @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$Executable = Join-Path $DistDir "EscapeAcademyRussianInstaller.exe"
$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Executable).Hash.ToLowerInvariant()
"$Hash  EscapeAcademyRussianInstaller.exe" | Set-Content -LiteralPath (Join-Path $DistDir "SHA256SUMS.txt") -Encoding ascii

Get-Item -LiteralPath $Executable | Select-Object Name, Length, LastWriteTime
Write-Host "SHA256: $Hash"
