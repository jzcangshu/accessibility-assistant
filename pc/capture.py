"""屏幕捕获：mss 走系统标准截屏接口，不产生任何系统通知、声音或屏幕变化。"""
import io

from PIL import Image


def open_screen():
    """打开 mss 会话。新版用 `mss.MSS` 类（10.2 起 `mss.mss()` 会打弃用告警），老版退回 `mss.mss()`。"""
    import mss

    return (getattr(mss, "MSS", None) or mss.mss)()


def pick_monitor(sct, spec="primary"):
    """把 capture.monitor 规格解析成 mss 的监视器字典。

    "primary" 不能想当然取 monitors[1]：mss 枚举显示器不保证主屏排第一，
    双屏机器（笔记本 + 外接屏）一旦排错就会一直截到另一块屏。这里用 mss 给的
    is_primary 标记认主屏（来自系统的 MONITORINFOF_PRIMARY，权威）；旧版 mss
    没有这个字段时退回 monitors[1] 的老行为。
    """
    mons = sct.monitors
    if spec == "all":
        return mons[0]
    if spec == "primary":
        for m in mons[1:]:
            if m.get("is_primary"):
                return m
        return mons[1] if len(mons) > 1 else mons[0]
    idx = int(spec)
    return mons[max(1, min(idx, len(mons) - 1))]


def describe_monitors():
    """一行文字列出所有显示器，给启动日志用，例如
    `0:全部拼合 5120x1600  1:-2560,0 2560x1600  2:[主]0,0 2560x1440`。"""
    import mss

    parts = []
    with open_screen() as sct:
        for i, m in enumerate(sct.monitors):
            if i == 0:
                parts.append(f"0:全部拼合 {m['width']}x{m['height']}")
            else:
                mark = "[主]" if m.get("is_primary") else ""
                parts.append(f"{i}:{mark}{m['left']},{m['top']} {m['width']}x{m['height']}")
    return "  ".join(parts)


def grab(region=None, monitor="primary", max_width=1600, quality=82):
    """返回 (发送用jpeg, OCR用全分辨率jpeg, (w, h))。region 为虚拟屏幕坐标 [x, y, w, h]。"""
    with open_screen() as sct:
        if region:
            box = {
                "left": int(region[0]),
                "top": int(region[1]),
                "width": int(region[2]),
                "height": int(region[3]),
            }
        else:
            box = pick_monitor(sct, monitor)
        shot = sct.grab(box)

    img = Image.frombytes("RGB", shot.size, shot.rgb)
    full_buf = io.BytesIO()
    img.save(full_buf, "JPEG", quality=92)
    if img.width > max_width:
        h = max(1, round(img.height * max_width / img.width))
        img = img.resize((max_width, h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=int(quality))
    return buf.getvalue(), full_buf.getvalue(), img.size
