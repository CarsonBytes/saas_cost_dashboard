pytest_plugins = ["nicegui.testing.user_plugin"]

import app  # noqa: F401,E402 -- registers the page; ui.run stays guarded

