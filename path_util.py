"""统一路径定位（兼容源码运行 + PyInstaller 打包运行）
PyInstaller 打包后 __file__ 会指向临时解压目录(_MEIPASS)，导致脚本用 Path(__file__).parent.parent
定位 BASE 时指向错误位置。本模块统一区分两类路径：

- data_root()   : 可写数据根（报告/看板/状态/归档/PDF/文本）。打包后 = exe 所在目录，源码 = project 根。
- resource_root(): 只读资源根（看板模板 index.html、lib/echarts.min.js）。打包后 = _MEIPASS，源码 = project 根。

各脚本只需：
    from path_util import data_root, resource_root
    BASE = data_root()
    RES  = resource_root()
"""
import sys
from pathlib import Path


def is_frozen():
    """是否 PyInstaller 打包运行。"""
    return bool(getattr(sys, "frozen", False))


def _project_root():
    """本文件所在目录（project 根）。"""
    return Path(__file__).resolve().parent


def data_root():
    """可写数据根目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _project_root()


def resource_root():
    """只读资源根目录。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return _project_root()
