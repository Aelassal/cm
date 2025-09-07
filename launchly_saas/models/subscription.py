from odoo import models, fields, api, _
from datetime import timedelta
import logging
import secrets
import string
from odoo.exceptions import UserError , ValidationError

_logger = logging.getLogger(__name__)

class Subscription(models.Model):
    _name = 'launchly.subscription'
    _description = 'SaaS Subscription'
    _order = 'create_date desc'

    name = fields.Char(string='Subscription Name', required=True, default=lambda self: 'New')
    partner_id = fields.Many2one('res.partner', string='Customer', required=True, ondelete='cascade')
    partner_email = fields.Char(string='Customer Email', related='partner_id.email', store=True)
    sale_order_id = fields.Many2one('sale.order', string='Sale Order', ondelete='cascade', readonly=True)
    instance_id = fields.Many2one('odoo.instance', string='Instance', ondelete='cascade', readonly=True)
    plan_id = fields.Many2one('instance.plan', string='Plan', required=True, ondelete='cascade')
    allowed_users_count = fields.Integer(string='Allowed Users Count', related='plan_id.allowed_users_count')
    allowed_modules_count = fields.Integer(string='Allowed Modules Count', related='plan_id.allowed_modules_count')
    subdomain = fields.Char(string='Subdomain', store=True)
    subscription_period = fields.Selection([
        ('3_months', '3 Months'),
        ('6_months', '6 Months'),
        ('yearly', 'Yearly')
    ], string='Subscription Period', default='yearly', required=True)

    start_date = fields.Date(string='Start Date', default=fields.Date.today, required=True)
    end_date = fields.Date(string='End Date', compute='_compute_end_date', store=True)

    state = fields.Selection([
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled')
    ], string='Status', default='active', required=True)

    days_remaining = fields.Integer(string='Days Remaining', compute='_compute_days_remaining')
    is_expired = fields.Boolean(string='Is Expired', compute='_compute_is_expired')
    instance_state = fields.Selection(related='instance_id.state', string='Instance State', store=True, readonly=True)

    renewal_history_ids = fields.One2many('subscription.renewal.history', 'subscription_id', string='Renewal History', ondelete='cascade')

    project_id = fields.Many2one('project.project', string='Project', ondelete='cascade', readonly=True)

    @api.depends('start_date', 'subscription_period')
    def _compute_end_date(self):
        for subscription in self:
            if subscription.start_date and subscription.subscription_period:
                days = 90 if subscription.subscription_period == '3_months' else 180 if subscription.subscription_period == '6_months' else 365
                subscription.end_date = subscription.start_date + timedelta(days=days)
            else:
                subscription.end_date = False

    @api.depends('end_date')
    def _compute_days_remaining(self):
        today = fields.Date.today()
        for subscription in self:
            subscription.days_remaining = (subscription.end_date - today).days if subscription.end_date else 0

    @api.depends('end_date')
    def _compute_is_expired(self):
        today = fields.Date.today()
        for subscription in self:
            subscription.is_expired = subscription.end_date and today > subscription.end_date

    @api.model
    def create(self, vals):
        plan_id = vals.get('plan_id')
        partner_id = vals.get('partner_id')

        if plan_id:
            # Check for active subscription with same plan
            active_sub = self.search([
                ('partner_id', '=', partner_id),
                ('plan_id', '=', plan_id),
                ('state', '=', 'active')
            ], limit=1)
            if active_sub:
                raise ValidationError(_("An active subscription already exists for this plan."))

            # Check for non-active subscription with same plan
            inactive_sub = self.search([
                ('partner_id', '=', partner_id),
                ('plan_id', '=', plan_id),
                ('state', '=', 'expired')
            ], limit=1)
            if inactive_sub:
                inactive_sub.action_renew_subscription(notes="Renewed automatically on creation attempt")
                return inactive_sub

        # Auto-generate name
        if vals.get('name', 'New') == 'New':
            vals['name'] = self.env['ir.sequence'].next_by_code('launchly.subscription') or 'New'

        subscription = super().create(vals)

        # Create project linked to subscription
        if partner_id:
            partner = self.env['res.partner'].browse(partner_id)
            project = self.env['project.project'].create({
                'name': partner.name or subscription.name,
                'subscription_id': subscription.id,
            })
            subscription.project_id = project.id

        if subscription.state == 'active':
            subscription._create_odoo_instance()

        return subscription
    def _create_odoo_instance(self):
        """Automatically create a odoo instance for this subscription."""
        self.ensure_one()

        partner = self.partner_id
        plan = self.plan_id
        if not plan:
            raise UserError(_("No plan selected for subscription %s") % self.name)

        activation_code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))
        instance_vals = {
            'user_email': partner.email,
            'user_phone': partner.phone,
            'country_id': partner.country_id.id if partner.country_id else False,
            'company_name': partner.company_name or partner.name,
            'plan_id': plan.id,
            'name': activation_code,
        }

        # If subscription has a subdomain
        if self.subdomain:
            instance_vals.update({
                'includes_subdomain': True,
                'subdomain_name': self.subdomain
            })

        instance = self.env['odoo.instance'].with_context(skip_template_apply=True).create(instance_vals)

        if not instance.http_port:
            instance.http_port = instance._get_available_port()
        if not instance.longpolling_port:
            instance.longpolling_port = instance._get_available_port(int(instance.http_port) + 1)

        template = plan.template_id
        if template:
            instance.write({'template_id': template.id})
            instance.onchange_template_id()

            odoo_var = instance.variable_ids.filtered(lambda r: r.name == '{{ODOO-VERSION}}')
            plan_odoo_var = template.variable_ids.filtered(lambda r: r.name == '{{ODOO-VERSION}}')
            if odoo_var and plan_odoo_var and plan_odoo_var.demo_value:
                odoo_var.demo_value = plan_odoo_var.demo_value

            pg_var = instance.variable_ids.filtered(lambda r: r.name == '{{POSTGRES-VERSION}}')
            plan_pg_var = template.variable_ids.filtered(lambda r: r.name == '{{POSTGRES-VERSION}}')
            if pg_var and plan_pg_var and plan_pg_var.demo_value:
                pg_var.demo_value = plan_pg_var.demo_value

        instance._compute_instance_url()
        instance._compute_config_id()
        instance.invalidate_recordset()
        instance.create_odoo_environment()
        instance.restart_instance()

        self.instance_id = instance.id
        _logger.info('odoo instance %s created for subscription %s', instance.name, self.name)

    def action_cancel_subscription(self):
        self.write({'state': 'cancelled'})

    def action_reactivate_subscription(self):
        self.write({'state': 'active'})

    def action_restart_instance(self):
        for subscription in self:
            if subscription.instance_id:
                try:
                    subscription.instance_id.restart_instance()
                except Exception as e:
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'title': 'Error',
                            'message': f'Failed to restart instance: {str(e)}',
                            'type': 'danger',
                            'sticky': False,
                        }
                    }
        return True

    def action_renew_subscription(self, notes=None):
        """Renew subscription without relying on sale orders."""
        for subscription in self:
            previous_start_date = subscription.start_date
            previous_end_date = subscription.end_date

            subscription.write({
                'start_date': fields.Date.today(),
                'state': 'active',
            })

            # Prepare renewal history
            history_vals = {
                'subscription_id': subscription.id,
                'renewal_date': fields.Date.today(),
                'previous_start_date': previous_start_date,
                'previous_end_date': previous_end_date,
                'new_start_date': subscription.start_date,
                'new_end_date': subscription.end_date,
                'notes': notes or 'Subscription renewed automatically during creation',
            }
            self.env['subscription.renewal.history'].create(history_vals)

            # Restart related instance if any
            subscription.action_restart_instance()

            _logger.info(f"Subscription {subscription.name} renewed automatically.")
        return True

    def action_stop_instance(self):
        for subscription in self:
            if subscription.instance_id:
                try:
                    subscription.instance_id.stop_instance()
                except Exception as e:
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'title': 'Error',
                            'message': f'Failed to stop instance: {str(e)}',
                            'type': 'danger',
                            'sticky': False,
                        }
                    }
        return True

    @api.model
    def _cron_check_expired_subscriptions(self):
        expired_subscriptions = self.search([
            ('state', '=', 'active'),
            ('end_date', '<', fields.Date.today())
        ])
        expired_subscriptions.write({'state': 'expired'})
        for subscription in expired_subscriptions:
            if subscription.instance_id:
                try:
                    subscription.instance_id.stop_instance()
                    _logger.info(f'Stopped instance {subscription.instance_id.name} for expired subscription {subscription.name}')
                except Exception as e:
                    _logger.error(f'Failed to stop instance {subscription.instance_id.name} for expired subscription {subscription.name}: {str(e)}')
        _logger.info(f'Updated {len(expired_subscriptions)} expired subscriptions')
