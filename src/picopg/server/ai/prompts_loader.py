"""版本化的提示词：prompts/<版本>.md。服务构造时读一次（启动阶段），不在导入时读文件。"""

from __future__ import annotations

import re
from pathlib import Path

PROMPTS = Path(__file__).with_name("prompts")
_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,62}")


def load_prompt(version: str, variable: str) -> str:
    """版本名只能是简单的名字（不能带路径）；名字不对或文件不存在都是配置错误，报出变量名与取值。"""
    if _NAME.fullmatch(version) is None:
        raise ValueError(f"{variable}={version!r} is not a prompt name")
    try:
        return (PROMPTS / f"{version}.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"{variable}={version!r}: there is no prompts/{version}.md") from None
