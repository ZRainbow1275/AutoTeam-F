"""Short-lived Playwright probe process.

Runs browser probes that are allowed to be killed as a whole process tree by the
parent process. This keeps periodic background checks from accumulating stuck
Playwright / Chromium children.
"""

from __future__ import annotations

import json
import logging
import sys

logging.basicConfig(level=logging.WARNING)


def _probe_team_member_count() -> dict[str, int]:
    from autoteam.chatgpt_api import ChatGPTTeamAPI
    from autoteam.manager import get_team_member_count

    chatgpt = ChatGPTTeamAPI()
    try:
        chatgpt.start()
        return {"count": int(get_team_member_count(chatgpt))}
    finally:
        chatgpt.stop()


def main() -> None:
    action = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()

    try:
        if action == "team-member-count":
            result = _probe_team_member_count()
        else:
            raise RuntimeError(f"unknown probe action: {action or 'empty'}")
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
