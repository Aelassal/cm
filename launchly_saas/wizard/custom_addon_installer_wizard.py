import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class CustomAddonInstallerWizard(models.TransientModel):
    _name = 'custom.addon.installer.wizard'
    _description = 'Custom Addon Installer Wizard'

    instance_id = fields.Many2one('odoo.instance', string='Instance', required=True)
    addon_line_ids = fields.Many2many('custom.addon.line', string='Addons to Install',
                                      domain="[('instance_id', '=', instance_id), ('is_extracted', '=', True)]")
    install_method = fields.Selection([
        ('restart', 'Restart Instance (Recommended)'),
        ('api', 'Install via API (Experimental)')
    ], string='Installation Method', default='restart', required=True)
    
    def action_install_addons(self):
        """Install selected custom addons"""
        if not self.addon_line_ids:
            raise UserError(_("Please select at least one addon to install."))
        
        if self.instance_id.state != 'running':
            raise UserError(_("Instance must be running to install addons."))
        
        addon_names = self.addon_line_ids.mapped('addon_name')
        
        if self.install_method == 'restart':
            # Apply changes and restart
            self.instance_id.apply_custom_addons_changes()
            self.instance_id.add_to_log(f"[INFO] Instance restarted with addons: {', '.join(addon_names)}")
            self.instance_id.add_to_log("[INFO] You can now install the addons from the Apps menu")
            
        elif self.install_method == 'api':
            # Try to install via API
            success = self.instance_id.install_custom_addon_in_odoo(addon_names)
            if not success:
                self.instance_id.add_to_log("[WARNING] API installation failed. Try restarting the instance instead.")
        
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    @api.model
    def default_get(self, fields):
        """Set default values from context"""
        res = super().default_get(fields)
        if self.env.context.get('active_model') == 'odoo.instance':
            res['instance_id'] = self.env.context.get('active_id')
        return res 