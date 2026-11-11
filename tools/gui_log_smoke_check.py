"""GUI smoke check for the console log window.

Opens the real GUI (skipping the setup wizard), injects log messages the same way
the download threads do, and verifies that the "Console Log" window shows them:

1. log window opened *after* the OnlyFans tab was built,
2. log window opened *before* the OnlyFans tab was built,
3. messages emitted before the window was opened are replayed.

Regression guard for the bug where OnlyFans downloads logged to the tab's inline
log and the Console Log window stayed empty.

Run it with the project's interpreter:

    .\\.venv\\Scripts\\python.exe tools\\gui_log_smoke_check.py

It opens and closes a few windows and writes its scratch files to
``.testtmp/gui_demo``.
"""

import os
import sys
import time
import traceback

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

WORK = REPO / '.testtmp' / 'gui_demo'
WORK.mkdir(parents=True, exist_ok=True)
os.chdir(WORK)

import gui.app  # noqa: E402

# Skip the setup wizard (we only want to exercise the log plumbing)
gui.app.should_run_wizard = lambda path: False

from gui.window import MainWindow  # noqa: E402


def pump(app, seconds: float) -> None:
    end = time.time() + seconds

    while time.time() < end:
        try:
            app.update()
        except Exception:
            return
        time.sleep(0.02)


def window_text(app) -> str:
    return app.log_window.log_text.get('1.0', 'end')


FAILS = []


def check(name, condition, extra=''):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f' â€” {extra}' if extra else ''))

    if not condition:
        FAILS.append(name)


def scenario_open_after_tab() -> None:
    print('\n1) Log window opened AFTER building the OnlyFans tab')
    app = MainWindow()
    pump(app, 1.2)

    app.tab_view.set('OnlyFans')
    app._on_tab_changed()
    pump(app, 0.3)

    app.toggle_log_window()
    pump(app, 0.3)

    app.of_handlers.on_log('MENSAJE GUI OF', 'error')
    pump(app, 0.8)

    text = window_text(app)
    check('OF message appears in Console Log', 'MENSAJE GUI OF' in text, text.strip()[-80:])
    check('main window still alive', app.winfo_exists() == 1)
    app.destroy()


def scenario_open_before_tab() -> None:
    print('\n2) Log window opened BEFORE building the OnlyFans tab')
    app = MainWindow()
    pump(app, 1.2)

    app.toggle_log_window()
    pump(app, 0.3)

    app.tab_view.set('OnlyFans')
    app._on_tab_changed()
    pump(app, 0.4)

    app.of_handlers.on_log('MENSAJE GUI OF 2', 'warning')
    pump(app, 0.8)

    text = window_text(app)
    check('OF message appears in Console Log', 'MENSAJE GUI OF 2' in text, text.strip()[-80:])
    app.destroy()


def scenario_history_replay() -> None:
    print('\n3) Messages emitted BEFORE the window was opened are replayed')
    app = MainWindow()
    pump(app, 1.2)

    app.handlers.on_log('MENSAJE PREVIO', 'info')
    pump(app, 0.6)

    app.toggle_log_window()
    pump(app, 0.4)

    text = window_text(app)
    check('earlier message is replayed', 'MENSAJE PREVIO' in text, text.strip()[-80:])
    app.destroy()


def main() -> int:
    try:
        scenario_open_after_tab()
        scenario_open_before_tab()
        scenario_history_replay()

    except Exception:
        traceback.print_exc()
        return 1

    print()

    if FAILS:
        print(f'FAILED: {len(FAILS)} check(s): {", ".join(FAILS)}')
        return 1

    print('GUI log smoke test passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

