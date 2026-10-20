"""PyInstaller 打包入口（保持 pc 包的相对导入可用）。

打包为无控制台 exe（--windowed）时 sys.stdout/stderr 可能是 None——先兜上空流，
后面任何一句 print / 日志都不会因此炸。
"""
import sys


class _NullStream:
    encoding = "utf-8"
    errors = "replace"

    def write(self, s):
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


if sys.stdout is None:
    sys.stdout = _NullStream()
if sys.stderr is None:
    sys.stderr = _NullStream()

from pc.main import main  # noqa: E402

if __name__ == "__main__":
    main()
