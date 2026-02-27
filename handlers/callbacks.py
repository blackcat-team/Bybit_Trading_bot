"""
Facade — re-exports from thematic sub-modules.
Keeps handlers.__init__ and bot_handlers working without import changes.
"""

from handlers.commands import (                    # noqa: F401
    start_trading, stop_trading,
    set_risk_command, add_note_handler,
)
from handlers.buttons import button_handler        # noqa: F401
from handlers.views_orders import (                # noqa: F401
    view_orders, view_symbol_orders,
)
from handlers.views_positions import (             # noqa: F401
    check_positions,
)
from handlers.reporting import send_report         # noqa: F401
from handlers.startup import (                     # noqa: F401
    STARTUP_MARKER_FILE, on_startup_check,
)
