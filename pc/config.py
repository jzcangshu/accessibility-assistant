"""配置加载/保存：config.json 深合并到默认配置上。"""
import copy
import json
import logging
import os
import sys
from pathlib import Path
from .swipe import DEFAULT_PARAMS

log = logging.getLogger("pc.config")


def _base_dir():
    """可写数据（config/captures/logs）目录：打包后为 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resource_dir():
    """只读资源（web 页面）目录：打包后为解包临时目录。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
RESOURCE_ROOT = _resource_dir()
ROOT = BASE_DIR  # 兼容别名：可写数据根目录

DEFAULTS = {
    "server": {"host": "0.0.0.0", "port": 8765},
    "hotkeys": {
        "capture": "f8",
        "calibrate": "f9",
        "capture_region": "f10",
        "quit": "ctrl+alt+q",
        "window": "ctrl+alt+h",
    },
    "capture": {"monitor": "primary", "jpeg_quality": 82, "max_width": 1600},
    "corner_snap": {"enabled": True, "radius": 8, "cooldown": 1.5},
    "swipe_snap": {"enabled": True, **DEFAULT_PARAMS},
    "region": None,
    "ocr": {"enabled": False, "send_text_only": False},
    "transfer": {"mode": "lan", "relay_url": "", "relay_token": ""},
    "history_dir": "captures",
    # 悬浮窗：只记住透明度（30–100，按 10 递减）；开关状态不持久化，启动时关闭。
    "overlay": {"opacity": 90},
    "allow_test_api": True,
    "stealth": {
        "enabled": True,
        "anti_capture": True,
        "title": "",
    },
}


def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _path(path=None):
    return Path(path) if path else ROOT / "config.json"


def load_config(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    p = _path(path)
    if p.exists():
        try:
            _merge(cfg, json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            log.warning("config.json 解析失败，使用默认配置: %s", e)
    return cfg


def save_config(cfg, path=None):
    p = _path(path)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
