from odoo import models, api
import secrets
import string
import logging
from odoo.http import request
from datetime import datetime, timedelta

_logger = logging.getLogger(__name__)

class OdooInstance(models.Model):
    _inherit = 'odoo.instance'

    @api.model
    def create_demo_instance_after_delay(self, post, partner_id):
        try:
            partner = self.env['res.partner'].sudo().browse(partner_id)
            plan = self.env['instance.plan'].sudo().browse(int(post.get('plan_id')))
            activation_code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))

            instance_vals = {
                'user_email': post.get('user_email'),
                'user_phone': post.get('user_phone'),
                'country_id': int(post.get('country_id')),
                'company_name': post.get('company_name'),
                'plan_id': plan.id,
                'name': activation_code,
                'includes_subdomain': True,
                'subdomain_name': post.get('subdomain_name'),
                'is_demo': True,
            }

            instance = self.sudo().with_context(skip_template_apply=True).create(instance_vals)

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

            try:
                instance.create_odoo_environment()
                instance.restart_instance()
            except Exception as e:
                _logger.warning('odoo setup failed for demo instance %s: %s', instance.id, str(e))
                instance.write({'state': 'created'})

            # Optional: create CRM lead if crm module is installed
            if 'crm.lead' in self.env:
                try:
                    self.env['crm.lead'].sudo().create({
                        'name': f"Demo Request - {partner.name}",
                        'partner_id': partner.id,
                        'email_from': partner.email,
                        'phone': partner.phone,
                        'description': f"Requested demo for plan {plan.name} with subdomain {post.get('subdomain_name')}.",
                    })
                except Exception as e:
                    _logger.warning("CRM lead creation failed: %s", str(e))

            # ✅ Send custom welcome email using _send_demo_welcome_email
            instance_by_email = self.env['odoo.instance'].sudo().search([
                ('user_email', '=', post.get('user_email')),
                ('is_demo', '=', True)
            ], limit=1)
            if instance_by_email:
                self._send_demo_welcome_email(instance_by_email, partner)

            _logger.info("Demo instance created with ID %s and partner %s", instance.id, partner.name)

        except Exception as e:
            _logger.error("Failed to create demo instance after delay: %s", str(e))

    def _send_demo_welcome_email(self, instance, partner):
        """Send welcome email for demo instance"""
        try:
            subject = f"Welcome to Your Demo Instance - {partner.company_name or 'Demo User'}"
            body = f"""
Dear {partner.company_name or 'Demo User'},

Thank you for creating your demo instance with us!

Your demo instance details:
- Company: {partner.company_name or 'N/A'}
- Login: {instance.user_email}
- Password: {instance.user_phone or 'N/A'}
- Database: {instance.domained_url or 'N/A'}
- Plan: {instance.plan_id.name if instance.plan_id else 'N/A'}

Your demo instance is being set up and will be ready shortly.

You have a 14-day free trial period to explore our platform.

If you have any questions or need support, please don't hesitate to contact us.

Best regards,  
The Launchly Team
            """

            mail_vals = {
                'subject': subject,
                'body_html': body.replace('\n', '<br>'),
                'email_from': request.env.company.email or 'contact@launchlyclub.com',
                'email_to': instance.user_email,
                'auto_delete': True,
            }

            mail = request.env['mail.mail'].sudo().create(mail_vals)
            mail.send()
            _logger.info('Welcome email sent successfully to %s', instance.user_email)

        except Exception as e:
            _logger.error('Error sending demo welcome email: %s', str(e))

    @api.model
    def cron_stop_old_demo_instances(self):
        """Stops demo instances that are older than 14 days."""
        expiration_date = datetime.utcnow() - timedelta(days=14)
        old_demo_instances = self.sudo().search([
            ('is_demo', '=', True),
            ('create_date', '<=', expiration_date),
            ('state', '!=', 'stopped'),
        ])

        for instance in old_demo_instances:
            instance.sudo().write({'state': 'stopped'})
            # Optional: also call any stop logic like stopping odoo containers, etc.
            if hasattr(instance, 'stop_instance'):
                instance.stop_instance()