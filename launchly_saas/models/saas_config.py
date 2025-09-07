from odoo import models, fields, api


class SaasConfig(models.Model):
    _name = 'saas.config'
    _description = 'SaaS Configuration'

    sudo_password = fields.Char()
    http_ip = fields.Char()
    backup_path = fields.Char(string='Backup Path', help='Base directory for instance backups')
    domain = fields.Char(string='Domain', help='Domain for the SaaS instances')
    ssl_email = fields.Char(string='SSL Email', help='Email for SSL certificate registration')
    instance_id = fields.Many2one('odoo.docker.instance', string='Default Instance')
