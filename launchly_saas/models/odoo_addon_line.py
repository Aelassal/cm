from odoo import models, fields, api

class OdooAddonLine(models.Model):
    _name = 'odoo.addon.line'
    _description = 'Odoo Addon Line (All Addons in Instance)'
    _rec_name = 'name'
    name = fields.Char(string='Addon Name', required=True)
    state = fields.Selection([
        ('installed', 'Installed'),
        ('uninstalled', 'Uninstalled')
    ], string='State', default='uninstalled', required=True)
    instance_id = fields.Many2one('odoo.docker.instance', string='Instance', ondelete='cascade', required=True)
    summary = fields.Char(string='Summary')
    latest_version = fields.Char(string='Latest Version')
    technical_name = fields.Char(string='Technical Name')
    application = fields.Boolean(string='Application', help='Is this addon an application?')
    license = fields.Char(string='License', help='License of the addon')
    display_name = fields.Char(string='Display Name')
    # Add more fields as needed (e.g., author, website, etc.)

    def action_install(self):
        for addon in self:
            if addon.state != 'installed':
                addon.instance_id.install_custom_addon_in_odoo([addon.name])
                addon.state = 'installed'

    def action_uninstall(self):
        for addon in self:
            if addon.state == 'installed':
                addon.instance_id.uninstall_addon_in_odoo([addon.name])
                addon.state = 'uninstalled' 