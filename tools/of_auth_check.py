"""OnlyFans credential / API diagnostic.

Checks, in order:

1. that ``onlyfans_config.ini`` has all four credentials,
2. a signed request to ``/users/me`` (this is what "Test Connection" does),
3. resolving one of your configured creators by username.

On failure it prints OnlyFans' own error message (the GUI used to show only
"400 Client Error") plus a hint about what to do. Use it whenever downloads stop
working with an API error.

Usage::

    python tools/of_auth_check.py
    python tools/of_auth_check.py pedrotubarao      # check a specific creator

Nothing secret is printed.
"""

from __future__ import annotations

import sys

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app_version import APP_VERSION  # noqa: E402
from api.onlyfans_api import describe_api_error  # noqa: E402
from config.onlyfans_config import OnlyFansConfig, load_onlyfans_config  # noqa: E402
from textio import print_error, print_info, print_warning  # noqa: E402


HINTS = (
    ('refresh the page', (
        'OnlyFans\' WAF rejected the request. It is usually transient.\n'
        '  - Wait a minute and run this check again, or\n'
        '  - reload onlyfans.com in your browser (F5), then copy sess and x-bc again.'
    )),
    ('invalid auth info', (
        'The session cookie is no longer valid:\n'
        '  log in to onlyfans.com in your browser and copy sess, auth_id, x-bc and\n'
        '  user_agent again (they must come from the same browser session).'
    )),
    ('user not found', (
        'That username does not exist (anymore), or it is misspelled.'
    )),
    ('country', (
        'OnlyFans may be blocking your region/VPN. Try another network.'
    )),
)


def print_hint(message: str) -> None:
    lowered = (message or '').lower()

    for marker, hint in HINTS:
        if marker in lowered:
            print_info(f'  Hint: {hint}')
            return

    print_info('  Hint: check the credentials and try again in a minute.')


def main() -> int:
    creator = sys.argv[1] if len(sys.argv) > 1 else None

    config = OnlyFansConfig(program_version=APP_VERSION)
    config.config_path = REPO_ROOT / 'onlyfans_config.ini'
    load_onlyfans_config(config)

    print()
    print_info(f'Config file: {config.config_path}')
    print_info(f'Auth ID:     {config.auth_id or "(missing)"}')
    print_info(f'Credentials: {"complete" if config.has_credentials() else "INCOMPLETE"}')

    if not config.has_credentials():
        print_error(
            'Missing credentials. Fill sess, auth_id, user_agent and x_bc in '
            'onlyfans_config.ini (or in the GUI OnlyFans tab).'
        )
        return 2

    api = config.get_api()

    print()
    print_info('1) Testing the signed request to /users/me ...')

    try:
        ok = api.test_auth()
    except Exception as error:
        print_error(f'   FAILED: {type(error).__name__}: {error}')
        response = getattr(error, 'response', None)

        if response is not None:
            print_hint(describe_api_error(response))

        return 1

    print_info(f'   {"OK" if ok else "FAILED"}')

    if not ok:
        return 1

    creators = sorted(config.user_names or [])
    target = creator or (creators[0] if creators else None)

    if not target:
        print()
        print_warning('No creator configured in onlyfans_config.ini - skipping step 2.')
        return 0

    print()
    print_info(f'2) Resolving creator "{target}" ...')

    try:
        user = api.get_user_by_username(target)
    except Exception as error:
        print_error(f'   FAILED: {type(error).__name__}: {error}')
        response = getattr(error, 'response', None)

        if response is not None:
            print_hint(describe_api_error(response))

        return 1

    print_info(f'   OK - id {user.get("id")}, {user.get("name") or "(no display name)"}')
    print()
    print_info('Authentication and API access work. If downloads still fail, the')
    print_info('problem is not the credentials - check the console log for the')
    print_info('per-file messages (they start with a cross or a circle).')

    return 0


if __name__ == '__main__':
    sys.exit(main())
