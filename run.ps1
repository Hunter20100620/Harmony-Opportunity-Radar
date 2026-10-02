# -*- coding: utf-8 -*-
<#
纯血鸿蒙机会雷达 · 快速运行脚本

用法:
    .\run.ps1                 交互菜单
    .\run.ps1 gui             启动 Web 情报看板
    .\run.ps1 full            全量流水线 capture -> probe -> audit
    .\run.ps1 capture         仅捕获 Apple 榜单
    .\run.ps1 probe           仅探测鸿蒙供给
    .\run.ps1 audit           仅四维审计
    .\run.ps1 check           环境自检
    .\run.ps1 install         安装依赖

也可双击根目录 run.bat（自动绕过执行策略）。
#>
param(
    [Parameter(Position = 0)][string]$Action = "",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

$script:Py = ""
$script:RadarExit = 0

function Get-Python {
    foreach ($cand in @("python", "py")) {
        if (Get-Command $cand -ErrorAction SilentlyContinue) { return $cand }
    }
    throw "未找到 Python 解释器，请先安装 Python 3.11+ 并加入 PATH。"
}

# 执行一步流水线；python 输出直达控制台，退出码存于 $script:RadarExit
function Invoke-Radar {
    param([string]$Label, [string[]]$RadarArgs)
    Write-Host ""
    Write-Host "==> $Label  (python -m radar $($RadarArgs -join ' '))" -ForegroundColor Cyan
    & $script:Py -m radar @RadarArgs
    $script:RadarExit = $LASTEXITCODE
    if ($script:RadarExit -ne 0) {
        Write-Host "步骤失败: $Label (退出码 $script:RadarExit)" -ForegroundColor Red
    }
}

function Invoke-Check {
    Write-Host "环境自检" -ForegroundColor Cyan
    Write-Host "  项目目录 : $Root"

    $ver = (& $script:Py -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null) -join ""
    Write-Host "  Python   : $ver"
    if ([version]$ver -lt [version]"3.11") {
        Write-Host "  [x] 需要 Python 3.11+（依赖 tomllib）" -ForegroundColor Red
    } else {
        Write-Host "  [v] Python 版本满足" -ForegroundColor Green
    }

    $mods = (& $script:Py -c "import importlib.util as u; print(' '.join(m if u.find_spec(m) else '-' for m in ('httpx','streamlit')))" 2>$null) -join ""
    foreach ($m in $mods.Split(" ")) {
        if ($m -eq "-") {
            Write-Host "  [x] 缺少依赖，请运行: .\run.ps1 install" -ForegroundColor Red
        } elseif ($m) {
            Write-Host "  [v] 依赖 $m 已安装" -ForegroundColor Green
        }
    }

    if (Test-Path (Join-Path $Root "config.toml")) {
        Write-Host "  [v] config.toml 存在" -ForegroundColor Green
    } else {
        Write-Host "  [i] config.toml 缺失，将使用内置默认值" -ForegroundColor Yellow
    }

    foreach ($f in @("raw_feed.json", "benchmarks.json", "probe.json", "opportunities.json")) {
        $p = Join-Path $Root "data\$f"
        if (Test-Path $p) {
            $kb = [math]::Round((Get-Item $p).Length / 1KB, 1)
            Write-Host "  [v] data/$f  ($kb KB)" -ForegroundColor Green
        } else {
            Write-Host "  [i] data/$f 未生成" -ForegroundColor Yellow
        }
    }
    Write-Host ""
}

function Show-Menu {
    Write-Host ""
    Write-Host "纯血鸿蒙机会雷达 · 快速运行" -ForegroundColor Cyan
    Write-Host "  1) 启动 Web 情报看板 (gui)"
    Write-Host "  2) 全量流水线 (capture -> probe -> audit)"
    Write-Host "  3) 仅捕获 (capture)"
    Write-Host "  4) 仅探测 (probe)"
    Write-Host "  5) 仅审计 (audit)"
    Write-Host "  6) 环境自检 (check)"
    Write-Host "  7) 安装依赖 (install)"
    Write-Host "  0) 退出"
    $choice = Read-Host "请输入序号"
    switch ($choice) {
        "1" { return "gui" }
        "2" { return "full" }
        "3" { return "capture" }
        "4" { return "probe" }
        "5" { return "audit" }
        "6" { return "check" }
        "7" { return "install" }
        default { return "" }
    }
}

$script:Py = Get-Python

if ([string]::IsNullOrWhiteSpace($Action)) {
    $Action = Show-Menu
}
if ([string]::IsNullOrWhiteSpace($Action)) {
    Write-Host "已退出。"
    exit 0
}

switch ($Action.ToLower()) {
    "gui" {
        Write-Host "启动 Streamlit 情报看板 ..." -ForegroundColor Cyan
        & $script:Py -m streamlit run app.py @Rest
        exit $LASTEXITCODE
    }
    "full" {
        # 全量扫描共享同一批次目录，便于统一回溯
        $batchId = Get-Date -Format "yyyyMMdd_HHmmss"
        foreach ($step in @(@("capture", "榜单捕获"), @("probe", "供给探测"), @("audit", "四维审计"))) {
            Invoke-Radar -Label $step[1] -RadarArgs @($step[0], "--batch-id", $batchId)
            if ($script:RadarExit -ne 0) { exit $script:RadarExit }
        }
        Write-Host ""
        Write-Host "全量流水线完成。批次: data/runs/$batchId/  看板: .\run.ps1 gui" -ForegroundColor Green
        exit 0
    }
    "capture" {
        Invoke-Radar -Label "榜单捕获" -RadarArgs @("capture")
        exit $script:RadarExit
    }
    "probe" {
        Invoke-Radar -Label "供给探测" -RadarArgs @("probe")
        exit $script:RadarExit
    }
    "audit" {
        Invoke-Radar -Label "四维审计" -RadarArgs @("audit")
        exit $script:RadarExit
    }
    "check" { Invoke-Check; exit 0 }
    "install" {
        Write-Host "安装依赖 (requirements.txt) ..." -ForegroundColor Cyan
        & $script:Py -m pip install -r requirements.txt
        exit $LASTEXITCODE
    }
    default {
        Write-Host "未知指令: $Action" -ForegroundColor Red
        Write-Host "可用: gui | full | capture | probe | audit | check | install"
        exit 1
    }
}