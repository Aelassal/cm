from odoo import models, api, _
from odoo.exceptions import ValidationError

class ResUsers(models.Model):
    _inherit = 'res.users'

    @api.constrains('active', 'share')
    def _check_internal_user_limit(self):
        for user in self:
            if user.active and not user.share:
                # Fetch the limit from config parameter (default to 5 if not set)
                limit = int(self.env['ir.config_parameter'].sudo().get_param('user_limit_enforcer.allowed_users_count', 5))
                active_internal = self.search_count([('active', '=', True), ('share', '=', False)])
                if active_internal > limit:
                    raise ValidationError(_("User limit reached: Only %d internal users allowed." % limit)) 