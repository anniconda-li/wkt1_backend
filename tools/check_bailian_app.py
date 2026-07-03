"""百炼应用手动测试脚本。

简单的命令行工具，用于手动测试百炼 AI 应用的调用。
发送一个问题文本并打印 AI 回复。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.config  # noqa: E402,F401 - 加载项目 .env 环境变量
from services.bailian_app_service import BailianAppService

DEFAULT_TEXT = "大雁塔和西游记有什么关系？"


def main() -> None:
    """主函数：创建百炼服务并发送测试问题。"""
    parser = argparse.ArgumentParser(description="手动测试百炼问答应用")
    parser.add_argument("--text", "-t", default=DEFAULT_TEXT, help="要发送给百炼应用的问题文本")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    service = BailianAppService()
    answer = service.ask(args.text)
    print(answer)


if __name__ == "__main__":
    main()
