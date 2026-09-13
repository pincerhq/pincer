#!/usr/bin/env python3
"""Answer one question: can this Twilio account place a call from this number?

Twilio error 21210 ("the source phone number provided is not yet verified for
your account") is almost never a problem with the number — it is the app
pointing at a different Twilio project than the one that owns it. The account
SID never appears in the console next to the number, so the mismatch is easy to
stare past. This prints both sides.

Usage:
    # the configured account, read from .env
    python scripts/check_twilio_number.py

    # a candidate project, before committing it to .env
    python scripts/check_twilio_number.py --sid ACxxxx --token yyyy --number +16615350105

Exit 0 when the number is usable as a `From` on that account, 1 otherwise.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ENV_LINE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*(?:#.*)?$")


def read_env(path: pathlib.Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        m = ENV_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sid", help="Account SID (default: PINCER_TWILIO_ACCOUNT_SID)")
    ap.add_argument("--token", help="Auth token (default: PINCER_TWILIO_AUTH_TOKEN)")
    ap.add_argument("--number", help="From number to test (default: PINCER_TWILIO_PHONE_NUMBER)")
    ap.add_argument("--env", default=".env", help="env file to read defaults from")
    args = ap.parse_args()

    env = read_env(pathlib.Path(args.env))
    sid = args.sid or env.get("PINCER_TWILIO_ACCOUNT_SID", "")
    token = args.token or env.get("PINCER_TWILIO_AUTH_TOKEN", "")
    number = args.number or env.get("PINCER_TWILIO_PHONE_NUMBER", "")

    if not sid or not token:
        print("No credentials: pass --sid/--token or set them in", args.env, file=sys.stderr)
        return 1

    try:
        from twilio.rest import Client
    except ImportError:
        print("Twilio SDK not installed: uv pip install 'pincer-agent[voice]'", file=sys.stderr)
        return 1

    client = Client(sid, token)
    try:
        account = client.api.accounts(sid).fetch()
    except Exception as exc:  # noqa: BLE001 — any failure here is a credential problem
        print(f"Could not authenticate as {sid}: {getattr(exc, 'msg', exc)}", file=sys.stderr)
        return 1

    owned = [n.phone_number for n in client.incoming_phone_numbers.list(limit=200)]
    verified = [v.phone_number for v in client.outgoing_caller_ids.list(limit=200)]

    print(f"account   {sid}")
    print(f"          {account.friendly_name!r}  type={account.type}  status={account.status}")
    print(f"owned     {', '.join(owned) if owned else '(none)'}")
    print(f"verified  {', '.join(verified) if verified else '(none)'}")
    print()

    if not number:
        print("No number to check. Pass --number or set PINCER_TWILIO_PHONE_NUMBER.")
        return 1

    if number in owned:
        print(f"OK  {number} is a Twilio number on this account — usable as From.")
        return 0
    if number in verified:
        print(f"OK  {number} is a verified caller ID on this account — usable as From.")
        if account.type == "Trial":
            print("    Trial account: calls may only go TO a verified number, i.e. one of")
            print(f"    {', '.join(verified)}")
        return 0

    print(f"FAIL  {number} is neither owned nor verified on {sid}.")
    print("      This is what produces Twilio error 21210 at call time.")
    print("      Either set PINCER_TWILIO_PHONE_NUMBER to one of the numbers above,")
    print("      or point PINCER_TWILIO_ACCOUNT_SID/AUTH_TOKEN at the project that")
    print("      owns the number (Twilio Console → project switcher → Account Info).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
