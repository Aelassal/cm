from odoo import models , fields

class Project(models.Model):
    _inherit = 'project.project'

    subscription_id = fields.Many2one('launchly.subscription', string='Subscription', ondelete='set null')
