"""防火墙自检：手机连不上时最常见的根因。

Windows 防火墙的规则是按「网络配置文件」（域 / 专用 / 公用）分开的，一条规则可以
只对其中一个生效。而程序第一次监听时弹的「是否允许访问」对话框，只会给**当时活动**
的那个配置文件建规则：在手机热点（被 Windows 判为「公用」）上点过「允许」，
后来换成宿舍/家里的 WiFi（被判为「专用」），那条规则就不再匹配了。

这个故障的现象很有迷惑性，值得记下来：电脑端明明在 0.0.0.0 上好好监听、手机也 ping
得通电脑，但页面就是打不开，而且**同一个 WiFi 下别的局域网互传 App 也一起失灵**——
于是很容易误判成路由器坏了或者手机有问题，去折腾 AP 隔离、DHCP 这些方向。

这里只做两件事，都是只读的：

1. `probe()` 查一次当前状态：活动的配置文件里，有没有启用中的入站放行规则覆盖指定端口；
2. `diagnose()` 把状态翻译成一句人话（没问题时返回 None）。

**不改系统设置**——动防火墙要管理员权限，得用户自己点头。`fix_command()` 只负责
把该执行的命令拼出来，顺带保证它带上 `profile=any`：漏掉这个参数正是上面那个坑的
源头，netsh 会按当时活动的配置文件建规则，换个网络就再次失效。
"""
import logging
import subprocess
import sys

log = logging.getLogger("pc.firewall")

# NET_FW_PROFILE2_* 的位掩码
PROFILE_NAMES = {1: "域", 2: "专用", 4: "公用"}

# NET_FW_IP_PROTOCOL_*
TCP = 6
UDP = 17
ANY_PROTO = 256

MDNS_PORT = 5353


def default_specs(port):
    """要检查的端口：业务端口（手机连页面）+ mDNS（手机端自动发现）。"""
    return [(TCP, int(port)), (UDP, MDNS_PORT)]


