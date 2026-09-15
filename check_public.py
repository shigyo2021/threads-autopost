"""公開リポジトリに入れてはいけないものが、コミットしようとしている差分にないか検査する

    py check_public.py            ステージ済みの差分を検査（pre-commit フックから呼ばれる）

見つけるもの: .env などの秘密ファイル／トークン・キーらしき文字列／PCのユーザー名入りのパス／メールアドレス
（検査用の値そのものはこのファイルに書かない。公開されるため）
"""

import re
import subprocess
import sys

from queue_store import contains_secret

FORBIDDEN_FILES = re.compile(r"(^|/)\.env($|\.(?!example$))")
LOCAL_PATH = re.compile(r"[A-Za-z]:\\\\?Users\\\\?(?!（ユーザー名）|<)[^\\\s\"']+", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def main() -> int:
    problems = []

    for name in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines():
        if FORBIDDEN_FILES.search(name):
            problems.append(f"{name}: 秘密情報のファイルはコミットしない")

    current_file = ""
    for line in _git("diff", "--cached", "-U0", "--diff-filter=ACMR").splitlines():
        if line.startswith("+++ "):
            current_file = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        if contains_secret(added):
            problems.append(f"{current_file}: トークンやキーらしき文字列")
        if LOCAL_PATH.search(added):
            problems.append(f"{current_file}: PCのユーザー名入りのパス")
        if EMAIL.search(added):
            problems.append(f"{current_file}: メールアドレス")

    if problems:
        print("❌ 公開してはいけないものが含まれている可能性があるため、コミットを止めました:")
        for p in sorted(set(problems)):
            print(f"   - {p}")
        print("   （値そのものは表示していません。該当の行を直すか、ステージから外してください）")
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
