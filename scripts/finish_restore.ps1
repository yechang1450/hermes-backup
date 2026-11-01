<#
finish_restore.ps1 — Hermes 恢复的"桌面端收尾器"

作用：等 Hermes 桌面端完全退出 → 把桌面数据 (desktop-overlay) 覆盖到 %APPDATA%\Hermes
      → 重启 Hermes 网关（让配置/凭据生效）→ 重新拉起桌面端。

由恢复流程以"独立进程"方式启动（WMI Create，不依赖对话进程，网关重启也不会带走它）：
  pwsh -NoProfile -Command "(Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='pwsh -NoProfile -File \"<REPO>\tools\finish_restore.ps1\" -Repo \"<REPO>\"'}).ReturnValue"

测试模式：-WaitMinutes 0 -DstRoot <临时目录> -SkipGateway -NoRelaunch
#>
param(
  [Parameter(Mandatory=$true)][string]$Repo,
  [int]$WaitMinutes = 20,
  [string]$DstRoot = '',
  [string]$ProcessName = 'Hermes',
  [switch]$SkipGateway,
  [switch]$NoRelaunch
)
$ErrorActionPreference = 'Continue'
$logDir = Join-Path $Repo 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$log = Join-Path $logDir 'finish-restore.log'
function L([string]$m) {
  "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Out-File -FilePath $log -Append -Encoding utf8
  Write-Host $m
}

L "== finish_restore 启动 (Repo=$Repo) =="
if ($WaitMinutes -gt 0) {
  L "等待 $ProcessName.exe 退出（最多 $WaitMinutes 分钟）..."
  $deadline = (Get-Date).AddMinutes($WaitMinutes)
  while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Seconds 3
  }
  if (Get-Process -Name $ProcessName -ErrorAction SilentlyContinue) {
    L "超时：$ProcessName 仍在运行。放弃本次收尾（稍后重新启动本脚本即可）。"
    exit 2
  }
  L "$ProcessName 已完全退出。"
} else {
  L "WaitMinutes=0：跳过等待（测试模式）。"
}

$src = Join-Path $Repo 'desktop-overlay'
if (-not (Test-Path $src)) { L "找不到 $src，退出。"; exit 3 }
if (-not $DstRoot) { $DstRoot = Join-Path $env:APPDATA 'Hermes' }
L "覆盖桌面数据: $src -> $DstRoot"
# 2026-11-01：覆盖前先给桌面层留一份备份——`/E` 是**合并式**覆盖，不做备份的话
# 同机恢复会把置顶/群聊/leveldb 直接回滚且无法撤回（leveldb 新旧 SST 混在一起还可能不一致）。
$bak = Join-Path (Split-Path $DstRoot -Parent) ("Hermes.pre-restore-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path $DstRoot) {
  try {
    robocopy $DstRoot $bak /E /NFL /NDL /NP /R:0 /W:0 | Out-Null
    L "桌面层已备份：$bak"
  } catch {
    L "桌面层备份失败（继续覆盖，请自行留意）：$_"
  }
}
robocopy $src $DstRoot /E /NFL /NDL /NP /R:1 /W:1 | Out-Null
$rc = $LASTEXITCODE
L "robocopy 结束 rc=$rc"
if ($rc -ge 8) { L "robocopy 失败（rc>=8）"; exit 3 }

if (-not $SkipGateway) {
  L "重启 Hermes 网关（hermes gateway restart）..."
  $hermes = Join-Path $env:LOCALAPPDATA 'hermes\bin\hermes.cmd'
  if (-not (Test-Path $hermes)) { $hermes = Join-Path $env:LOCALAPPDATA 'hermes\bin\hermes.exe' }
  if (Test-Path $hermes) {
    & $hermes gateway restart 2>&1 | ForEach-Object { L "  [gateway] $_" }
    L "gateway restart rc=$LASTEXITCODE"
  } else {
    L "未找到 $hermes，跳过网关重启（请在下次打开 Hermes 后让它检查网关）"
  }
}

if (-not $NoRelaunch) {
  $exe = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\apps\desktop\release\win-unpacked\Hermes.exe'
  if (Test-Path $exe) {
    Start-Process $exe
    L "已重新拉起 Hermes 桌面端"
  } else {
    L "未找到 Hermes.exe（$exe），请手动打开桌面端"
  }
}

$marker = Join-Path $Repo 'restore-out\FINISH-DONE.txt'
New-Item -ItemType Directory -Path (Split-Path $marker) -Force | Out-Null
"finished $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') robocopy_rc=$rc" | Out-File -FilePath $marker -Encoding utf8
L "== 收尾完成 =="
