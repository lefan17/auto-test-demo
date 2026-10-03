"""YAML 读取工具：所有配置和数据文件统一从这里读。

为什么要有这一层：用例里直接 open() 读文件，路径会随工作目录变化而失效；
集中在这里用「项目根目录」拼绝对路径，才能保证从任何目录跑都正常。
"""
from pathlib import Path

import yaml

# common/yaml_util.py -> common/ -> 项目根目录
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"


def load_yaml(file_name: str) -> dict:
    """读取 data/ 目录下的 YAML 文件。"""
    path = DATA_DIR / file_name
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    # encoding 必须显式指定，Windows 默认 GBK 会读乱中文
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_config() -> dict:
    """读取全局配置 config.yaml。"""
    return load_yaml("config.yaml")
