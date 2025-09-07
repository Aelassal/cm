from odoo import models, fields, api
from datetime import datetime
import logging

_logger = logging.getLogger(__name__)

class SubscriptionRenewalHistory(models.Model):
    _name = 'subscription.renewal.history'
    _description = 'Subscription Renewal History'
    _order = 'renewal_date desc'

    name = fields.Char(string='Renewal Reference', required=True, default=lambda self: 'New')
    subscription_id = fields.Many2one('launchly.subscription', string='Subscription', required=True, ondelete='cascade')
    partner_id = fields.Many2one('res.partner', string='Customer', related='subscription_id.partner_id', store=True)
    renewal_date = fields.Date(string='Renewal Date', default=fields.Date.today, required=True)
    previous_start_date = fields.Date(string='Previous Start Date', required=True)
    previous_end_date = fields.Date(string='Previous End Date', required=True)
    new_start_date = fields.Date(string='New Start Date', required=True)
    new_end_date = fields.Date(string='New End Date', required=True)
    notes = fields.Text(string='Notes')
    
    @api.model
    def create(self, vals):
        if vals.get('name', 'New') == 'New':
            sequence = self.env['ir.sequence'].search([('code', '=', 'subscription.renewal.history')], limit=1)
            if sequence:
                vals['name'] = sequence.next_by_id()
                _logger.info(f'Generated renewal reference: {vals["name"]}')
            else:
                _logger.error('Sequence subscription.renewal.history not found!')
                vals['name'] = f'REN{self.env.cr.dsn.split("/")[-1]}_{fields.Datetime.now().strftime("%Y%m%d%H%M%S")}'
        return super().create(vals) 