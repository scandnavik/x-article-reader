param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Args
)

$defaultPython = "C:\Users\User\AppData\Local\Python\pythoncore-3.14-64\python.exe"

if ($env:XARTICLE_READER_PYTHON) {
  $python = $env:XARTICLE_READER_PYTHON
} elseif (Test-Path $defaultPython) {
  $python = $defaultPython
} else {
  $python = (Get-Command python -ErrorAction Stop).Source
}

$scriptPath = Join-Path $PSScriptRoot "run_harness.py"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
& $python -X utf8 $scriptPath @Args
exit $LASTEXITCODE
