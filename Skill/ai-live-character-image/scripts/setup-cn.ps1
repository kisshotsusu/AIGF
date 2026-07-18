$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent $PSScriptRoot
py -m pip install -r (Join-Path $SkillRoot "requirements.txt") -i https://pypi.tuna.tsinghua.edu.cn/simple
Write-Host "Dependencies installed."

