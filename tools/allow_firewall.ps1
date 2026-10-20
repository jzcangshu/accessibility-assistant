# 放行防火墙：让手机能连上电脑端。
#
# 什么时候需要它：手机打不开页面，而且同一个 WiFi 下别的局域网互传 App 也一起失灵。
# 根因通常是 Windows 防火墙按「网络类型」（域 / 专用 / 公用）分开管，而程序第一次
# 弹「是否允许访问」时只给当时那个网络类型建了规则——换个网络（手机热点 ↔ WiFi）
# 规则就失配了。这个脚本把规则建成「所有网络类型都生效」，以后换网络都不用再跑。
#
# 会自己弹 UAC 提权。只加两条入站放行规则，不改任何其它设置。
#
# 提权后是新开一个窗口跑，那个窗口做完就关——所以结果同时写一份到临时文件，
# 由没提权的原窗口读回来显示，用户才看得到（原窗口末尾有 pause 不会闪退）。
param([string]$LogPath = '')

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$script:lines = @()

function Say($msg, $color = 'Gray') {
    Write-Host $msg -ForegroundColor $color
    $script:lines += $msg
}

function Flush-Log {
    if ($LogPath) {
        try { $script:lines -join "`r`n" | Out-File -FilePath $LogPath -Encoding UTF8 } catch { }
    }
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    $log = Join-Path $env:TEMP ('assistant-fw-' + [Guid]::NewGuid().ToString('N').Substring(0, 8) + '.log')
    Write-Host "需要管理员权限，正在请求提权（请在弹窗里点「是」）..." -ForegroundColor Yellow
    try {
        Start-Process powershell -Verb RunAs -Wait -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', "`"$PSCommandPath`"", '-LogPath', "`"$log`"")
    } catch {
        Write-Host "提权被取消，没有做任何改动。" -ForegroundColor Red
        exit 1
    }
    if (Test-Path $log) {
        Get-Content -Encoding UTF8 $log | ForEach-Object { Write-Host $_ }
        Remove-Item $log -ErrorAction SilentlyContinue
    } else {
        Write-Host "提权后的窗口没有留下结果，请手动确认规则是否已添加。" -ForegroundColor Yellow
    }
    exit 0
}

# ---- 以下在管理员窗口里执行 ----

# 端口从 config.json 读，读不到就用默认值
$port = 8765
foreach ($rel in @('config.json', 'dist\config.json')) {
    $p = Join-Path $root $rel
    if (Test-Path $p) {
        try {
            $v = (Get-Content -Raw -Encoding UTF8 $p | ConvertFrom-Json).server.port
            if ($v) { $port = [int]$v; break }
        } catch { }
    }
}

Say ""
Say "无障碍助手：放行防火墙（端口 $port + mDNS 5353）" Cyan
Say ("-" * 46)
Say ""

$rules = @(
    @{ Name = "accessibility-assistant-$port"; OldName = "qa-assist-$port";    Proto = 'TCP'; Port = $port },
    @{ Name = 'accessibility-assistant-mdns';  OldName = 'qa-assist-udp5353'; Proto = 'UDP'; Port = 5353 }
)
$ok = 0
foreach ($r in $rules) {
    # 先删同名旧规则（含旧版命名），保证重复执行不会堆出一堆重复项
    netsh advfirewall firewall delete rule name="$($r.Name)" dir=in 2>&1 | Out-Null
    netsh advfirewall firewall delete rule name="$($r.OldName)" dir=in 2>&1 | Out-Null
    $out = netsh advfirewall firewall add rule name="$($r.Name)" dir=in action=allow `
        protocol=$($r.Proto) localport=$($r.Port) profile=any 2>&1
    if ($LASTEXITCODE -eq 0) {
        Say ("  [ok]   " + $r.Proto + " " + $r.Port) Green
        $ok++
    } else {
        Say ("  [失败] " + $r.Proto + " " + $r.Port + " -> " + $out) Red
    }
}

Say ""
if ($ok -eq $rules.Count) {
    Say "完成。规则对「域 / 专用 / 公用」三种网络都生效，换网络不用再跑一遍。" Cyan
    Say "现在回到手机，重新打开控制台上那个地址试试。"
} else {
    Say "有规则没加上，请把上面的信息发给开发者。" Yellow
}
Say ""
Say "（手机端 App 的「自动搜索」还需要 mDNS，上面已经一并放行了。）" DarkGray
Flush-Log
exit 0
