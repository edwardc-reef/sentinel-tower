# Import all handlers to trigger @register decoration.
from apps.notifications.handlers import (
    admin_utils,  # noqa: F401
    coldkey_swap,  # noqa: F401
    subnet_dissolution,  # noqa: F401
    subnet_owner_change,  # noqa: F401
    subnet_registration,  # noqa: F401
    sudo,  # noqa: F401
)
