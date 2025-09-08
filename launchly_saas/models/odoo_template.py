import logging
import os
import re
from functools import reduce

from odoo import models, fields, api, _, Command
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class OdooTemplate(models.Model):
    _name = 'odoo.template'
    _description = 'odoo Compose Template'
    _order = 'sequence asc, id'

    _sql_constraints = [
        ('name_uniq', 'unique (name)', 'The name of the template must be unique !'),
    ]

    def _default_template_odoo_conf(self):
        odoo_conf_content = "[options]\naddons_path =/mnt/extra-addons/ \n"
        odoo_conf_content += "admin_passwd = admin\n"
        odoo_conf_content += "data_dir = /var/lib/odoo\n"
        odoo_conf_content += "logfile = /var/log/odoo/odoo.log\n"
        return odoo_conf_content

    name = fields.Char(string="Name", required=True)
    sequence = fields.Integer(required=True, default=0)
    active = fields.Boolean(default=True)
    
    # Fields for bash script installation (replacing template variables)
    source_path = fields.Char(string='Odoo Source Path',
                             help='Path to the Odoo source code (e.g., /opt/odoo/odoo-18.0)')
    odoo_version = fields.Selection([
        ('14', 'Odoo 14'),
        ('15', 'Odoo 15'),
        ('16', 'Odoo 16'),
        ('17', 'Odoo 17'),
        ('18', 'Odoo 18'),
    ], string='Odoo Version', required=True, default='18',
       help='Odoo version for installation')

    @api.constrains('source_path', 'odoo_version')
    def _check_source_path(self):
        """Validate that the Odoo source path exists and contains odoo-bin"""
        for record in self:
            if record.source_path:
                if not os.path.exists(record.source_path):
                    raise ValidationError(f"Odoo source path does not exist: {record.source_path}")
                if not os.path.exists(os.path.join(record.source_path, 'odoo-bin')):
                    raise ValidationError(f"odoo-bin not found in source path: {record.source_path}")