#!/usr/bin/env python
"""Send one message to an A2A agent and print its reply.

Usage:
    python scripts/a2a_ping.py <agent_url> "<text>"

Example (Table 7 SRE Copilot):
    python scripts/a2a_ping.py https://t07-sre-copilot-xxxx-ew.a.run.app \
        "Is checkout-payment-svc healthy right now?"
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seed_packs.t07_self_healing_ops.a2a_client import send_a2a_text  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    reply = asyncio.run(send_a2a_text(sys.argv[1], sys.argv[2], timeout_s=120))
    print(reply or "(no text reply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
