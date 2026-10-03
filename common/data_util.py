"""测试数据读取：JSON / YAML 两种格式都支持。

@pytest.mark.parametrize 的数据源就用这里，做到「用例逻辑与数据分离」。
"""
import json
from pathlib import Path

from common.yaml_util import DATA_DIR, load_yaml


def load_json(file_name: str) -> dict:
    path = Path(DATA_DIR) / file_name
    if not path.exists():
        raise FileNotFoundError(f"数据文件不存在: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


__all__ = ["load_yaml", "load_json", "DATA_DIR"]
