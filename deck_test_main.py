import importlib
import os

import app  # noqa: E402

# The harness wiped NiceGUI's global UI state before running this file as
# __main__; 'app' is already in sys.modules (tests imported it), so a plain
# import is a no-op -- reload it to re-register @ui.page("/"). Reload mutates
# the SAME module object, so tests share its STATE.
importlib.reload(app)

from nicegui import ui  # noqa: E402

# The test harness expects ui.run() to have been called
# (NICEGUI_USER_SIMULATION keeps it from binding a real port).
ui.run(title="Command Deck", favicon="\U0001f4b0", port=int(os.environ.get("PORT", "8095")),
       reload=False, show=False, storage_secret="command-deck-test-storage")
