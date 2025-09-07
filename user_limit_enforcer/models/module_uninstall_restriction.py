from odoo import models, _
from odoo.exceptions import UserError

class IrModuleModule(models.Model):
    _inherit = 'ir.module.module'

    def button_immediate_uninstall(self):
        for module in self:
            if module.name == 'user_limit_enforcer' and self.env.uid != 1:
                raise UserError(_("this module cannot be uninstalled"))
        return super(IrModuleModule, self).button_immediate_uninstall()