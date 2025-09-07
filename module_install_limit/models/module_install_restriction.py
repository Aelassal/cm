from odoo import models, _
from odoo.exceptions import UserError

class IrModuleModule(models.Model):
    _inherit = 'ir.module.module'

    def button_immediate_install(self):
        env = self.env
        # Get allowed addons list from config parameter (comma-separated string)
        allowed_addons_str = env['ir.config_parameter'].sudo().get_param(
            'module_install_limit.allowed_module_names', ''
        )
        allowed_addon_names = allowed_addons_str.split(',')

        # Check each module being installed
        for module in self:
            if module.name not in allowed_addon_names:
                raise UserError(_(
                    f"You are not allowed to install the module '{module.name}'. "
                    f"It is not whitelisted in your instance configuration."
                ))

        return super().button_immediate_install()
