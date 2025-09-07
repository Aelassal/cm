from . import models
from . import hooks
from .hooks import hide_apps_menu, restore_apps_menu
import sys
current_module = sys.modules[__name__]
setattr(current_module, 'hide_apps_menu', hide_apps_menu)
setattr(current_module, 'restore_apps_menu', restore_apps_menu)
