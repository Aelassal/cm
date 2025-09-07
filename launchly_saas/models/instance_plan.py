from odoo import models, fields, api

class InstancePlan(models.Model):
    _name = 'instance.plan'
    _description = 'Instance Plan Template'

    name = fields.Char(string='Plan Name', required=True)
    is_demo = fields.Boolean(string='Create with Demo Data', default=False)
    allowed_users_count = fields.Integer(string='Allowed Users Count', default=1)
    allowed_modules_count = fields.Integer(string='Allowed Modules Count', default=100)
    template_id = fields.Many2one(
        'docker.compose.template',
        string='Docker Compose Template',
        help='Select a docker compose template for this plan.'
    )
    config_id = fields.Many2one(
        'saas.config',
        string='Configuration',
        compute='_compute_config_id',
        store=True
    )

    @api.depends('template_id')
    def _compute_config_id(self):
        for record in self:
            # Example logic:
            # If instance_url is set, assign first saas.config record, else clear config_id
            if record.template_id:
                record.config_id = self.env['saas.config'].search([], limit=1)
            else:
                record.config_id = False

    # Computed field to get the instance ID for domain filtering
    instance_id_for_domain = fields.Many2one(
        'odoo.docker.instance',
        string='Instance for Domain',
        compute='_compute_instance_id_for_domain',
        store=False
    )

    @api.depends('config_id', 'config_id.instance_id')
    def _compute_instance_id_for_domain(self):
        for record in self:
            if record.config_id and record.config_id.instance_id:
                record.instance_id_for_domain = record.config_id.instance_id
            else:
                record.instance_id_for_domain = False

    custom_addon_line_ids = fields.Many2many(
        'custom.addon.line',
        'instance_plan_custom_addon_rel',
        'plan_id', 'custom_addon_id',
        string='Custom Addons',
        help='Select custom addons from the instance associated with this plan\'s configuration.'
    )
    
    @api.onchange('config_id')
    def _onchange_config_id(self):
        """Clear custom addon selections when config changes"""
        if self.config_id:
            # Optionally, you could auto-populate with the instance's custom addons here
            pass
        else:
            self.custom_addon_line_ids = [(5, 0, 0)]  # Clear all selections
    
    def get_available_custom_addons(self):
        """Get available custom addons based on config_id.instance_id"""
        self.ensure_one()
        if self.config_id and self.config_id.instance_id:
            return self.config_id.instance_id.custom_addon_line
        return self.env['custom.addon.line']
    
    odoo_addon_line_ids = fields.Many2many(
        'odoo.addon.line',
        'instance_plan_odoo_addon_rel',
        'plan_id', 'odoo_addon_id',
        string='Odoo Addons',
        help='Select Odoo addons from any instance to include in this plan.'
    )
    instance_ids = fields.One2many('odoo.docker.instance', 'plan_id', string='Instances Using This Plan') 