$ErrorActionPreference = "Stop"

$BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (Test-Path $BundledPython) {
    $Python = $BundledPython
} else {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        throw "Python interpreter not found."
    }
    $Python = $PythonCommand.Source
}

& $Python scripts\validation\validate_final_e2e_mvp.py
& $Python scripts\project_cleanup.py --report-only
