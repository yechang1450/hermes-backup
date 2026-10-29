<#
start-finish-restore.ps1 — 以「独立进程」方式启动 finish_restore.ps1（恢复流程第 5 步）

为什么要有这个壳：
  直接在内联 pwsh 里拼 Win32_Process 的 CommandLine 时，bash → pwsh → 字符串 三层转义只要错一个反斜杠就
  ParserError（2026-10-29 实测）。把拼接放进脚本文件，手册里就只剩一条永远不会被转义坑到的命令：

    pwsh -NoProfile -File "<REPO>\tools\start-finish-restore.ps1" -Repo "<REPO>"

用法：
  pwsh -NoProfile -File start-finish-restore.ps1 -Repo C:\Users\你\hermes-backup
  pwsh -NoProfile -File start-finish-restore.ps1 -Repo ... -DryRun     # 只打印将执行的命令行，不启动
#>
param(
  [string]$Repo = '',
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if (-not $Repo) {
  $Repo = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
}
$Repo = [System.IO.Path]::GetFullPath($Repo)
$fin = Join-Path $Repo 'tools\finish_restore.ps1'

if (-not (Test-Path -LiteralPath $fin)) {
  [Console]::Error.WriteLine("找不到收尾器：$fin")
  exit 2
}

$exe = (Get-Command pwsh -ErrorAction Stop).Source
$cmd = '"' + $exe + '" -NoProfile -File "' + $fin + '" -Repo "' + $Repo + '"'

if ($DryRun) {
  Write-Output "DRYRUN command=$cmd"
  exit 0
}

$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
Write-Output ("launched return=" + $r.ReturnValue + " pid=" + $r.ProcessId)
if ($r.ReturnValue -ne 0) { exit 1 }
