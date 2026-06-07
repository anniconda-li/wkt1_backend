from __future__ import annotations

import logging

import core.config  # noqa: F401 - loads project .env
from services.bailian_app_service import BailianAppService


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    service = BailianAppService()
    answer = service.ask("大雁塔和西游记有什么关系？")
    print(answer)


if __name__ == "__main__":
    main()
