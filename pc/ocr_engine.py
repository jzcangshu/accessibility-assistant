"""本地 OCR（可选组件 rapidocr-onnxruntime，离线可用，无需联网）。

识别为空时自动把图片放大 2 倍再试一次，提升小字/低分辨率场景的识别率。
"""
import io
import logging
import threading

import numpy as np
from PIL import Image

log = logging.getLogger("ocr")
_ocr = None
_lock = threading.Lock()


def _get():
    global _ocr
    with _lock:
        if _ocr is None:
            from rapidocr_onnxruntime import RapidOCR

            log.info("正在初始化 RapidOCR（首次调用需要数秒）…")
            _ocr = RapidOCR()
    return _ocr


def _run(img) -> str:
    arr = np.array(img)[:, :, ::-1].copy()  # RapidOCR 按 cv2 约定读 BGR
    result, _ = _get()(arr)
    if not result:
        return ""
    return "\n".join(line[1] for line in result)


def recognize(jpeg: bytes) -> str:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    text = _run(img)
    if not text:
        big = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        text = _run(big)
        if text:
            log.info("OCR 放大 2x 后识别成功")
    return text
