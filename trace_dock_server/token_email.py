"""Recover an email from a v2 token without contacting the database."""
import getpass
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

from .settings import SERVER_ROOT
from .token_security import recover_email


def main():
    env_file = Path(os.environ.get("TRACE_DOCK_ENV_FILE", SERVER_ROOT / ".env")).expanduser()
    load_dotenv(env_file, override=False)
    pepper = os.environ.get("TOKEN_PEPPER")
    if not pepper or len(pepper) < 32:
        print("TOKEN_PEPPER 未配置或长度不足；请设置环境变量或配置文件。", file=sys.stderr)
        raise SystemExit(2)

    token = getpass.getpass("Token: ") if sys.stdin.isatty() else sys.stdin.readline().strip()
    if not token:
        print("未读取到 token。", file=sys.stderr)
        raise SystemExit(2)
    try:
        print(recover_email(token, pepper))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