# 枚举规则用的是 COM 接口 HNetCfg.FwPolicy2：实测约 0.2 秒扫完 1600 条规则，
# 而 Get-NetFirewallRule 要几十秒，启动时用不起。
_PROBE_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
# 不显式指定的话，PowerShell 按系统 ANSI 代码页（中文机是 GBK）写 stdout，
# 而 Python 那边按 UTF-8 解码，一遇到非 ASCII 字节就整个探测失败。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$prog = '__PROG__'
$specs = __SPECS__
$fw = New-Object -ComObject HNetCfg.FwPolicy2
if (-not $fw) { exit 1 }
$cur = [int]$fw.CurrentProfileTypes
$active = @()
$enabled = @()
foreach ($t in 1,2,4) {
  if ($cur -band $t) {
    $active += $t
    if ($fw.FirewallEnabled($t)) { $enabled += $t }
  }
}
$rules = @()
foreach ($r in $fw.Rules) {
  if (-not $r.Enabled) { continue }
  if ([int]$r.Direction -ne 1) { continue }
  # 只认「对所有程序生效」和「对本程序生效」两种。
  # 这里有两个坑，都会把规则误当成对我们生效，导致自检永远不报警：
  #   1) 应用商店/UWP 应用的规则 ApplicationName 是空的，它其实靠 LocalAppPackageId
  #      （包 SID）限定作用域——必须跳过；
  #   2) 服务作用域的规则（如 mDNS 那条只给 dnscache 用的）对普通进程不生效。
  $app = [string]$r.ApplicationName
  if ($app -and $app -ne $prog) { continue }
  if (-not $app) {
    $pkg = [string]$r.LocalAppPackageId
    if ($pkg) { continue }
    $svc = [string]$r.ServiceName
    if ($svc) { continue }
  }
  $rules += ,@([int]$r.Protocol, [string]$r.LocalPorts, [int]$r.Action, [int]$r.Profiles)
}
foreach ($s in $specs) {
  $proto = [int]$s.P
  $want = [int]$s.L
  $cov = @{}
  $blk = @{}
  foreach ($r in $rules) {
    $rp = $r[0]
    if ($rp -ne $proto -and $rp -ne 256) { continue }
    $ports = $r[1]
    $hit = $false
    if ($ports -eq '' -or $ports -eq '*') { $hit = $true }
    elseif ($ports -match "(^|,)$want(,|$)") { $hit = $true }
    elseif ($ports -match '(^|,)(\d+)-(\d+)(,|$)') {
      if ($want -ge [int]$Matches[2] -and $want -le [int]$Matches[3]) { $hit = $true }
    }
    if (-not $hit) { continue }
    foreach ($t in 1,2,4) {
      if ($r[3] -band $t) {
        if ($r[2] -eq 1) { $cov[$t] = $true } else { $blk[$t] = $true }
      }
    }
  }
  $c = @(); foreach ($t in 1,2,4) { if ($cov[$t]) { $c += $t } }
  $b = @(); foreach ($t in 1,2,4) { if ($blk[$t]) { $b += $t } }
  "S|$proto|$want|" + ($active -join ',') + "|" + ($enabled -join ',') + "|" + ($c -join ',') + "|" + ($b -join ',')
}
"""


def fix_command(port, protocol="TCP", name=None, localport=None):
    """放行端口的命令（要管理员权限执行）。"""
    lp = int(localport if localport is not None else port)
    nm = name or (f"accessibility-assistant-{lp}" if protocol == "TCP"
                  else f"accessibility-assistant-{protocol.lower()}{lp}")
    return (
        f'netsh advfirewall firewall add rule name="{nm}" '
        f"dir=in action=allow protocol={protocol} localport={lp} profile=any"
    )


def fix_commands(port):
    """完整放行所需的两条命令：业务端口 + mDNS。"""
    return [fix_command(port, "TCP"), fix_command(port, "UDP", localport=MDNS_PORT)]


def _names(bits):
    return "、".join(PROFILE_NAMES.get(b, str(b)) for b in sorted(bits))


def _parse_bits(text):
    return {int(x) for x in (text or "").strip().split(",") if x.strip().isdigit()}


def parse_probe(text):
    """把探测输出解析成 {(proto, port): {'active'|'enabled'|'covered'|'blocked': set[int]}}。

    单独拆出来是为了能脱机测：这段逻辑要是读错，给出的结论会把人带偏，比不检查更糟。
    """
    out = {}
    for line in (text or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) != 7 or parts[0] != "S":
            continue
        try:
            key = (int(parts[1]), int(parts[2]))
        except ValueError:
            continue
        out[key] = {
            "active": _parse_bits(parts[3]),
            "enabled": _parse_bits(parts[4]),
            "covered": _parse_bits(parts[5]),
            "blocked": _parse_bits(parts[6]),
        }
    return out


def diagnose(results, port):
    """把探测结果翻译成人话；一切正常时返回 None。"""
    if not results:
        return None
    msgs = []
    tcp = results.get((TCP, int(port)))
    if tcp:
        on = tcp["active"] & tcp["enabled"]
        hit = on & tcp["blocked"]
        miss = on - tcp["covered"]
        if hit:
            msgs.append(
                f"⚠ 防火墙有规则明确拦着端口 {port}（{_names(hit)}），手机连不上。"
                f"到「Windows 安全中心 → 防火墙和网络保护 → 高级设置 → 入站规则」里删掉它。"
            )
        elif miss:
            msgs.append(
                f"⚠ 当前网络是「{_names(miss)}」，却没有入站规则放行端口 {port}——"
                f"手机打不开多半就是这个原因（同一个 WiFi 下别的互传 App 也会一起失灵）。\n"
                f"  放行（需要管理员，执行一次即可，以后换网络都不用再管）：\n"
                f"    {fix_commands(port)[0]}"
            )
    mdns = results.get((UDP, MDNS_PORT))
    if mdns:
        on = mdns["active"] & mdns["enabled"]
        if on - mdns["covered"] and not msgs:
            # 只是自动发现会退化，页面照样能开，所以单独一句、不说成故障
            msgs.append(
                f"  提示：mDNS（UDP {MDNS_PORT}）没放行，手机端「自动搜索」可能搜不到，"
                f"用扫码或手填地址不受影响。要放行就执行：\n"
                f"    {fix_commands(port)[1]}"
            )
    return "\n".join(msgs) if msgs else None


def probe(specs, program=None, timeout=8.0):
    """跑一次探测。任何失败都返回 None——自检失败不该影响主流程。"""
    pairs = ", ".join("@{{P={0};L={1}}}".format(int(p), int(l)) for p, l in specs)
    script = _PROBE_SCRIPT.replace("__SPECS__", "@(" + pairs + ")")
    script = script.replace("__PROG__", (program or "").replace("'", "''"))
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            # 不加这个，从 exe 里调起来会闪一个黑框
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        log.debug("防火墙自检没跑起来：%s: %r", type(e).__name__, e)
        return None
    if proc.returncode != 0:
        log.debug("防火墙自检返回码 %s", proc.returncode)
        return None
    return parse_probe(proc.stdout)


def check(port, program=None):
    """探测 + 诊断一步到位，返回 (results, 提示语或 None)。"""
    results = probe(default_specs(port), program if program is not None else sys.executable)
    return results, diagnose(results, port)
