import logging
import os
import socket
import subprocess
import secrets
import string
from datetime import datetime, timedelta
import time
import stat
import shutil
import pytz
import re
import json
import psycopg2
from psycopg2 import sql
from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError

_logger = logging.getLogger(__name__)


class OdooInstance(models.Model):
    _name = 'odoo.instance'
    _inherit = "odoo.template"
    _description = 'Odoo odoo Instance'

    name = fields.Char(string='Instance Name', required=True)
    active = fields.Boolean(default=True)

    state = fields.Selection(
        [('draft', 'Draft'), ('stopped', 'Stopped'), ('running', 'Running'), ('installing', 'Installing'),
         ('installed', 'Installed'), ('error', 'Error')],
        string='State', default='draft')

    # User Information Fields
    user_email = fields.Char(string='User Email', required=True,
                             help="Email address for the admin user (will be used as login)")
    user_phone = fields.Char(string='Phone Number', required=True,
                             help="Phone number of the user (will be used as login password)")
    company_name = fields.Char(string='Company Name', required=True, help="Company name (used for database name)")
    country_id = fields.Many2one('res.country', string='Country', required=True,
                                 help="User's country (used for localization)")
    user_password = fields.Char(string='Generated Password', readonly=True,
                                help="Fallback password if phone number is not provided")
    admin_password = fields.Char(string='Admin Master Password', readonly=True,
                                 help="Auto-generated admin master password")
    database_name = fields.Char(string='Database Name', compute='_compute_database_name', store=True,
                                help="Auto-generated database name")

    http_port = fields.Char(string='HTTP Port')
    longpolling_port = fields.Char(string='Longpolling Port')
    instance_url = fields.Char(string='Instance URL', compute='_compute_instance_url', store=True)
    custom_addon_line = fields.One2many('custom.addon.line', 'instance_id', string='Custom Addons')
    log = fields.Html(string='Log')
    odoo_logs = fields.Text(string='Odoo Service Logs', readonly=True)
    odoo_conf_content = fields.Text(string='Odoo Configuration File Content',
                                    help="Content of the odoo.conf file")
    addons_path = fields.Char(string='Addons Path', related='template_id.source_path', store=True)
    user_path = fields.Char(string='User Path', compute='_compute_user_path', store=True)
    instance_data_path = fields.Char(string='Instance Data Path', compute='_compute_user_path', store=True)
    template_id = fields.Many2one('odoo.template', string='Template')

    root_sudo_password = fields.Char(string='Root Sudo Password', related='config_id.sudo_password')
    user_done = fields.Boolean(string='User Setup Done', default=False,
                               help="Indicates if user setup script has been successfully executed")
    is_demo = fields.Boolean(string='Create with Demo Data', default=False,
                             help="If enabled, the database will be created with demo data")
    prevent_installing_modules = fields.Boolean(string='Prevent Installing Modules', default=False,
                                                help="If enabled, users (even admin) of this instance cannot install new modules or addons from the UI.")
    db_users = fields.One2many('odoo.db.user', 'instance_id', string='Database Users',
                               help="List of all users in the instance database")
    http_ip = fields.Char(related='config_id.http_ip')
    # Active Users Tracking
    active_users_count = fields.Integer(string='Active Users Count', default=0,
                                        help="Number of users active in the last 10 minutes")
    user_activity_status = fields.Selection([
        ('inactive', 'Inactive'),
        ('active', 'Active')
    ], string='User Activity Status', default='inactive',
        help="Status based on user activity in the last 10 minutes")

    # Subdomain Fields
    includes_subdomain = fields.Boolean(string='Include Subdomain', default=False,
                                        help="Enable custom subdomain for this instance")
    subdomain_name = fields.Char(string='Subdomain Name',
                                 help="Subdomain name (e.g., 'mycompany' for mycompany.launchlyclub.com)")
    domained_url = fields.Char(string='Domain URL', compute='_compute_domained_url', store=True,
                               help="HTTPS URL with custom domain")
    nginx_config_path = fields.Char(string='Nginx Config Path', compute='_compute_nginx_config_path', store=True,
                                    help="Path to the Nginx configuration file")
    ssl_certificate_expiry = fields.Datetime(string='SSL Certificate Expiry', compute='_compute_ssl_certificate_expiry',
                                             help="SSL certificate expiration date")
    config_id = fields.Many2one(
        'saas.config',
        string='Configuration',
        compute='_compute_config_id',
        store=True
    )
    addon_line = fields.One2many('odoo.addon.line', 'instance_id', string='All Addons')
    allowed_users_count = fields.Integer()
    allowed_modules_count = fields.Integer()
    plan_id = fields.Many2one('instance.plan', string='Plan', ondelete='set null',
                              help='Plan template to use for this instance')
    odoo_addon_line_ids = fields.Many2many(
        'odoo.addon.line',
        'instance_odoo_addon_rel',  # relation table
        'instance_id',  # link to odoo.instance
        'odoo_addon_id',  # link to odoo.addon.line
        string='Odoo Addons',
        help='Select Odoo addons included in this instance.',
        domain=[('application', '=', True), ('license', '=', 'LGPL-3')]
    )
    # custom_addon_line_ids = fields.Many2many(
    #     'custom.addon.line',
    #     'instance_odoo_custom_addon_rel',  # relation table
    #     'instance_id',  # link to odoo.instance
    #     'custom_addon_id',  # link to odoo.addon.line
    #     string='Custom Addons',
    #     help='Select Custom addons included in this instance.',
    #
    # )
    storage_usage = fields.Char(string="Storage Usage", readonly=True,
                                help="Storage space used by instance (e.g., 1.2GB / 10GB)")
    cpu_usage_percent = fields.Float(string="CPU Usage (%)", readonly=True,
                                     help="Total CPU usage percent of instance service")
    memory_usage_percent = fields.Float(string="Memory Usage (%)", readonly=True,
                                        help="Total memory usage percent of instance service")
    memory_usage = fields.Char(string="Memory Usage (used/total)", readonly=True,
                               help="Memory usage in bytes, e.g. 50MiB / 2GiB")
    net_io = fields.Char(string="Network I/O", readonly=True, help="Network I/O usage")
    block_io = fields.Char(string="Block I/O", readonly=True, help="Block I/O usage")
    pids_count = fields.Integer(string="PIDs", readonly=True, help="Number of PIDs used by instance service")

    # Helper computed fields for progress bars (0-100)
    cpu_usage_bar = fields.Integer(string="CPU Usage Bar (%)", compute="_compute_cpu_usage_bar", store=False)
    memory_usage_bar = fields.Integer(string="Memory Usage Bar (%)", compute="_compute_memory_usage_bar", store=False)

    @api.onchange('plan_id')
    def _onchange_plan_id(self):
        """Ensure plan's addons are included in odoo_addon_line_ids."""
        if self.plan_id:
            # Add plan's addons + existing selected addons
            all_addons = self.odoo_addon_line_ids | self.plan_id.odoo_addon_line_ids
            self.odoo_addon_line_ids = all_addons

    @api.depends('cpu_usage_percent')
    def _compute_cpu_usage_bar(self):
        for rec in self:
            rec.cpu_usage_bar = min(100, max(0, int(rec.cpu_usage_percent)))

    @api.depends('memory_usage_percent')
    def _compute_memory_usage_bar(self):
        for rec in self:
            rec.memory_usage_bar = min(100, max(0, int(rec.memory_usage_percent)))

    def _parse_size_to_bytes(self, size_str):
        """Convert size string like '39 MB' or '29M' to bytes."""
        if not size_str or size_str in ['N/A', '0B']:
            return 0

        size_str = size_str.strip().upper()
        match = re.match(r'^(\d+(?:\.\d+)?)\s*([KMGTPE]?B?)$', size_str)
        if not match:
            return 0

        number = float(match.group(1))
        unit = match.group(2)

        multipliers = {
            'B': 1, 'K': 1024, 'KB': 1024, 'M': 1024 ** 2, 'MB': 1024 ** 2,
            'G': 1024 ** 3, 'GB': 1024 ** 3, 'T': 1024 ** 4, 'TB': 1024 ** 4,
            'P': 1024 ** 5, 'PB': 1024 ** 5, 'E': 1024 ** 6, 'EB': 1024 ** 6,
        }
        return int(number * multipliers.get(unit, 1))

    def _bytes_to_human_readable(self, bytes_size):
        """Convert bytes to human readable format."""
        if bytes_size == 0:
            return "0B"

        units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
        unit_index = 0
        size = float(bytes_size)

        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1

        return f"{int(size)}{units[unit_index]}" if size == int(size) else f"{size:.1f}{units[unit_index]}"

    def _get_filestore_size(self):
        """Get the size of the filestore directory."""
        try:
            filestore_path = f'/opt/{self.name}/data/filestore/{self.database_name}'

            # Check if directory exists
            check_result = self.excute_command_with_sudo(f"test -d {filestore_path}", check=False)
            if check_result.returncode != 0:
                return "0B"

            # Get directory size
            result = self.excute_command_with_sudo(f"du -sh {filestore_path}", check=False)
            if result.returncode == 0 and result.stdout:
                return result.stdout.split('\t')[0].strip()
            return "0B"
        except Exception:
            return "0B"

    def _calculate_total_storage_size(self, db_size, filestore_size):
        """Calculate the total storage size by converting both sizes to bytes and adding them."""
        try:
            db_bytes = self._parse_size_to_bytes(db_size)
            filestore_bytes = self._parse_size_to_bytes(filestore_size)
            total_bytes = db_bytes + filestore_bytes
            return self._bytes_to_human_readable(total_bytes)
        except Exception:
            return f"{db_size} + {filestore_size}"

    def _get_db_size(self):
        """Fetch database size and filestore size, return combined information."""
        try:
            db_name = self.database_name or self.name
            if not db_name:
                return "N/A"

            # Check if database exists
            check_cmd = f"-u postgres psql -d postgres -t -c \"SELECT 1 FROM pg_database WHERE datname = '{db_name}';\""
            check_result = self.excute_command_with_sudo(check_cmd, check=False)

            if check_result.returncode != 0 or not check_result.stdout.strip():
                filestore_size = self._get_filestore_size()
                total_size = self._calculate_total_storage_size("0B", filestore_size)
                return f"{total_size} (0B + {filestore_size} filestore)"

            # Get database size
            query_cmd = f"-u postgres psql -d postgres -t -c \"SELECT pg_size_pretty(pg_database_size('{db_name}'));\""
            db_result = self.excute_command_with_sudo(query_cmd, check=False)

            if db_result.returncode != 0:
                return "N/A"

            db_size = db_result.stdout.strip() or "0B"
            filestore_size = self._get_filestore_size()
            total_size = self._calculate_total_storage_size(db_size, filestore_size)

            return f"{total_size} ({db_size} + {filestore_size} filestore)"

        except Exception:
            return "N/A"

    def get_instance_resource_usage(self):
        self.ensure_one()
        usage = {
            "cpu": 0.0, "mem_percent": 0.0, "mem_usage": "", "net_io": "N/A (systemd)",
            "block_io": "N/A (systemd)", "pids": 0, "services_count": 1, "storage_usage": ""
        }

        try:
            service_name = f"{self.name}.service"

            # Check if service is active
            status_result = subprocess.run(["sudo", "systemctl", "is-active", service_name],
                                           capture_output=True, text=True)

            if status_result.returncode != 0 or status_result.stdout.strip() != "active":
                usage["storage_usage"] = self._get_db_size()
                return usage

            # Get main PID
            pid_result = subprocess.run(["sudo", "systemctl", "show", service_name,
                                         "--property=MainPID", "--value"],
                                        capture_output=True, text=True)
            main_pid = pid_result.stdout.strip()

            if not main_pid or main_pid == "0":
                usage["storage_usage"] = self._get_db_size()
                return usage

            # Get main process stats
            ps_result = subprocess.run(["ps", "--no-headers", "-o", "pid,ppid,%cpu,%mem,vsz,rss", "-p", main_pid],
                                       capture_output=True, text=True)

            if ps_result.stdout.strip():
                parts = ps_result.stdout.strip().split()
                if len(parts) >= 6:
                    usage["cpu"] = round(float(parts[2]), 2)
                    usage["mem_percent"] = round(float(parts[3]), 2)
                    usage["mem_usage"] = f"{int(parts[5]) // 1024}MiB / {int(parts[4]) // 1024}MiB"
                    usage["pids"] = 1

            # Get child processes stats
            children_result = subprocess.run(["ps", "--no-headers", "-o", "pid,%cpu,%mem", "--ppid", main_pid],
                                             capture_output=True, text=True)

            if children_result.returncode == 0 and children_result.stdout.strip():
                for line in children_result.stdout.strip().split('\n'):
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        usage["cpu"] += round(float(parts[1]), 2)
                        usage["mem_percent"] += round(float(parts[2]), 2)
                        usage["pids"] += 1

            usage["storage_usage"] = self._get_db_size()

        except Exception:
            usage["storage_usage"] = self._get_db_size()

        return usage

    def update_resource_fields(self):
        for instance in self:
            usage = instance.get_instance_resource_usage()
            instance.cpu_usage_percent = usage["cpu"]
            instance.memory_usage_percent = usage["mem_percent"]
            instance.memory_usage = usage["mem_usage"]
            instance.net_io = usage["net_io"]
            instance.block_io = usage["block_io"]
            instance.pids_count = usage["pids"]
            instance.storage_usage = usage["storage_usage"]
            instance._compute_cpu_usage_bar()
            instance._compute_memory_usage_bar()

    @api.constrains('odoo_addon_line_ids', 'allowed_modules_count')
    def _check_allowed_modules_count(self):
        for record in self:
            if len(record.odoo_addon_line_ids) > record.allowed_modules_count:
                raise ValidationError(
                    f"You can select up to {record.allowed_modules_count} modules only. "
                    f"You selected {len(record.odoo_addon_line_ids)}."
                )

    @api.depends('instance_url')
    def _compute_config_id(self):
        for record in self:
            # Example logic:
            # If instance_url is set, assign first saas.config record, else clear config_id
            if record.instance_url:
                record.config_id = self.env['saas.config'].search([], limit=1)
            else:
                record.config_id = False

    @api.onchange('name')
    def onchange_name(self):
        self.http_port = self._get_available_port()
        self.longpolling_port = self._get_available_port(int(self.http_port) + 1)

    @api.depends('name')
    def _compute_user_path(self):
        for instance in self:
            if not instance.name:
                continue
            # sanitise the instance name for paths
            safe_name = instance.name.replace('.', '_').replace(' ', '_').lower()

            # base path is /opt/<instance_name>
            instance.user_path = os.path.join('/opt', safe_name)

            # data path is /opt/<instance_name>/data
            instance.instance_data_path = os.path.join(instance.user_path, 'data')

    def add_to_log(self, message):
        """Agrega un mensaje al registro (log) y lo limpia si supera 1000 caracteres."""
        # Log to Odoo backend for debugging
        _logger.info(f"[LAUNCHLY_SAAS - {self.name}] {message}")

        now = datetime.now()
        new_log = "</br> \n#" + str(now.strftime("%m/%d/%Y, %H:%M:%S")) + " " + str(message) + " " + str(self.log)
        if len(new_log) > 10000:
            new_log = "</br>" + str(now.strftime("%m/%d/%Y, %H:%M:%S")) + " " + str(message)
        self.log = new_log

    def clear_log(self):
        """Clear the log field for selected instances"""
        for instance in self:
            instance.log = ""
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Log cleared by user")
        # return {
        #     'type': 'ir.actions.client',
        #     'tag': 'reload',
        # }

    def refresh_odoo_logs(self):
        """Refresh systemd service logs from journalctl and log files"""
        for instance in self:
            try:
                service_name = f"{instance.name}.service"
                logs = ""

                # Get systemd service logs using journalctl
                journal_cmd = ["sudo", "journalctl", "-u", service_name, "--lines=100", "--no-pager"]
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Fetching systemd logs: {' '.join(journal_cmd)}")

                journal_result = subprocess.run(journal_cmd, capture_output=True, text=True, timeout=30)

                if journal_result.returncode == 0:
                    if journal_result.stdout:
                        logs += "=== SYSTEMD SERVICE LOGS ===\n" + journal_result.stdout + "\n"
                    if journal_result.stderr:
                        logs += "=== SYSTEMD STDERR ===\n" + journal_result.stderr + "\n"
                else:
                    logs += f"Error fetching systemd logs: {journal_result.stderr}\n"

                # Try to get Odoo log file from the systemd log location
                log_file_path = f"/var/log/{instance.name}.log"
                try:
                    if os.path.exists(log_file_path):
                        _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Reading Odoo log file: {log_file_path}")
                        # Read last 200 lines of the log file
                        with open(log_file_path, 'r') as f:
                            file_lines = f.readlines()
                            last_lines = file_lines[-200:] if len(file_lines) > 200 else file_lines
                            odoo_logs = ''.join(last_lines)
                            logs += "\n=== ODOO LOG FILE ===\n" + odoo_logs
                    else:
                        logs += f"\n=== ODOO LOG FILE ===\nLog file not found at: {log_file_path}"
                except Exception as log_error:
                    logs += f"\n=== ODOO LOG FILE ===\nError reading log file: {str(log_error)}"

                instance.odoo_logs = logs if logs else "No logs available"
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Systemd logs refreshed successfully")

            except subprocess.TimeoutExpired:
                instance.odoo_logs = "Timeout while fetching systemd logs"
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Timeout while fetching systemd logs")
            except Exception as e:
                instance.odoo_logs = f"Error fetching systemd logs: {str(e)}"
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error fetching systemd logs: {str(e)}")

        # return {
        #     'type': 'ir.actions.client',
        #     'tag': 'reload',
        # }

    def clear_odoo_logs(self):
        """Clear the odoo_logs field for selected instances"""
        for instance in self:
            instance.odoo_logs = ""
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Service logs cleared by user")
        # return {
        #     'type': 'ir.actions.client',
        #     'tag': 'reload',
        # }

    def load_odoo_conf(self):
        """Load the odoo.conf file content from the system location (bash script approach)"""
        for instance in self:
            try:
                # With bash script approach, config is stored at /etc/{instance_name}.conf
                conf_file_path = f"/etc/{instance.name}.conf"
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Loading Odoo configuration from: {conf_file_path}")

                if os.path.exists(conf_file_path):
                    # Read the system config file (requires sudo)
                    if instance.root_sudo_password:
                        # Use the sudo password method
                        result = instance.excute_command_with_sudo(f"cat {conf_file_path}")
                        if result.returncode == 0:
                            instance.odoo_conf_content = result.stdout
                            instance.add_to_log(f"[INFO] Odoo configuration loaded successfully from {conf_file_path}")
                            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Odoo configuration loaded successfully")
                        else:
                            raise Exception(f"Failed to read config file: {result.stderr}")
                    else:
                        # Try to read without sudo first (might work if file permissions allow)
                        try:
                            with open(conf_file_path, 'r', encoding='utf-8') as f:
                                instance.odoo_conf_content = f.read()
                            instance.add_to_log(f"[INFO] Odoo configuration loaded successfully from {conf_file_path}")
                            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Odoo configuration loaded successfully")
                        except PermissionError:
                            instance.odoo_conf_content = "# Configuration file exists but requires sudo access\n# Please provide root_sudo_password to load configuration"
                            instance.add_to_log(f"[WARNING] Configuration file requires sudo access: {conf_file_path}")
                            _logger.warning(
                                f"[LAUNCHLY_SAAS - {instance.name}] Configuration file requires sudo access")
                else:
                    instance.odoo_conf_content = "# Configuration file not found\n# Please create the instance first"
                    instance.add_to_log(f"[WARNING] Configuration file not found at: {conf_file_path}")
                    _logger.warning(f"[LAUNCHLY_SAAS - {instance.name}] Configuration file not found: {conf_file_path}")

            except Exception as e:
                error_msg = f"Error loading configuration file: {str(e)}"
                instance.odoo_conf_content = f"# Error loading configuration file\n# {error_msg}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] {error_msg}")

        # return {
        #     'type': 'ir.actions.client',
        #     'tag': 'reload',
        # }

    def save_odoo_conf(self):
        """Save the odoo.conf file content to the system location (bash script approach)"""
        for instance in self:
            try:
                # With bash script approach, config is stored at /etc/{instance_name}.conf
                conf_file_path = f"/etc/{instance.name}.conf"
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Saving Odoo configuration to: {conf_file_path}")

                if instance.root_sudo_password:
                    # Create a temporary file with the new content
                    import tempfile
                    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.conf') as temp_file:
                        temp_file.write(instance.odoo_conf_content or "")
                        temp_file_path = temp_file.name

                    # Copy the temporary file to the system location with sudo
                    copy_result = instance.excute_command_with_sudo(f"cp {temp_file_path} {conf_file_path}")

                    # Clean up temporary file
                    os.unlink(temp_file_path)

                    if copy_result.returncode == 0:
                        # Set proper ownership and permissions
                        instance.excute_command_with_sudo(f"chown {instance.name}:{instance.name} {conf_file_path}")
                        instance.excute_command_with_sudo(f"chmod 640 {conf_file_path}")

                        instance.add_to_log(f"[INFO] Odoo configuration saved successfully to {conf_file_path}")
                        _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Odoo configuration saved successfully")

                        # If the instance is running, restart the service to apply changes
                        if instance.state == 'running':
                            instance.add_to_log(
                                "[INFO] Configuration saved. Restarting Odoo service to apply changes...")
                            try:
                                restart_result = instance.excute_command_with_sudo(
                                    f"systemctl restart {instance.name}.service")
                                if restart_result.returncode == 0:
                                    instance.add_to_log("[INFO] Odoo service restarted successfully")
                                else:
                                    instance.add_to_log(f"[WARNING] Failed to restart service: {restart_result.stderr}")
                            except Exception as restart_error:
                                instance.add_to_log(f"[WARNING] Error restarting service: {str(restart_error)}")
                    else:
                        raise Exception(f"Failed to save config file: {copy_result.stderr}")
                else:
                    raise Exception("root_sudo_password is required to save configuration file")

            except Exception as e:
                error_msg = f"Error saving configuration file: {str(e)}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] {error_msg}")

        # return {
        #     'type': 'ir.actions.client',
        #     'tag': 'reload',
        # }

    @api.depends('http_port', 'http_ip')
    def _compute_instance_url(self):
        http_ip = self.env['saas.config'].search([], limit=1).http_ip
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        base_url = base_url.split(':')
        base_url = base_url[0] + ':' + base_url[1] + ':'
        for instance in self:
            if not instance.http_port:
                continue
            # Use http_ip if it's set, otherwise use the default base_url
            ip_base = f"http://{http_ip}:" if http_ip else base_url
            instance.instance_url = f"{ip_base}{instance.http_port}"

    def open_instance_url(self):
        for instance in self:
            if instance.http_port:
                url = instance.instance_url
                return {
                    'type': 'ir.actions.act_url',
                    'url': url,
                    'target': 'new',
                }

    def _get_available_port(self, start_port=8069, end_port=9000):
        # Define el rango de puertos en el que deseas buscar disponibles
        # buscar todos los puertos de las instancias
        instances = self.env['odoo.instance'].search([])
        # crear una lista con los puertos de las instancias
        ports = []
        for instance in instances:
            ports.append(int(instance.http_port))
            ports.append(int(instance.longpolling_port))

        for port in range(start_port, end_port + 1):
            # Si el puerto ya está en uso, continúa con el siguiente
            if port in ports:
                continue
            # Intenta crear un socket en el puerto
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)  # Establece un tiempo de espera para la conexión

            try:
                # Intenta vincular el socket al puerto
                sock.bind(("0.0.0.0", port))
                return port  # Si tiene éxito, el puerto está disponible
            except Exception as e:
                # Si no tiene éxito, el puerto ya está en uso
                pass
            finally:
                sock.close()
        self.add_to_log("[ERROR] No se encontraron puertos disponibles en el rango especificado.")

    def _create_host_directories(self):
        """Create host directories with proper permissions for Odoo service using sudo"""
        for instance in self:
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Creating host directories with sudo permissions")

            # Create required directories
            directories = [
                os.path.join(instance.instance_data_path, "data"),
                os.path.join(instance.instance_data_path, "logs"),
                os.path.join(instance.instance_data_path, "postgresql"),
                os.path.join(instance.instance_data_path, "addons"),
                os.path.join(instance.instance_data_path, "addons", "custom"),  # Add custom addons directory
                os.path.join(instance.instance_data_path, "etc"),
                os.path.join(instance.instance_data_path, "odoo_init"),
                os.path.join(instance.instance_data_path, "postgresql_init")
            ]

            for directory in directories:
                instance._makedirs(directory)
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Created directory: {directory}")

            # Set proper permissions using sudo if password is provided
            if instance.root_sudo_password:
                try:
                    data_dir = os.path.join(instance.instance_data_path, "data")
                    logs_dir = os.path.join(instance.instance_data_path, "logs")

                    # Use sudo with password to set proper ownership and permissions
                    commands = [
                        f"sudo -S chown -R 101:101 {data_dir}",
                        f"sudo -S chown -R 101:101 {logs_dir}",
                        f"sudo -S chmod -R 755 {data_dir}",
                        f"sudo -S chmod -R 755 {logs_dir}"
                    ]

                    for cmd in commands:
                        _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Executing: {cmd}")
                        result = subprocess.run(
                            cmd,
                            shell=True,
                            input=instance.root_sudo_password + '\n',
                            text=True,
                            capture_output=True,
                            timeout=30
                        )

                        if result.returncode != 0:
                            _logger.warning(
                                f"[LAUNCHLY_SAAS - {instance.name}] Command failed: {cmd}, Error: {result.stderr}")
                        else:
                            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Command successful: {cmd}")

                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Set proper permissions using sudo")
                    instance.add_to_log("[INFO] Host directories created with proper sudo permissions")

                except Exception as e:
                    _logger.warning(f"[LAUNCHLY_SAAS - {instance.name}] Could not set sudo permissions: {str(e)}")
                    instance.add_to_log(f"[WARNING] Could not set sudo permissions: {str(e)}")
                    instance.add_to_log("[INFO] odoo will handle internal permissions")
            else:
                # Fallback without sudo
                try:
                    data_dir = os.path.join(instance.instance_data_path, "data")
                    logs_dir = os.path.join(instance.instance_data_path, "logs")

                    os.chmod(data_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH)  # 755
                    os.chmod(logs_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH)  # 755

                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Set basic permissions for directories")
                    instance.add_to_log("[INFO] Host directories created with basic permissions")

                except Exception as e:
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Could not set host permissions: {str(e)}")
                    instance.add_to_log("[INFO] Host directories created - odoo will handle internal permissions")

            instance.add_to_log("[INFO] Directory permissions configured for odoo service")

    def _setup_custom_addons(self):
        """Setup custom addons for the instance"""
        for instance in self:
            if not instance.custom_addon_line:
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] No custom addons to setup")
                return

            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Setting up custom addons")
            instance.add_to_log(f"[INFO] Setting up {len(instance.custom_addon_line)} custom addon(s)")

            custom_addons_dir = os.path.join(instance.instance_data_path, "addons", "custom")
            instance._makedirs(custom_addons_dir)

            for addon_line in instance.custom_addon_line:
                try:
                    if addon_line.addon_file and addon_line.addon_filename:
                        # Re-extract addon if needed
                        if not addon_line.is_extracted or not os.path.exists(addon_line.addon_path or ''):
                            _logger.info(
                                f"[LAUNCHLY_SAAS - {instance.name}] Extracting custom addon: {addon_line.addon_name}")
                            instance.add_to_log(f"[INFO] Extracting custom addon: {addon_line.addon_name}")
                            addon_line._extract_addon_file()
                        else:
                            _logger.info(
                                f"[LAUNCHLY_SAAS - {instance.name}] Custom addon already extracted: {addon_line.addon_name}")
                            instance.add_to_log(f"[INFO] Custom addon already extracted: {addon_line.addon_name}")

                except Exception as e:
                    _logger.error(
                        f"[LAUNCHLY_SAAS - {instance.name}] Error setting up custom addon {addon_line.addon_name}: {str(e)}")
                    instance.add_to_log(f"[ERROR] Error setting up custom addon {addon_line.addon_name}: {str(e)}")

            instance.add_to_log(f"[INFO] Custom addons setup completed")

    def clean_custom_addons(self):
        """Clean up custom addons files for selected instances"""
        for instance in self:
            try:
                custom_addons_dir = os.path.join(instance.instance_data_path, "addons", "custom")
                if os.path.exists(custom_addons_dir):
                    # Try normal removal first
                    try:
                        shutil.rmtree(custom_addons_dir)
                        _logger.info(
                            f"[LAUNCHLY_SAAS - {instance.name}] Removed custom addons directory: {custom_addons_dir}")
                        instance.add_to_log("[INFO] Custom addons directory cleaned")
                    except PermissionError as pe:
                        # Permission denied - try with sudo if password is available
                        if instance.root_sudo_password:
                            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Permission denied, trying with sudo...")
                            instance.add_to_log("[INFO] Permission denied, using sudo to clean custom addons...")

                            try:
                                # Use sudo to change ownership and then remove
                                sudo_commands = [
                                    f"sudo -S chown -R {os.getuid()}:{os.getgid()} {custom_addons_dir}",
                                    f"sudo -S chmod -R 755 {custom_addons_dir}",
                                    f"sudo -S rm -rf {custom_addons_dir}"
                                ]

                                for cmd in sudo_commands:
                                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Executing: {cmd}")
                                    result = subprocess.run(
                                        cmd,
                                        shell=True,
                                        input=instance.root_sudo_password + '\n',
                                        text=True,
                                        capture_output=True,
                                        timeout=30
                                    )

                                    if result.returncode != 0:
                                        _logger.warning(
                                            f"[LAUNCHLY_SAAS - {instance.name}] Sudo command failed: {cmd}, Error: {result.stderr}")
                                        raise Exception(f"Sudo command failed: {result.stderr}")
                                    else:
                                        _logger.info(
                                            f"[LAUNCHLY_SAAS - {instance.name}] Sudo command successful: {cmd}")

                                _logger.info(
                                    f"[LAUNCHLY_SAAS - {instance.name}] Custom addons directory removed using sudo")
                                instance.add_to_log("[SUCCESS] Custom addons directory cleaned using sudo")

                            except Exception as sudo_error:
                                _logger.error(
                                    f"[LAUNCHLY_SAAS - {instance.name}] Sudo removal failed: {str(sudo_error)}")
                                instance.add_to_log(
                                    f"[ERROR] Failed to clean custom addons with sudo: {str(sudo_error)}")
                                instance.add_to_log(
                                    "[INFO] You may need to manually remove files with root permissions")
                                raise sudo_error
                        else:
                            # No sudo password available
                            _logger.error(
                                f"[LAUNCHLY_SAAS - {instance.name}] Permission denied and no sudo password available")
                            instance.add_to_log(
                                "[ERROR] Permission denied: Cannot remove files created by odoo")
                            instance.add_to_log(
                                "[INFO] Please provide sudo password in instance settings or manually remove the files")
                            instance.add_to_log(f"[INFO] Manual command: sudo rm -rf {custom_addons_dir}")
                            raise pe

                # Reset extraction status for all custom addons
                for addon_line in instance.custom_addon_line:
                    addon_line.write({
                        'is_extracted': False,
                        'addon_path': False,
                        'state': 'draft'
                    })

                instance.add_to_log("[INFO] Custom addons cleaned and reset")

            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error cleaning custom addons: {str(e)}")
                instance.add_to_log(f"[ERROR] Error cleaning custom addons: {str(e)}")
                # Don't re-raise the exception to allow partial cleanup

    def _process_pending_custom_addons(self):
        """Process any custom addons that were added before instance creation"""
        for instance in self:
            pending_addons = instance.custom_addon_line.filtered(lambda a: a.state == 'draft')
            if pending_addons:
                instance.add_to_log(f"[INFO] Processing {len(pending_addons)} pending custom addons...")
                for addon in pending_addons:
                    try:
                        addon._process_addon()
                        instance.add_to_log(f"[SUCCESS] Processed custom addon: {addon.addon_name}")
                    except Exception as e:
                        instance.add_to_log(f"[ERROR] Failed to process custom addon {addon.addon_name}: {str(e)}")
                        _logger.error(
                            f"[LAUNCHLY_SAAS - {instance.name}] Failed to process custom addon {addon.addon_name}: {str(e)}")
            else:
                instance.add_to_log("[INFO] No pending custom addons to process")

    def apply_custom_addons_changes(self):
        """Apply custom addon changes to running or stopped instances"""
        for instance in self:
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Applying custom addon changes")
            instance.add_to_log("[INFO] Applying custom addon changes...")

            if instance.state == 'draft':
                instance.add_to_log("[WARNING] Instance is in draft state. Please create odoo environment first.")
                continue

            try:
                # Setup/update custom addons
                instance._setup_custom_addons()

                # Update odoo.conf with new addons path
                instance._update_odoo_conf_addons_path()

                # If instance is running, restart only the Odoo service to apply changes
                if instance.state == 'running':
                    instance.add_to_log("[INFO] Restarting Odoo service to apply addon changes...")
                    instance.restart_odoo_service()
                else:
                    instance.add_to_log("[INFO] Custom addon changes applied. Start the instance to use new addons.")

                instance.add_to_log("[SUCCESS] Custom addon changes applied successfully!")

            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error applying custom addon changes: {str(e)}")
                instance.add_to_log(f"[ERROR] Error applying custom addon changes: {str(e)}")

    def restart_odoo_service(self):
        """Restart the systemd Odoo service"""
        for instance in self:
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Restarting systemd Odoo service")
            instance.add_to_log("[INFO] Restarting Odoo service to load new addons...")

            try:
                # Restart the systemd service directly
                cmd = ['sudo', '-S', 'systemctl', 'restart', f'{instance.name}']

                result = subprocess.run(
                    cmd,
                    input=f"{instance.root_sudo_password}\n",
                    capture_output=True,
                    text=True,
                    check=True
                )

                instance.add_to_log("[INFO] Odoo service restarted successfully")
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Service restarted successfully")

            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to restart service: {e.stderr}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] {error_msg}")
            except Exception as e:
                error_msg = f"Failed to restart service: {str(e)}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] {error_msg}")

    def start_odoo_service(self):
        """Start the systemd Odoo service"""
        for instance in self:
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Starting systemd Odoo service")
            instance.add_to_log("[INFO] Starting Odoo service...")

            try:
                # Start the systemd service
                cmd = ['sudo', '-S', 'systemctl', 'start', f'{instance.name}']

                result = subprocess.run(
                    cmd,
                    input=f"{instance.root_sudo_password}\n",
                    capture_output=True,
                    text=True,
                    check=True
                )

                instance.add_to_log("[INFO] Odoo service started successfully")
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Service started successfully")

            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to start service: {e.stderr}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] {error_msg}")
            except Exception as e:
                error_msg = f"Failed to start service: {str(e)}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] {error_msg}")

    def stop_odoo_service(self):
        """Stop the systemd Odoo service"""
        for instance in self:
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Stopping systemd Odoo service")
            instance.add_to_log("[INFO] Stopping Odoo service...")

            try:
                # Stop the systemd service
                cmd = ['sudo', '-S', 'systemctl', 'stop', f'{instance.name}']

                result = subprocess.run(
                    cmd,
                    input=f"{instance.root_sudo_password}\n",
                    capture_output=True,
                    text=True,
                    check=True
                )

                instance.add_to_log("[INFO] Odoo service stopped successfully")
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Service stopped successfully")

            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to stop service: {e.stderr}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] {error_msg}")
            except Exception as e:
                error_msg = f"Failed to stop service: {str(e)}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] {error_msg}")

    def update_addons_list(self):
        """Update the addons list in the running Odoo instance using superuser script."""
        for instance in self:
            if instance.state != 'running':
                instance.add_to_log("[ERROR] Instance must be running to update addons list")
                return False

            try:
                import os
                script_content = f'''#!/usr/bin/env python3
import os
import sys

# Add Odoo source path to Python path (from the source installation)
odoo_source_path = '{instance._get_source_path_from_template()}'
sys.path.insert(0, odoo_source_path)

import odoo
from odoo import api, SUPERUSER_ID
odoo.tools.config.parse_config(['-c', '/etc/{instance.name}.conf'])
try:
    from odoo.modules.registry import Registry
    registry = Registry.new('{instance.database_name}')
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {{}})
        # Update the module list
        env['ir.module.module'].update_list()
        cr.commit()
        print("SUCCESS|Addons list updated successfully")
except Exception as e:
    print("ERROR|{{}}".format(str(e)))
    '''
                script_path = os.path.join(instance.instance_data_path, "odoo_init", "update_addons.py")
                instance.create_file(script_path, script_content)
                instance.chmod_with_sudo(script_path, 0o755)
                # Execute script directly using instance's Python environment
                venv_python = f"/opt/{instance.name}/venv/bin/python3"
                cmd = f"{venv_python} {script_path}"

                if instance.root_sudo_password:
                    result = instance.excute_command_with_sudo(cmd, shell=True, check=False)
                    instance.restart_odoo_service()
                else:
                    result = instance.excute_command(cmd, shell=True, check=False)

                if result.returncode == 0:
                    output = result.stdout.strip().splitlines()
                    if not output:
                        instance.add_to_log("[ERROR] No output returned from script")
                        return False

                    for line in output:
                        if line.startswith("SUCCESS|"):
                            instance.add_to_log(f"[SUCCESS] {line.split('|', 1)[1]}")
                            instance.add_to_log("[INFO] New addons should now be visible in the Apps menu")

                            # Recompute installation status for all custom addon lines
                            for addon_line in instance.custom_addon_line:
                                addon_line._compute_is_installed()

                            # Clean up
                            if os.path.exists(script_path):
                                os.remove(script_path)
                            return True
                        elif line.startswith("ERROR|"):
                            instance.add_to_log(f"[ERROR] Script error: {line.split('|', 1)[1]}")
                            return False

                    instance.add_to_log("[ERROR] Unexpected script output")
                    return False
                else:
                    instance.add_to_log(f"[ERROR] Script failed: {result.stderr}")
                    return False

            except Exception as e:
                instance.add_to_log(f"[ERROR] Error updating addons list: {str(e)}")
                instance.add_to_log("[INFO] Try restarting Odoo service or the entire instance")
                return False

    def _analyze_installation_error(self, return_code, stdout_lines, stderr_lines):
        """Analyze common error patterns and provide helpful messages"""
        all_output = "\n".join(stdout_lines + stderr_lines).lower()

        error_patterns = [
            ('permission denied', 'Permission denied - check sudo password and user permissions'),
            ('command not found', 'Required command not found - check system dependencies'),
            ('no such file or directory', 'File or directory not found - check paths'),
            ('address already in use', f'Port {self.http_port} is already in use'),
            ('database.*already exists', 'Database already exists'),
            ('connection refused', 'Database connection failed - check credentials'),
            ('authentication failed', 'Authentication failed - check database credentials'),
            ('sudo.*password', 'Sudo password incorrect or required'),
            ('timeout', 'Operation timed out - check network or system load'),
            ('disk space', 'Insufficient disk space'),
            ('memory', 'Insufficient memory'),
        ]

        for pattern, message in error_patterns:
            if pattern in all_output:
                return message

        # Check last few error lines for clues
        last_errors = stderr_lines[-5:] if stderr_lines else stdout_lines[-5:]
        if last_errors:
            return f"Last error: {' | '.join(last_errors)}"

        return "Unknown error - check full log for details"

    def create_odoo_environment(self):
        """Create the complete Odoo environment using bash script installation"""
        for instance in self:
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Creating Odoo environment")
            instance.add_to_log("[INFO] Creating Odoo Environment")

            # Ensure passwords are generated if they don't exist
            vals_to_update = {}
            if not instance.user_password:
                vals_to_update['user_password'] = instance._generate_random_password()
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Generated user password")
            if not instance.admin_password:
                vals_to_update['admin_password'] = instance._generate_random_password()
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Generated admin password")

            if vals_to_update:
                instance.write(vals_to_update)
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Updated instance with generated passwords")

            try:
                # Create host directories for addons and configurations
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Creating host directories...")
                instance._create_host_directories()

                # Execute bash script installation instead of odoo-compose
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Running bash script installation...")
                installation_result = instance._execute_bash_installation()

                # Check and clone repositories with explicit skip logic
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Checking and cloning repositories...")
                instance.add_to_log("[INFO] Checking repository requirements...")

                # Count total repositories and categorize them

                # Setup custom addons (odoo.conf is created by bash script)
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Setting up custom addons...")
                instance._setup_custom_addons()

                # Log credentials and completion info
                user_login_password = instance.user_phone if instance.user_phone else instance.user_password

                instance.add_to_log("[INFO] Environment setup completed successfully!")
                instance.add_to_log(f"[INFO] Database: {instance.database_name}")
                instance.add_to_log(f"[INFO] Login Email: {instance.user_email}")
                instance.add_to_log(f"[INFO] Login Password: {user_login_password}")
                instance.add_to_log(f"[INFO] Admin Master Password: {instance.admin_password}")
                # Fix typo in company name display
                display_company = instance.company_name.replace('Abdulrahamn',
                                                                'Abdulrahman') if instance.company_name else ''
                instance.add_to_log(f"[INFO] Company: {display_company}")
                instance.add_to_log(f"[INFO] Country: {instance.country_id.name if instance.country_id else 'Not set'}")

                instance.add_to_log(f"[INFO] Instance ready to start")

                # Create subdomain configuration if enabled

                # Set state to stopped (ready to start)
                # instance.write({'state': 'stopped'})
                # odoo compose cleanup no longer needed with bash script installation
                instance.start_instance()

                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] odoo environment created successfully")
                if instance.includes_subdomain and instance.subdomain_name:
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Creating subdomain configuration")
                    instance.add_to_log("[INFO] Creating subdomain configuration...")
                    instance._create_subdomain_config()
                # ENFORCE USER LIMIT after instance is started

                # --- Install Odoo addons from plan if any ---
                if instance.plan_id and instance.plan_id.odoo_addon_line_ids:
                    addon_names = [a.name for a in instance.plan_id.odoo_addon_line_ids]
                    if addon_names:
                        instance.add_to_log(f"[INFO] Installing plan Odoo addons: {', '.join(addon_names)}")
                        instance.install_custom_addon_in_odoo(addon_names)
                # --- Install Custom addons from plan if any ---
                if instance.plan_id and instance.plan_id.custom_addon_line_ids:
                    custom_addon_names = [a.addon_name for a in instance.plan_id.custom_addon_line_ids]
                    if custom_addon_names:
                        instance.add_to_log(f"[INFO] Installing plan Custom addons: {', '.join(custom_addon_names)}")
                        instance.install_custom_addon_in_odoo(custom_addon_names)

                # Return the installation success notification
                return installation_result

            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Failed to create environment: {str(e)}")
                instance.add_to_log(f"[ERROR] Failed to create environment: {str(e)}")
                instance.write({'state': 'error'})
                raise UserError(f"Environment creation failed: {str(e)}")

    def _execute_bash_installation(self):
        """Execute bash script installation similar to OdooInstance but with SaaS features"""
        self.ensure_one()

        if self.state not in ['draft', 'error']:
            raise UserError("Instance can only be installed from Draft or Error state")

        # Get script path (same as OdooInstance)
        script_path = self.config_id.script_path

        # Pre-installation checks
        self.add_to_log("[INFO] Starting installation pre-checks...")

        if not os.path.exists(script_path):
            error_msg = f"Installation script not found: {script_path}"
            self.add_to_log(f"[ERROR] {error_msg}")
            self.state = 'error'
            raise UserError(error_msg)

        self.add_to_log(f"[INFO] Script found at: {script_path}")

        # Check script permissions
        if not os.access(script_path, os.X_OK):
            self.add_to_log("[WARNING] Script is not executable, attempting to fix...")
            try:
                self.chmod_with_sudo(script_path, 0o755)
                self.add_to_log("[INFO] Script permissions fixed (755)")
            except Exception as e:
                error_msg = f"Cannot make script executable: {str(e)}"
                self.add_to_log(f"[ERROR] {error_msg}")
                self.state = 'error'
                raise UserError(error_msg)

        self.state = 'installing'

        # Get Odoo version from template
        if not self.template_id or not self.template_id.odoo_version:
            raise UserError(f"Template with Odoo version must be configured for instance '{self.name}'")
        odoo_version = self.template_id.odoo_version
        company_name = self.company_name or ""
        # Lowercase, remove all non-alphanumeric characters
        db_user = re.sub(r'[^0-9A-Za-z]+', '', company_name).lower()
        # Prepare enhanced addons path including repositories
        enhanced_addons_path = self._get_enhanced_addons_path_for_script()

        # Prepare the command with user credentials for admin setup
        country_code = self.country_id.code if self.country_id else "US"
        is_demo = "true" if self.is_demo else "false"
        cmd = [
            'sudo', '-S', script_path,
            self.name,  # instance name
            odoo_version,  # odoo version
            self._get_source_path_from_template(),  # source path
            str(self.http_port),  # http port
            self.database_name,  # db name
            db_user,  # db user (standardized)
            'adminpwd',  # db password (standardized)
            self.admin_password,  # admin password
            self.user_email,  # user email for admin login
            self.user_phone or self.user_password,  # user phone/password for admin password
            country_code,  # country code
            is_demo  # demo data flag
        ]

        self.add_to_log("[INFO] " + "=" * 60)
        self.add_to_log("[INFO] INSTALLATION COMMAND EXECUTED:")
        safe_cmd = cmd[:3] + [self.name, odoo_version, "***", str(self.http_port), "***", "***", "***", "***"]
        self.add_to_log("[INFO] " + " ".join(safe_cmd))
        self.add_to_log("[INFO] " + "=" * 60)
        self.add_to_log("[INFO] Starting installation process...")

        try:
            _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Running bash script installation")

            # Set environment for non-interactive operation
            env = os.environ.copy()
            env['DEBIAN_FRONTEND'] = 'noninteractive'
            env['NEEDRESTART_MODE'] = 'a'

            # Run with sudo password provided via stdin
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1,
                env=env
            )

            # Provide sudo password via stdin
            sudo_password_input = self.root_sudo_password + '\n'
            process.stdin.write(sudo_password_input)
            process.stdin.flush()

            stdout_lines, stderr_lines = [], []
            line_count = 0

            # Real-time output capture (same as OdooInstance)
            self.add_to_log("[INFO] Reading process output in real-time...")

            while True:
                # Read stdout
                stdout_line = process.stdout.readline()
                if stdout_line:
                    clean_line = stdout_line.strip()
                    stdout_lines.append(clean_line)
                    self.add_to_log(f"[OUT] {clean_line}")
                    line_count += 1

                # Read stderr
                stderr_line = process.stderr.readline()
                if stderr_line:
                    clean_line = stderr_line.strip()
                    stderr_lines.append(clean_line)
                    self.add_to_log(f"[ERR] {clean_line}")
                    line_count += 1

                # Check if process has finished
                if process.poll() is not None:
                    break

            # Get return code
            return_code = process.poll()
            self.add_to_log(f"[INFO] Process completed with return code: {return_code}")

            # Capture any remaining output
            remaining_stdout, remaining_stderr = process.communicate()
            if remaining_stdout:
                for line in remaining_stdout.split('\n'):
                    if line.strip():
                        stdout_lines.append(line.strip())
                        self.add_to_log(f"[OUT] {line.strip()}")
            if remaining_stderr:
                for line in remaining_stderr.split('\n'):
                    if line.strip():
                        stderr_lines.append(line.strip())
                        self.add_to_log(f"[ERR] {line.strip()}")

            if return_code == 0:
                self.state = 'installed'
                self.add_to_log("[INFO] ✓ Installation completed successfully!")

                # Extract credentials from output
                admin_password = None
                user_email = None
                user_phone = None

                for line in stdout_lines + stderr_lines:
                    if line.startswith("ADMIN_PASSWORD:"):
                        admin_password = line.split("ADMIN_PASSWORD:")[-1].strip()
                    elif line.startswith("USER_EMAIL:"):
                        user_email = line.split("USER_EMAIL:")[-1].strip()
                    elif line.startswith("USER_PHONE:"):
                        user_phone = line.split("USER_PHONE:")[-1].strip()

                if admin_password:
                    self.admin_password = admin_password
                    self.add_to_log(f"[INFO] ✓ Admin password extracted: {admin_password}")

                if user_email:
                    self.add_to_log(f"[INFO] ✓ User email confirmed: {user_email}")

                if user_phone:
                    self.add_to_log(f"[INFO] ✓ User phone confirmed: {user_phone}")

                if not (admin_password and user_email and user_phone):
                    self.add_to_log("[WARNING] ⚠ Some credentials not found in output, but installation completed")

                _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Bash script installation completed successfully")

                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Success',
                        'message': f'Instance {self.name} installed successfully!',
                        'type': 'success',
                        'sticky': True,
                    }
                }
            else:
                self.state = 'error'
                error_analysis = self._analyze_installation_error(return_code, stdout_lines, stderr_lines)

                self.add_to_log("[INFO] " + "=" * 60)
                self.add_to_log("[ERROR] ❌ INSTALLATION FAILED")
                self.add_to_log(f"[ERROR] Return code: {return_code}")
                self.add_to_log(f"[ERROR] Error analysis: {error_analysis}")
                self.add_to_log("[INFO] " + "=" * 60)

                # Add full error context
                self.add_to_log("[ERROR] FULL ERROR OUTPUT:")
                for i, line in enumerate(stderr_lines, 1):
                    self.add_to_log(f"[ERR][{i}]: {line}")

                self.add_to_log("[ERROR] LAST 10 STDOUT LINES:")
                for line in stdout_lines[-10:]:
                    self.add_to_log(f"[OUT]: {line}")

                _logger.error(
                    f"[LAUNCHLY_SAAS - {self.name}] Installation failed with code {return_code}: {error_analysis}")

                raise UserError(
                    f"Installation failed (Code {return_code}): {error_analysis}\nCheck installation log for details.")

        except Exception as e:
            self.state = 'error'
            error_msg = f"Installation failed with exception: {str(e)}"
            self.add_to_log(f"[ERROR] {error_msg}")
            _logger.error(f"[LAUNCHLY_SAAS - {self.name}] {error_msg}")
            raise UserError(error_msg)

    def _get_enhanced_addons_path_for_script(self):
        """Get enhanced addons path including all repositories for the bash script"""
        # Start with the computed addons path or build it
        paths = []

        # Add source addons (will be handled by script)
        source_path = self._get_source_path_from_template()
        if source_path:
            paths.append(f"{source_path}/addons")

        # Add custom addons path (will be created by script)
        paths.append(f"/opt/{self.name}/custom-addons")

        # Add repository paths (these will be cloned after installation)

        return ",".join(paths)

    def _get_source_path_from_template(self):
        """Get Odoo source path from template"""
        if self.template_id and self.template_id.source_path:
            return self.template_id.source_path

        # Fallback to default path based on Odoo version from template
        odoo_version = self.template_id.odoo_version if self.template_id else '18'
        return f"/opt/odoo/odoo-{odoo_version}.0"

    def _get_odoo_version_from_template(self):
        """Get Odoo version from template"""
        if self.template_id and self.template_id.odoo_version:
            return self.template_id.odoo_version
        return '18'  # Default fallback

    def start_instance(self):
        """Start the Odoo instance using systemd"""
        for instance in self:
            if instance.state == 'running':
                instance.add_to_log("[INFO] Instance is already running")
                return

            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Starting instance")
            instance.add_to_log("[INFO] Starting Odoo service...")

            try:
                # Start the systemd service (similar to OdooInstance)
                cmd = ['sudo', '-S', 'systemctl', 'start', f'{instance.name}']

                result = subprocess.run(
                    cmd,
                    input=f"{instance.root_sudo_password}\n",
                    capture_output=True,
                    text=True,
                    check=True
                )

                instance.state = 'running'
                instance.add_to_log("[INFO] Odoo service started successfully")
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Instance started successfully")

                # Process any pending custom addons that were added before instance creation
                instance._process_pending_custom_addons()

                # User setup is now handled by bash script, only do addon management
                if not instance.user_done:
                    instance.add_to_log("[INFO] Setting up addon limits and refreshing addon list...")
                    instance._set_user_limit_in_instance()
                    instance._set_module_limit_in_instance()
                    instance.refresh_addons_list()
                    instance.user_done = True
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Addon setup completed")
                else:
                    instance.add_to_log("[INFO] User setup already completed, skipping user configuration")
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] User setup already done, skipping")

            except subprocess.CalledProcessError as e:
                instance.state = 'error'
                error_msg = f"Failed to start service: {e.stderr}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Failed to start instance: {e.stderr}")
            except Exception as e:
                instance.state = 'error'
                error_msg = f"Failed to start instance: {str(e)}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] {error_msg}")

        # Database creation and initialization is now handled by the bash script
        # This method is no longer needed but kept for compatibility
        return True

    def _setup_user_after_startup(self):
        """Setup user after Odoo instance is running - monitors logs for registry loaded message"""
        _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Setting up user after startup")
        self.add_to_log("[INFO] Monitoring Odoo logs for registry loaded message...")

        import time

        # Monitor Odoo logs for registry loaded message
        max_wait_time = 300  # 5 minutes maximum wait
        check_interval = 5  # Check every 5 seconds (more responsive)
        waited_time = 0
        last_position = 0  # Track file position to read only new content

        log_file_path = f"/var/log/{self.name}.log"

        while waited_time < max_wait_time:
            try:
                if os.path.exists(log_file_path):
                    try:
                        with open(log_file_path, 'r') as f:
                            # Seek to last read position to read only new content
                            f.seek(last_position)
                            new_content = f.read()

                            if new_content:
                                # Update position for next read
                                last_position = f.tell()

                                # Check if registry loaded message appears in new content
                                if "odoo.modules.registry: Registry loaded in" in new_content:
                                    _logger.info(
                                        f"[LAUNCHLY_SAAS - {self.name}] Registry loaded message found in logs, Odoo is ready!")
                                    self.add_to_log("[INFO] Odoo registry loaded successfully, instance is ready!")
                                    break

                            # Only log status every 30 seconds to reduce noise
                            if waited_time % 30 == 0:
                                _logger.info(
                                    f"[LAUNCHLY_SAAS - {self.name}] Registry not loaded yet, waiting... ({waited_time}s)")
                                self.add_to_log(f"[INFO] Waiting for Odoo registry to load... ({waited_time}s)")

                    except Exception as log_error:
                        # Only log errors every 30 seconds to reduce noise
                        if waited_time % 30 == 0:
                            _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Could not read log file: {str(log_error)}")
                            self.add_to_log(f"[INFO] Waiting for log file to be available... ({waited_time}s)")
                else:
                    # Only log file not found every 30 seconds
                    if waited_time % 30 == 0:
                        _logger.info(
                            f"[LAUNCHLY_SAAS - {self.name}] Log file not found yet, waiting... ({waited_time}s)")
                        self.add_to_log(f"[INFO] Waiting for Odoo to create log file... ({waited_time}s)")

                time.sleep(check_interval)
                waited_time += check_interval

            except Exception as e:
                # Only log errors every 30 seconds
                if waited_time % 30 == 0:
                    _logger.warning(f"[LAUNCHLY_SAAS - {self.name}] Error monitoring logs: {str(e)}")
                time.sleep(check_interval)
                waited_time += check_interval

        if waited_time >= max_wait_time:
            _logger.warning(
                f"[LAUNCHLY_SAAS - {self.name}] Timeout waiting for registry loaded message, proceeding anyway")
            self.add_to_log("[WARNING] Timeout waiting for registry loaded message, attempting user setup anyway...")

        # User setup is now handled by the bash script during installation
        # Just log the completion and mark user as done
        self.add_to_log("[INFO] User setup was completed during installation")
        self.add_to_log(f"[INFO] Instance ready at: {self.instance_url}")
        self.add_to_log(f"[INFO] Login: {self.user_email}")
        self.add_to_log(f"[INFO] Password: {self.user_phone if self.user_phone else self.user_password}")

        # Set user_done to True
        self.user_done = True
        _logger.info(f"[LAUNCHLY_SAAS - {self.name}] User setup marked as done")

        # Refresh db_users list
        self.add_to_log("[INFO] Refreshing database users list...")
        self.refresh_db_users()

    def setup_user_manually(self):
        """Manual button to setup user after instance is running"""
        for instance in self:
            if instance.state != 'running':
                instance.add_to_log("[ERROR] Instance must be running to setup user")
                return

            instance.add_to_log("[INFO] User setup is now handled by the installation script")
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] User setup is handled during installation")

    def stop_instance(self):
        """Stop Odoo systemd service for the instance"""
        for instance in self:
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Stopping Odoo service")
            instance.add_to_log("[INFO] Stopping Odoo service")

            try:
                # Stop the systemd service (similar to OdooInstance)
                cmd = ['sudo', '-S', 'systemctl', 'stop', f'{instance.name}']

                result = subprocess.run(
                    cmd,
                    input=f"{instance.root_sudo_password}\n",
                    capture_output=True,
                    text=True,
                    check=True
                )

                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Service stopped successfully")
                instance.add_to_log("[INFO] Odoo service stopped successfully!")
                instance.write({'state': 'stopped'})

            except subprocess.CalledProcessError as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Failed to stop service: {str(e)}")
                instance.add_to_log(f"[ERROR] Failed to stop service: {e.stderr}")
                instance.write({'state': 'error'})
            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Failed to stop service: {str(e)}")
                instance.add_to_log(f"[ERROR] Failed to stop service: {str(e)}")
                instance.write({'state': 'error'})

    def restart_instance(self):
        """Restart Odoo systemd service for the instance"""
        for instance in self:
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Restarting instance")
            instance.add_to_log("[INFO] Restarting Odoo service...")

            try:
                # Restart the systemd service (similar to OdooInstance)
                cmd = ['sudo', '-S', 'systemctl', 'restart', f'{instance.name}']

                result = subprocess.run(
                    cmd,
                    input=f"{instance.root_sudo_password}\n",
                    capture_output=True,
                    text=True,
                    check=True
                )

                instance.state = 'running'
                instance.add_to_log("[INFO] Odoo service restarted successfully")
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Instance restarted successfully")

            except subprocess.CalledProcessError as e:
                instance.state = 'error'
                error_msg = f"Failed to restart service: {e.stderr}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Failed to restart instance: {e.stderr}")
            except Exception as e:
                instance.state = 'error'
                error_msg = f"Failed to restart instance: {str(e)}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] {error_msg}")
                instance.write({'state': 'error'})

    def reload_instance(self):
        """Reload Odoo systemd service for the instance"""
        for instance in self:
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Reloading Odoo service")
            instance.add_to_log("[INFO] Reloading Odoo service")

            try:
                # Reload the systemd service (equivalent to restart)
                cmd = ['sudo', '-S', 'systemctl', 'restart', f'{instance.name}']

                result = subprocess.run(
                    cmd,
                    input=f"{instance.root_sudo_password}\n",
                    capture_output=True,
                    text=True,
                    check=True
                )

                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Service reloaded successfully")
                instance.add_to_log("[INFO] Odoo service reloaded successfully!")
                instance.add_to_log(f"[INFO] Access URL: {instance.instance_url}")
                instance.write({'state': 'running'})

            except subprocess.CalledProcessError as e:
                instance.state = 'error'
                error_msg = f"Failed to reload service: {e.stderr}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Failed to reload instance: {e.stderr}")
                instance.write({'state': 'error'})
            except Exception as e:
                instance.state = 'error'
                error_msg = f"Failed to reload instance: {str(e)}"
                instance.add_to_log(f"[ERROR] {error_msg}")
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] {error_msg}")
                instance.write({'state': 'error'})

    def excute_command_with_sudo(self, cmd, shell=True, check=True):
        """Execute command with sudo using the stored password"""
        _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Executing sudo command: {cmd}")

        if self.root_sudo_password:
            # Prepend sudo -S to the command
            sudo_cmd = f"sudo -S {cmd}"
            try:
                result = subprocess.run(
                    sudo_cmd,
                    shell=shell,
                    check=check,
                    input=self.root_sudo_password + '\n',
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=300  # 5 minute timeout
                )

                # Log successful execution
                if result.stdout:
                    stdout_str = result.stdout
                    _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Sudo command output: {stdout_str}")
                    self.add_to_log(f"[INFO] Sudo command output: {stdout_str}")

                _logger.info(
                    f"[LAUNCHLY_SAAS - {self.name}] Sudo command executed successfully with return code: {result.returncode}")
                return result

            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {self.name}] Sudo command failed: {sudo_cmd}")
                _logger.error(f"[LAUNCHLY_SAAS - {self.name}] Error: {str(e)}")

                self.add_to_log(f"Error to execute sudo command: {str(e)}")
                if hasattr(e, 'stderr') and e.stderr:
                    stderr_str = e.stderr
                    _logger.error(f"[LAUNCHLY_SAAS - {self.name}] Sudo command stderr: {stderr_str}")
                    self.add_to_log("[ERROR]  " + stderr_str)
                else:
                    self.add_to_log("[ERROR]  " + str(e))
                raise e
        else:
            # Fallback to regular command execution
            return self.excute_command(cmd, shell=shell, check=check)

    def excute_command(self, cmd, shell=True, check=True):
        # Log command execution to Odoo backend
        _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Executing command: {cmd}")

        try:
            result = subprocess.run(cmd, shell=shell, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            # Log successful execution
            if result.stdout:
                stdout_str = result.stdout.decode('utf-8')
                _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Command output: {stdout_str}")
                self.add_to_log(f"[INFO] Command output: {stdout_str}")

            _logger.info(
                f"[LAUNCHLY_SAAS - {self.name}] Command executed successfully with return code: {result.returncode}")
            return result

        except Exception as e:
            # Enhanced error logging
            _logger.error(f"[LAUNCHLY_SAAS - {self.name}] Command failed: {cmd}")
            _logger.error(f"[LAUNCHLY_SAAS - {self.name}] Error: {str(e)}")

            self.add_to_log(f"Error to execute command: {str(e)}")
            self.add_to_log("[INFO] **** Execute the following command manually from the terminal for more details "
                            "****  " + cmd)
            if hasattr(e, 'stderr') and e.stderr:
                stderr_str = e.stderr.decode('utf-8')
                _logger.error(f"[LAUNCHLY_SAAS - {self.name}] Command stderr: {stderr_str}")
                self.add_to_log("[ERROR]  " + stderr_str)
            else:
                self.add_to_log("[ERROR]  " + str(e))
            raise e

    def _makedirs(self, path):
        try:
            if not os.path.exists(path):
                _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Creating directory: {path}")

                # Check if we need sudo for /opt directories
                if path.startswith('/opt') and self.root_sudo_password:
                    # Use sudo to create directories in /opt
                    result = self.excute_command_with_sudo(f"mkdir -p {path}", check=False)
                    if result.returncode == 0:
                        _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Directory created successfully with sudo: {path}")
                    else:
                        _logger.error(
                            f"[LAUNCHLY_SAAS - {self.name}] Failed to create directory with sudo {path}: {result.stderr}")
                        self.add_to_log(f"Error while creating directory {path} with sudo: {result.stderr}")
                        # Fallback to regular makedirs
                        os.makedirs(path, exist_ok=True)
                        _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Directory created with fallback method: {path}")
                else:
                    # Use regular makedirs for non-/opt directories or when no sudo password
                    os.makedirs(path, exist_ok=True)
                    _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Directory created successfully: {path}")
            else:
                _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Directory already exists: {path}")
        except Exception as e:
            _logger.error(f"[LAUNCHLY_SAAS - {self.name}] Failed to create directory {path}: {str(e)}")
            self.add_to_log(f"Error while creating directory {path} : {str(e)}")

    def create_file(self, modified_path, script_content):
        try:
            _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Creating file: {modified_path}")
            _logger.info(
                f"[LAUNCHLY_SAAS - {self.name}] File content length: {len(script_content) if script_content else 0} characters")
            with open(modified_path, "w") as modified_file:
                modified_file.write(script_content)
            _logger.info(f"[LAUNCHLY_SAAS - {self.name}] File created successfully: {modified_path}")
        except PermissionError as e:
            _logger.warning(
                f"[LAUNCHLY_SAAS - {self.name}] Permission denied for {modified_path}, trying with sudo: {str(e)}")
            # Try to create the file with sudo
            if self.create_file_with_sudo(modified_path, script_content):
                _logger.info(f"[LAUNCHLY_SAAS - {self.name}] File created successfully with sudo: {modified_path}")
            else:
                self.state = 'error'
                raise e
        except Exception as e:
            _logger.error(f"[LAUNCHLY_SAAS - {self.name}] Failed to create file {modified_path}: {str(e)}")
            self.state = 'error'
            self.add_to_log(f"[ERROR] Error to create file: {str(e)}")

    def create_file_with_sudo(self, file_path, content):
        """Create a file with sudo privileges when regular file creation fails due to permissions"""
        try:
            _logger.info(f"[LAUNCHLY_SAAS - {self.name}] Creating file with sudo: {file_path}")
            _logger.info(
                f"[LAUNCHLY_SAAS - {self.name}] File content length: {len(content) if content else 0} characters")

            # First ensure the directory exists
            directory = os.path.dirname(file_path)
            if directory and not os.path.exists(directory):
                mkdir_cmd = f"mkdir -p '{directory}'"
                if self.root_sudo_password:
                    self.excute_command_with_sudo(mkdir_cmd, shell=True, check=True)
                else:
                    self.excute_command(mkdir_cmd, shell=True, check=True)

            # Create a temporary file first
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.tmp') as tmp_file:
                tmp_file.write(content)
                tmp_file_path = tmp_file.name

            # Move the temporary file to the target location using sudo
            move_cmd = f"mv '{tmp_file_path}' '{file_path}'"
            if self.root_sudo_password:
                result = self.excute_command_with_sudo(move_cmd, shell=True, check=True)
            else:
                result = self.excute_command(move_cmd, shell=True, check=True)

            _logger.info(f"[LAUNCHLY_SAAS - {self.name}] File created successfully with sudo: {file_path}")
            return True

        except Exception as e:
            _logger.error(f"[LAUNCHLY_SAAS - {self.name}] Failed to create file with sudo {file_path}: {str(e)}")
            # Clean up temporary file if it exists
            try:
                if 'tmp_file_path' in locals() and os.path.exists(tmp_file_path):
                    os.unlink(tmp_file_path)
            except:
                pass
            self.add_to_log(f"[ERROR] Failed to create file with sudo: {str(e)}")
            return False

    def chmod_with_sudo(self, file_path, mode):
        """Change file permissions with sudo when regular chmod fails due to permissions"""
        try:
            # Try regular chmod first
            os.chmod(file_path, mode)
            _logger.info(
                f"[LAUNCHLY_SAAS - {self.name}] File permissions changed successfully: {file_path} (mode: {oct(mode)})")
            return True
        except PermissionError:
            _logger.warning(f"[LAUNCHLY_SAAS - {self.name}] Permission denied for chmod {file_path}, trying with sudo")
            try:
                # Use sudo to change permissions
                chmod_cmd = f"chmod {oct(mode)[2:]} '{file_path}'"
                if self.root_sudo_password:
                    result = self.excute_command_with_sudo(chmod_cmd, shell=True, check=True)
                else:
                    result = self.excute_command(chmod_cmd, shell=True, check=True)

                _logger.info(
                    f"[LAUNCHLY_SAAS - {self.name}] File permissions changed successfully with sudo: {file_path} (mode: {oct(mode)})")
                return True
            except Exception as e:
                _logger.error(
                    f"[LAUNCHLY_SAAS - {self.name}] Failed to change permissions with sudo {file_path}: {str(e)}")
                self.add_to_log(f"[ERROR] Failed to change file permissions: {str(e)}")
                return False
        except Exception as e:
            _logger.error(f"[LAUNCHLY_SAAS - {self.name}] Failed to change file permissions {file_path}: {str(e)}")
            self.add_to_log(f"[ERROR] Failed to change file permissions: {str(e)}")
            return False

    @api.depends('company_name')
    def _compute_database_name(self):
        for instance in self:
            if instance.company_name:
                # Create database name from company name (sanitized)
                db_name = instance.company_name.lower().replace(' ', '_').replace('-', '_')
                # Remove special characters and keep only alphanumeric and underscore
                db_name = ''.join(c for c in db_name if c.isalnum() or c == '_')

                # Fix common typos in the database name (safeguard)
                db_name = db_name.replace('abdulrahamn', 'abdulrahman')  # Fix missing 'h'

                instance.database_name = db_name[:63]  # PostgreSQL database name limit
            else:
                instance.database_name = False

    @api.model
    def create(self, vals):
        # Generate random passwords when creating instance
        if not vals.get('user_password'):
            vals['user_password'] = self._generate_random_password()
        if not vals.get('admin_password'):
            vals['admin_password'] = self._generate_random_password()
        plan_id = vals.get('plan_id')
        skip_template = self.env.context.get('skip_template_apply')
        if plan_id:
            plan = self.env['instance.plan'].browse(plan_id)
            if plan:
                vals['is_demo'] = plan.is_demo
                vals['allowed_users_count'] = plan.allowed_users_count
                vals['allowed_modules_count'] = plan.allowed_modules_count
                if plan.template_id and not skip_template:
                    vals['template_id'] = plan.template_id.id

        instance = super().create(vals)
        # Always copy addons from plan, regardless of skip_template
        if instance.plan_id:
            for custom_addon in instance.plan_id.custom_addon_line_ids:
                custom_addon.copy({'instance_id': instance.id})
            for odoo_addon in instance.plan_id.odoo_addon_line_ids:
                odoo_addon.copy({'instance_id': instance.id})
            # Only apply template logic if not skipped

        config = instance.env['saas.config'].search([], limit=1)
        if config:
            backup_path = os.path.join(config.backup_path or '/tmp', instance.name)
            instance.env['odoo.instance.backup'].create({
                'config_id': config.id,
                'instance_ids': [(6, 0, [instance.id])],
                'backup_path': backup_path,
                'auto_remove': True,
                'days_to_remove': 3,
            })
        return instance

    def _generate_random_password(self):
        """Generate a secure random password"""
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        password = ''.join(secrets.choice(alphabet) for i in range(12))
        _logger.info(f"Generated secure password for new instance")
        return password

    def unlink(self):
        """Delete instance and clean up all associated odoo service and files"""
        for instance in self:
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Starting complete instance deletion process")
            instance.add_to_log("[INFO] Starting complete instance deletion - this will destroy all data")

            # Call destroy_instance to ensure complete cleanup
            try:
                instance.destroy_instance()
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Instance destruction completed successfully")
            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error during destruction process: {str(e)}")
                # Continue with deletion even if destruction fails
                instance.add_to_log(f"[WARNING] Some cleanup operations failed: {str(e)}")

                # Fallback cleanup if destroy_instance fails
                try:

                    # Clean up instance files and directories as fallback
                    if os.path.exists(instance.instance_data_path):
                        import shutil
                        shutil.rmtree(instance.instance_data_path)
                        _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Fallback cleanup completed")

                except Exception as fallback_error:
                    _logger.warning(
                        f"[LAUNCHLY_SAAS - {instance.name}] Fallback cleanup also failed: {str(fallback_error)}")
                    instance.add_to_log(f"[ERROR] Could not remove some files: {str(fallback_error)}")

        return super(OdooInstance, self).unlink()

    def destroy_instance(self):
        """Completely destroy the systemd service, database, and all files (full reset)"""
        for instance in self:
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Destroying systemd service and database")
            instance.add_to_log("[WARNING] Destroying systemd service and database - This cannot be undone!")

            # Remove subdomain configuration if it exists
            if instance.includes_subdomain and instance.subdomain_name:
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Removing subdomain configuration")
                instance.add_to_log("[INFO] Removing subdomain configuration...")
                instance._remove_subdomain_config()

            try:
                # First, stop the systemd service
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Stopping systemd service...")
                instance.add_to_log("[INFO] Stopping systemd service...")

                try:
                    # Stop the service
                    stop_cmd = ['sudo', '-S', 'systemctl', 'stop', f'{instance.name}']
                    if instance.root_sudo_password:
                        subprocess.run(
                            stop_cmd,
                            input=instance.root_sudo_password + '\n',
                            text=True,
                            capture_output=True,
                            timeout=30
                        )

                    # Disable the service
                    disable_cmd = ['sudo', '-S', 'systemctl', 'disable', f'{instance.name}']
                    if instance.root_sudo_password:
                        subprocess.run(
                            disable_cmd,
                            input=instance.root_sudo_password + '\n',
                            text=True,
                            capture_output=True,
                            timeout=30
                        )

                    instance.add_to_log("[INFO] Systemd service stopped and disabled")

                    # Drop the database using direct PostgreSQL access
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Dropping database...")
                    instance.add_to_log("[INFO] Dropping database...")

                    drop_db_cmd = [
                        'sudo', '-u', 'postgres', 'psql',
                        '-d', 'postgres',
                        '-c', f"DROP DATABASE IF EXISTS {instance.database_name};"
                    ]

                    drop_result = subprocess.run(drop_db_cmd, capture_output=True, text=True, timeout=30)

                    if drop_result.returncode == 0:
                        _logger.info(
                            f"[LAUNCHLY_SAAS - {instance.name}] Database {instance.database_name} dropped successfully")
                        instance.add_to_log(
                            f"[SUCCESS] Database '{instance.database_name}' dropped successfully")
                    else:
                        _logger.warning(
                            f"[LAUNCHLY_SAAS - {instance.name}] Failed to drop database: {drop_result.stderr}")
                        instance.add_to_log(f"[WARNING] Failed to drop database: {drop_result.stderr}")

                except Exception as db_drop_error:
                    _logger.warning(
                        f"[LAUNCHLY_SAAS - {instance.name}] Error during database drop attempt: {str(db_drop_error)}")
                    instance.add_to_log(f"[WARNING] Error during database drop: {str(db_drop_error)}")
                    instance.add_to_log("[INFO] Will proceed with service and file removal")

                # Remove systemd service file
                service_file = f"/etc/systemd/system/{instance.name}.service"
                if os.path.exists(service_file):
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Removing systemd service file...")
                    instance.add_to_log("[INFO] Removing systemd service file...")

                    try:
                        remove_service_cmd = f"sudo -S rm {service_file}"
                        if instance.root_sudo_password:
                            subprocess.run(
                                remove_service_cmd,
                                shell=True,
                                input=instance.root_sudo_password + '\n',
                                text=True,
                                capture_output=True,
                                timeout=10
                            )

                        # Reload systemd daemon
                        reload_cmd = "sudo -S systemctl daemon-reload"
                        if instance.root_sudo_password:
                            subprocess.run(
                                reload_cmd,
                                shell=True,
                                input=instance.root_sudo_password + '\n',
                                text=True,
                                capture_output=True,
                                timeout=10
                            )

                        instance.add_to_log("[INFO] Systemd service file removed and daemon reloaded")

                    except Exception as e:
                        _logger.warning(f"[LAUNCHLY_SAAS - {instance.name}] Could not remove service file: {str(e)}")
                        instance.add_to_log(f"[WARNING] Could not remove service file: {str(e)}")

                # Remove system user and home directory
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Removing system user and home directory...")
                instance.add_to_log("[INFO] Removing system user and home directory...")

                try:
                    # Remove user home directory first
                    user_home = f"/opt/{instance.name}"
                    if os.path.exists(user_home):
                        remove_home_cmd = f"sudo -S rm -rf {user_home}"
                        if instance.root_sudo_password:
                            subprocess.run(
                                remove_home_cmd,
                                shell=True,
                                input=instance.root_sudo_password + '\n',
                                text=True,
                                capture_output=True,
                                timeout=60
                            )

                    # Remove system user
                    remove_user_cmd = f"sudo -S deluser --system {instance.name}"
                    if instance.root_sudo_password:
                        subprocess.run(
                            remove_user_cmd,
                            shell=True,
                            input=instance.root_sudo_password + '\n',
                            text=True,
                            capture_output=True,
                            timeout=30
                        )

                    # Remove configuration file
                    config_file = f"/etc/{instance.name}.conf"
                    if os.path.exists(config_file):
                        remove_config_cmd = f"sudo -S rm {config_file}"
                        if instance.root_sudo_password:
                            subprocess.run(
                                remove_config_cmd,
                                shell=True,
                                input=instance.root_sudo_password + '\n',
                                text=True,
                                capture_output=True,
                                timeout=10
                            )

                    instance.add_to_log("[INFO] System user and configuration files removed")

                except Exception as e:
                    _logger.warning(f"[LAUNCHLY_SAAS - {instance.name}] Could not remove system user: {str(e)}")
                    instance.add_to_log(f"[WARNING] Could not remove system user: {str(e)}")

                instance.add_to_log("[INFO] All systemd resources cleaned up")

                # Clean up host directories using sudo if password is provided
                if os.path.exists(instance.instance_data_path):
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Removing instance data directory...")
                    instance.add_to_log("[INFO] Removing all instance data files...")

                    if instance.root_sudo_password:
                        # Use sudo to remove directories with proper permissions
                        try:
                            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Using sudo to remove instance data...")
                            instance.add_to_log("[INFO] Using sudo to remove instance data...")

                            # Change ownership to current user first, then remove
                            sudo_commands = [
                                f"sudo -S chown -R {os.getuid()}:{os.getgid()} {instance.instance_data_path}",
                                f"sudo -S chmod -R 755 {instance.instance_data_path}",
                                f"sudo -S rm -rf {instance.instance_data_path}"
                            ]

                            for cmd in sudo_commands:
                                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Executing: {cmd}")
                                result = subprocess.run(
                                    cmd,
                                    shell=True,
                                    input=instance.root_sudo_password + '\n',
                                    text=True,
                                    capture_output=True,
                                    timeout=60
                                )

                                if result.returncode != 0:
                                    _logger.warning(
                                        f"[LAUNCHLY_SAAS - {instance.name}] Sudo command failed: {cmd}, Error: {result.stderr}")
                                else:
                                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Sudo command successful: {cmd}")

                            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Instance data removed using sudo")
                            instance.add_to_log("[SUCCESS] Instance data removed using sudo")

                        except Exception as sudo_error:
                            _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Sudo removal failed: {str(sudo_error)}")
                            instance.add_to_log(f"[ERROR] Sudo removal failed: {str(sudo_error)}")

                            # Fallback to regular removal
                            try:
                                import shutil
                                shutil.rmtree(instance.instance_data_path)
                                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Fallback removal successful")
                                instance.add_to_log("[INFO] Fallback removal successful")
                            except Exception as fallback_error:
                                _logger.error(
                                    f"[LAUNCHLY_SAAS - {instance.name}] Fallback removal also failed: {str(fallback_error)}")
                                instance.add_to_log(f"[ERROR] Could not remove some files: {str(fallback_error)}")

                    else:
                        # Regular removal without sudo
                        try:
                            # Remove PostgreSQL data directory (contains the database)
                            postgresql_dir = os.path.join(instance.instance_data_path, "postgresql")
                            if os.path.exists(postgresql_dir):
                                shutil.rmtree(postgresql_dir)
                                _logger.info(
                                    f"[LAUNCHLY_SAAS - {instance.name}] Removed PostgreSQL data directory (database dropped)")
                                instance.add_to_log("[INFO] Database files removed (database dropped)")

                            # Remove other data directories
                            data_dirs = ["data", "logs", "addons", "etc", "odoo_init", "postgresql_init"]
                            for data_dir in data_dirs:
                                dir_path = os.path.join(instance.instance_data_path, data_dir)
                                if os.path.exists(dir_path):
                                    shutil.rmtree(dir_path)
                                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Removed directory: {dir_path}")

                            # Remove the entire instance directory if it's empty
                            try:
                                os.rmdir(instance.instance_data_path)
                                _logger.info(
                                    f"[LAUNCHLY_SAAS - {instance.name}] Removed instance directory: {instance.instance_data_path}")
                                instance.add_to_log("[INFO] Instance directory removed")
                            except OSError:
                                # Directory not empty, that's ok
                                _logger.info(
                                    f"[LAUNCHLY_SAAS - {instance.name}] Instance directory not empty, keeping: {instance.instance_data_path}")

                        except Exception as e:
                            _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error removing instance files: {str(e)}")
                            instance.add_to_log(f"[WARNING] Error removing some files: {str(e)}")

                # Reset repository clone status

                # Clear logs
                instance.odoo_logs = ""
                instance.log = ""

                # Set state to draft (needs to be recreated)
                instance.write({'state': 'draft'})

                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Instance destroyed successfully")
                instance.add_to_log("[SUCCESS] Instance completely destroyed!")
                instance.add_to_log("[INFO] Database properly dropped and all resources removed")
                instance.add_to_log("[INFO] Instance reset to draft state - ready for recreation")
                instance.add_to_log("[INFO] You can now run 'Create odoo Environment' to recreate the instance")

            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Failed to destroy instance: {str(e)}")
                instance.add_to_log(f"[ERROR] Failed to destroy instance: {str(e)}")
                instance.write({'state': 'error'})

    def _update_odoo_conf_addons_path(self):
        """Update odoo.conf file with current addons path, ensuring correct line endings, formatting, and indentation"""
        for instance in self:
            try:
                odoo_conf_path = f"/etc/{instance.name}.conf"

                if not os.path.exists(odoo_conf_path):
                    _logger.warning(f"[LAUNCHLY_SAAS - {instance.name}] odoo.conf not found, creating new one")

                    return

                # Read current odoo.conf
                with open(odoo_conf_path, 'r') as f:
                    conf_lines = f.readlines()

                # Detect indentation from the first non-empty, non-comment config line with '='
                indent = ""
                for l in conf_lines:
                    if l.strip() and not l.strip().startswith("#") and "=" in l:
                        indent = l[:len(l) - len(l.lstrip())]
                        break

                # Update addons_path line
                updated_lines = []
                addons_path_updated = False

                for line in conf_lines:
                    if line.strip().startswith('addons_path'):
                        # Get current computed addons path
                        current_addons_path = f"/opt/{instance.name}/custom-addons"
                        if current_addons_path:
                            # Always include base Odoo addons
                            base_addons = instance.template_id.source_path
                            full_addons_path = f"{base_addons},{current_addons_path}"
                        else:
                            full_addons_path = instance.template_id.source_path
                        updated_lines.append(f"{indent}addons_path = {full_addons_path}\n")
                        addons_path_updated = True
                        _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Updated addons_path to: {full_addons_path}")
                    else:
                        # Ensure every line ends with a single newline
                        updated_lines.append(line if line.endswith('\n') else line + '\n')

                # If addons_path wasn't found, add it at the end
                if not addons_path_updated:
                    current_addons_path = f"/opt/{instance.name}/custom-addons"
                    if current_addons_path:
                        base_addons = instance.template_id.source_path
                        full_addons_path = f"{base_addons},{current_addons_path}"
                    else:
                        full_addons_path = instance.template_id.source_path
                    updated_lines.append(f"{indent}addons_path = {full_addons_path}\n")
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Added addons_path: {full_addons_path}")

                # Write updated odoo.conf
                with open(odoo_conf_path, 'w') as f:
                    f.writelines(updated_lines)

                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] odoo.conf updated successfully")
                instance.add_to_log("[INFO] odoo.conf updated with new addons path")

            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error updating odoo.conf: {str(e)}")
                instance.add_to_log(f"[ERROR] Error updating odoo.conf: {str(e)}")

    def install_custom_addon_in_odoo(self, addon_names):
        """Install specific addons in the running Odoo instance via superuser script."""
        for instance in self:
            if instance.state != 'running':
                instance.add_to_log("[ERROR] Instance must be running to install addons")
                return False

            try:
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Installing addons {addon_names} via superuser script")
                instance.add_to_log(f"[INFO] Installing addons {addon_names} via internal script...")

                # Convert addon_names to a Python list string for the script
                addon_names_str = str(addon_names) if isinstance(addon_names, list) else str([addon_names])

                script_content = f'''#!/usr/bin/env python3
import os
import sys

# Add Odoo source path to Python path (from the source installation)
odoo_source_path = '{instance._get_source_path_from_template()}'
sys.path.insert(0, odoo_source_path)

import odoo
from odoo import api, SUPERUSER_ID

# Configure Odoo
odoo.tools.config.parse_config(['-c', '/etc/{instance.name}.conf'])

try:
    from odoo.modules.registry import Registry
    registry = Registry.new('{instance.database_name}')

    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {{}})
        addon_names = {addon_names_str}

        for addon_name in addon_names:
            # Search for the module
            modules = env['ir.module.module'].search([('name', '=', addon_name)])

            if not modules:
                print("WARNING|Module '{{}}' not found".format(addon_name))
                continue

            module = modules[0]

            # Check if module is already installed
            if module.state == 'installed':
                print("WARNING|Module '{{}}' is already installed".format(addon_name))
                continue

            # Check if module is installable
            if module.state not in ('uninstalled', 'to install'):
                print("WARNING|Module '{{}}' cannot be installed (current state: {{}})".format(addon_name, module.state))
                continue

            # Install the module
            try:
                module.button_immediate_install()
                print("SUCCESS|Module '{{}}' installed successfully".format(addon_name))
            except Exception as e:
                print("ERROR|Failed to install module '{{}}': {{}}".format(addon_name, str(e)))

except Exception as e:
    print("ERROR|Script execution failed: {{}}".format(str(e)))
    '''

                script_path = os.path.join(instance.instance_data_path, "odoo_init", "install_addons.py")
                instance.create_file(script_path, script_content)
                instance.chmod_with_sudo(script_path, 0o755)

                # Execute script directly using instance's Python environment
                venv_python = f"/opt/{instance.name}/venv/bin/python3"
                cmd = f"{venv_python} {script_path}"

                if instance.root_sudo_password:
                    result = instance.excute_command_with_sudo(cmd, shell=True, check=False)
                else:
                    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)

                if result.returncode == 0:
                    output = result.stdout.strip().splitlines()
                    if not output:
                        instance.add_to_log("[ERROR] No output returned from script")
                        return False

                    success_count = 0
                    warning_count = 0
                    error_count = 0

                    for line in output:
                        if line.startswith("SUCCESS|"):
                            message = line.split("|", 1)[1]
                            instance.add_to_log(f"[SUCCESS] {message}")
                            success_count += 1
                        elif line.startswith("WARNING|"):
                            message = line.split("|", 1)[1]
                            instance.add_to_log(f"[WARNING] {message}")
                            warning_count += 1
                        elif line.startswith("ERROR|"):
                            message = line.split("|", 1)[1]
                            instance.add_to_log(f"[ERROR] {message}")
                            error_count += 1

                    # Summary log
                    if success_count > 0:
                        instance.add_to_log(f"[INFO] Successfully installed {success_count} addon(s)")
                    if warning_count > 0:
                        instance.add_to_log(f"[INFO] {warning_count} warning(s) encountered")
                    if error_count > 0:
                        instance.add_to_log(f"[INFO] {error_count} error(s) encountered")

                    _logger.info(
                        f"[LAUNCHLY_SAAS - {instance.name}] Install completed: {success_count} success, {warning_count} warnings, {error_count} errors")

                    # Clean up
                    if os.path.exists(script_path):
                        os.remove(script_path)

                    return error_count == 0  # Return True only if no errors occurred

                else:
                    instance.add_to_log(f"[ERROR] Script failed: {result.stderr}")
                    return False

            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error installing addons: {str(e)}")
                instance.add_to_log(f"[ERROR] Error installing addons: {str(e)}")
                return False

    def upgrade_custom_addon_in_odoo(self, addon_names):
        """Upgrade specific addons in the running Odoo instance via superuser script."""
        for instance in self:
            if instance.state != 'running':
                instance.add_to_log("[ERROR] Instance must be running to upgrade addons")
                return False

            try:
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Upgrading addons {addon_names} via superuser script")
                instance.add_to_log(f"[INFO] Upgrading addons {addon_names} via internal script...")

                addon_names_str = str(addon_names) if isinstance(addon_names, list) else str([addon_names])

                script_content = f'''#!/usr/bin/env python3
import os
import sys

# Add Odoo source path to Python path (from the source installation)
odoo_source_path = '{instance._get_source_path_from_template()}'
sys.path.insert(0, odoo_source_path)

import odoo
from odoo import api, SUPERUSER_ID

odoo.tools.config.parse_config(['-c', '/etc/{instance.name}.conf'])

try:
    from odoo.modules.registry import Registry
    registry = Registry.new('{instance.database_name}')

    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {{}})
        addon_names = {addon_names_str}

        for addon_name in addon_names:
            modules = env['ir.module.module'].search([('name', '=', addon_name)])

            if not modules:
                print("WARNING|Module '{{}}' not found".format(addon_name))
                continue

            module = modules[0]

            if module.state != 'installed':
                print("WARNING|Module '{{}}' is not installed and cannot be upgraded".format(addon_name))
                continue

            try:
                module.button_immediate_upgrade()
                print("SUCCESS|Module '{{}}' upgraded successfully".format(addon_name))
            except Exception as e:
                print("ERROR|Failed to upgrade module '{{}}': {{}}".format(addon_name, str(e)))

except Exception as e:
    print("ERROR|Script execution failed: {{}}".format(str(e)))
    '''

                script_path = os.path.join(instance.instance_data_path, "odoo_init", "upgrade_addons.py")
                instance.create_file(script_path, script_content)
                instance.chmod_with_sudo(script_path, 0o755)
                # Execute script directly using instance's Python environment
                venv_python = f"/opt/{instance.name}/venv/bin/python3"
                cmd = f"{venv_python} {script_path}"

                if instance.root_sudo_password:
                    result = instance.excute_command_with_sudo(cmd, shell=True, check=False)
                else:
                    result = instance.excute_command(cmd, shell=True, check=False)

                if result.returncode == 0:
                    output = result.stdout.strip().splitlines()
                    if not output:
                        instance.add_to_log("[ERROR] No output returned from script")
                        return False

                    success_count = 0
                    warning_count = 0
                    error_count = 0

                    for line in output:
                        if line.startswith("SUCCESS|"):
                            message = line.split("|", 1)[1]
                            instance.add_to_log(f"[SUCCESS] {message}")
                            success_count += 1
                        elif line.startswith("WARNING|"):
                            message = line.split("|", 1)[1]
                            instance.add_to_log(f"[WARNING] {message}")
                            warning_count += 1
                        elif line.startswith("ERROR|"):
                            message = line.split("|", 1)[1]
                            instance.add_to_log(f"[ERROR] {message}")
                            error_count += 1

                    if success_count > 0:
                        instance.add_to_log(f"[INFO] Successfully upgraded {success_count} addon(s)")
                    if warning_count > 0:
                        instance.add_to_log(f"[INFO] {warning_count} warning(s) encountered")
                    if error_count > 0:
                        instance.add_to_log(f"[INFO] {error_count} error(s) encountered")

                    _logger.info(
                        f"[LAUNCHLY_SAAS - {instance.name}] Upgrade completed: {success_count} success, {warning_count} warnings, {error_count} errors")

                    if os.path.exists(script_path):
                        os.remove(script_path)

                    return error_count == 0

                else:
                    instance.add_to_log(f"[ERROR] Script failed: {result.stderr}")
                    return False

            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error upgrading addons: {str(e)}")
                instance.add_to_log(f"[ERROR] Error upgrading addons: {str(e)}")
                return False

    def refresh_db_users(self):
        """Refresh the list of database users from inside the Odoo instance without requiring admin login"""
        for instance in self:
            if instance.state != 'running':
                instance.add_to_log("[ERROR] Instance must be running to refresh database users")
                return False

            try:
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Refreshing database users via ORM script")
                instance.add_to_log("[INFO] Refreshing database users via internal script...")

                script_content = f'''#!/usr/bin/env python3
import os
import sys

# Add Odoo source path to Python path (from the source installation)
odoo_source_path = '{instance._get_source_path_from_template()}'
sys.path.insert(0, odoo_source_path)

import odoo
from odoo import api, SUPERUSER_ID

# Configure Odoo
odoo.tools.config.parse_config(['-c', '/etc/{instance.name}.conf'])

try:
    from odoo.modules.registry import Registry
    registry = Registry.new('{instance.database_name}')

    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {{}})
        users = env['res.users'].search([])
        for user in users:
            print("USER|{{}}|{{}}|{{}}|{{}}|{{}}|{{}}|{{}}".format(
                user.id,
                user.login,
                user.email or '',
                user.phone or '',
                user.name,
                user.active,
                user.login_date or ''
            ))
except Exception as e:
    print("ERROR|{{}}".format(str(e)))
    '''

                script_path = os.path.join(instance.instance_data_path, "odoo_init", "refresh_users.py")
                instance.create_file(script_path, script_content)
                instance.chmod_with_sudo(script_path, 0o755)
                # Execute script directly using instance's Python environment
                venv_python = f"/opt/{instance.name}/venv/bin/python3"
                cmd = f"{venv_python} {script_path}"

                if instance.root_sudo_password:
                    result = instance.excute_command_with_sudo(cmd, shell=True, check=False)
                else:
                    result = instance.excute_command(cmd, shell=True, check=False)

                if result.returncode == 0:
                    output = result.stdout.strip().splitlines()
                    if not output:
                        instance.add_to_log("[ERROR] No output returned from script")
                        return False

                    # Clear existing db_users records for this instance
                    instance.db_users.unlink()

                    success_count = 0
                    for line in output:
                        if line.startswith("USER|"):
                            parts = line.split("|")
                            if len(parts) == 8:
                                login_date = None
                                if parts[7]:
                                    try:
                                        login_date = datetime.strptime(parts[7], "%Y-%m-%d %H:%M:%S.%f")
                                    except ValueError as e:
                                        instance.add_to_log(
                                            f"[WARNING] Failed to parse login_date for user {parts[2]}: {str(e)}")

                                instance.env['odoo.db.user'].create({
                                    'instance_id': instance.id,
                                    'user_id': int(parts[1]),
                                    'login': parts[2],
                                    'email': parts[3],
                                    'phone': parts[4],
                                    'name': parts[5],
                                    'active': parts[6] == 'True',
                                    'login_date': login_date,
                                    'current_password': '***HIDDEN***',
                                })
                                success_count += 1
                        elif line.startswith("ERROR|"):
                            instance.add_to_log(f"[ERROR] Script error: {line.split('|', 1)[1]}")
                            return False

                    instance.add_to_log(f"[SUCCESS] Refreshed {success_count} database users via script")
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Refreshed {success_count} users")

                    # Clean up
                    if os.path.exists(script_path):
                        os.remove(script_path)

                    return True

                else:
                    instance.add_to_log(f"[ERROR] Script failed: {result.stderr}")
                    return False

            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error refreshing users: {str(e)}")
                instance.add_to_log(f"[ERROR] Error refreshing users: {str(e)}")
                return False

    def change_user_password_with_sudo(self, user_login, new_password):
        """Change password for a specific user using sudo access to the database"""
        for instance in self:
            if instance.state != 'running':
                instance.add_to_log("[ERROR] Instance must be running to change user passwords")
                return False

            if not instance.root_sudo_password:
                instance.add_to_log("[ERROR] Root sudo password is required for this operation")
                return False

            try:
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Changing password for user: {user_login}")
                instance.add_to_log(f"[INFO] Changing password for user: {user_login}")

                # Create a Python script to change the password
                script_content = f'''#!/usr/bin/env python3
import os
import sys

# Add Odoo source path to Python path (from the source installation)
odoo_source_path = '{instance._get_source_path_from_template()}'
sys.path.insert(0, odoo_source_path)

import odoo
from odoo import api, SUPERUSER_ID

# Configure Odoo
odoo.tools.config.parse_config(['-c', '/etc/{instance.name}.conf'])

try:
    # Get registry
    from odoo.modules.registry import Registry
    registry = Registry.new('{instance.database_name}')

    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {{}})

        # Find the user
        user = env['res.users'].search([('login', '=', '{user_login}')], limit=1)

        if user:
            # Change password
            user.write({{'password': '{new_password}'}})
            cr.commit()
            print(f"SUCCESS: Password changed for user {{user.login}} ({{user.name}})")
        else:
            print(f"ERROR: User with login '{user_login}' not found")
            sys.exit(1)

except Exception as e:
    print(f"ERROR: {{str(e)}}")
    sys.exit(1)
'''

                # Write the script to a temporary file
                script_path = os.path.join(instance.instance_data_path, "odoo_init", "change_password.py")
                instance.create_file(script_path, script_content)
                instance.chmod_with_sudo(script_path, 0o755)

                # Execute script directly using instance's Python environment
                venv_python = f"/opt/{instance.name}/venv/bin/python3"
                cmd = f"{venv_python} {script_path}"

                if instance.root_sudo_password:
                    result = instance.excute_command_with_sudo(cmd, shell=True, check=False)
                else:
                    result = instance.excute_command(cmd, shell=True, check=False)

                if result.returncode == 0:
                    output = result.stdout if result.stdout else ''
                    if "SUCCESS:" in output:
                        instance.add_to_log(f"[SUCCESS] Password changed for user: {user_login}")
                        _logger.info(
                            f"[LAUNCHLY_SAAS - {instance.name}] Password changed successfully for user: {user_login}")

                        # Update the db_users record with new password
                        db_user = instance.db_users.filtered(lambda u: u.login == user_login)
                        if db_user:
                            db_user.current_password = new_password
                        if os.path.exists(script_path):
                            os.remove(script_path)
                        return True
                    else:
                        instance.add_to_log(f"[ERROR] Failed to change password: {output}")
                        return False
                else:
                    error_output = result.stderr if result.stderr else 'Unknown error'
                    instance.add_to_log(f"[ERROR] Password change failed: {error_output}")
                    return False
            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error changing user password: {str(e)}")
                instance.add_to_log(f"[ERROR] Error changing user password: {str(e)}")
                return False

    def count_active_users(self):
        """Count users that were active in the last 10 minutes"""
        for instance in self:
            try:
                instance.refresh_db_users()
                from datetime import datetime, timedelta
                import pytz
                now_utc = datetime.now(pytz.UTC)
                threshold_time = now_utc - timedelta(days=1)
                active_count = 0
                for user in instance.db_users:
                    if user.login_date and user.active:
                        if isinstance(user.login_date, str):
                            try:
                                if 'T' in user.login_date:
                                    if user.login_date.endswith('Z'):
                                        login_datetime = datetime.fromisoformat(user.login_date.replace('Z', '+00:00'))
                                    elif '+' in user.login_date or user.login_date.count(':') == 2:
                                        login_datetime = datetime.fromisoformat(user.login_date)
                                    else:
                                        login_datetime = datetime.fromisoformat(user.login_date + '+00:00')
                                else:
                                    login_datetime = datetime.strptime(user.login_date, '%Y-%m-%d %H:%M:%S')
                                    login_datetime = pytz.UTC.localize(login_datetime)
                            except (ValueError, TypeError):
                                continue
                        else:
                            login_datetime = user.login_date
                        if login_datetime.tzinfo is None:
                            login_datetime = pytz.UTC.localize(login_datetime)
                        elif login_datetime.tzinfo != pytz.UTC:
                            login_datetime = login_datetime.astimezone(pytz.UTC)
                        if login_datetime >= threshold_time:
                            active_count += 1
                instance.active_users_count = active_count
                instance.user_activity_status = 'active' if active_count > 0 else 'inactive'
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Active users count updated: {active_count}")
                return active_count
            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error counting active users: {str(e)}")
                instance.active_users_count = 0
                instance.user_activity_status = 'inactive'
                return 0

    def refresh_active_users(self):
        """Manual method to refresh active users count - callable from UI"""
        for instance in self:
            instance.count_active_users()
            instance.add_to_log(f"[INFO] Active users refreshed: {instance.active_users_count} users active")
        # return {
        #     'type': 'ir.actions.client',
        #     'tag': 'reload',
        # }

    @api.model
    def cron_check_active_users(self):
        """Cron job method to check active users for all running instances"""
        running_instances = self.search([('state', '=', 'running')])
        for instance in running_instances:
            try:
                instance.count_active_users()
                _logger.info(f"[LAUNCHLY_SAAS CRON] Checked active users for instance: {instance.name}")
            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS CRON] Error checking active users for {instance.name}: {str(e)}")

    @api.depends('subdomain_name')
    def _compute_domained_url(self):
        for instance in self:
            if instance.includes_subdomain and instance.subdomain_name:
                instance.domained_url = f"https://{instance.subdomain_name}.{instance.config_id.domain}"
            else:
                instance.domained_url = ""

    @api.depends('subdomain_name')
    def _compute_nginx_config_path(self):
        for instance in self:
            if instance.includes_subdomain and instance.subdomain_name:
                instance.nginx_config_path = f"/etc/nginx/sites-available/{instance.subdomain_name}.{instance.config_id.domain}"
            else:
                instance.nginx_config_path = ""

    @api.depends('subdomain_name')
    def _compute_ssl_certificate_expiry(self):
        for instance in self:
            if instance.includes_subdomain and instance.subdomain_name:
                try:
                    cert_path = f"/etc/letsencrypt/live/{instance.subdomain_name}.{instance.config_id.domain}/fullchain.pem"
                    if os.path.exists(cert_path):
                        # Get certificate expiry date using openssl
                        cmd = f"openssl x509 -enddate -noout -in {cert_path}"
                        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
                        if result.returncode == 0:
                            # Parse the output: notAfter=Jan 1 00:00:00 2025 GMT
                            output = result.stdout.strip()
                            if 'notAfter=' in output:
                                date_str = output.split('notAfter=')[1]
                                # Convert to datetime
                                expiry_date = datetime.strptime(date_str, '%b %d %H:%M:%S %Y %Z')
                                instance.ssl_certificate_expiry = expiry_date
                            else:
                                instance.ssl_certificate_expiry = False
                        else:
                            instance.ssl_certificate_expiry = False
                    else:
                        instance.ssl_certificate_expiry = False
                except Exception as e:
                    _logger.warning(f"[LAUNCHLY_SAAS - {instance.name}] Error reading SSL certificate expiry: {str(e)}")
                    instance.ssl_certificate_expiry = False
            else:
                instance.ssl_certificate_expiry = False

    @api.constrains('http_port')
    def _check_port_range(self):
        """Validate HTTP port range"""
        for record in self:
            if record.http_port:
                try:
                    port_num = int(record.http_port)
                    if port_num < 1024 or port_num > 65535:
                        raise ValidationError("HTTP port must be between 1024 and 65535")
                except ValueError:
                    raise ValidationError("HTTP port must be a valid number")

    @api.constrains('subdomain_name')
    def _check_subdomain_name(self):
        for instance in self:
            if instance.includes_subdomain and instance.subdomain_name:
                # Check subdomain format
                if not re.match(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$', instance.subdomain_name):
                    raise ValidationError(
                        "Subdomain name must contain only lowercase letters, numbers, and hyphens. "
                        "It cannot start or end with a hyphen and must be 1-63 characters long."
                    )

                # Check for uniqueness
                existing = self.search([
                    ('subdomain_name', '=', instance.subdomain_name),
                    ('includes_subdomain', '=', True),
                    ('id', '!=', instance.id)
                ])
                if existing:
                    raise ValidationError(
                        f"Subdomain '{instance.subdomain_name}' is already in use by another instance.")

    @api.onchange('includes_subdomain')
    def _onchange_includes_subdomain(self):
        if not self.includes_subdomain:
            self.subdomain_name = ""

    @api.onchange('company_name')
    def _onchange_company_name(self):
        if self.company_name and not self.subdomain_name:
            # Auto-generate subdomain from company name
            subdomain = re.sub(r'[^a-z0-9]', '', self.company_name.lower())
            if subdomain:
                self.subdomain_name = subdomain[:63]  # Limit to 63 characters

    def _create_subdomain_config(self):
        """Create Nginx configuration and SSL certificate for subdomain"""
        for instance in self:
            if not instance.includes_subdomain or not instance.subdomain_name:
                continue

            domain = f"{instance.subdomain_name}.{instance.config_id.domain}"
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Creating subdomain configuration for {domain}")
            instance.add_to_log(f"[INFO] Creating subdomain configuration for {domain}")

            try:
                # Create Nginx configuration (HTTP-only first, SSL added after certificate generation)
                nginx_config_http = f"""# HTTP server block for {domain}
server {{
    listen 80;
    server_name {domain};

    location / {{
        proxy_pass http://127.0.0.1:{instance.http_port};
        proxy_redirect http://{domain} http://{domain};
        proxy_redirect http://127.0.0.1:{instance.http_port} http://{domain};

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Port $server_port;
    }}
}}"""

                # Write HTTP-only Nginx configuration file first
                config_path = f"/etc/nginx/sites-available/{domain}"
                temp_config_path = f"/tmp/nginx_{domain}.conf"

                # First, write to a temporary file
                try:
                    with open(temp_config_path, 'w') as temp_file:
                        temp_file.write(nginx_config_http)
                    _logger.info(
                        f"[LAUNCHLY_SAAS - {instance.name}] Temporary HTTP config file created: {temp_config_path}")
                except Exception as temp_error:
                    raise Exception(f"Failed to create temporary config file: {str(temp_error)}")

                # Then move it to the final location with sudo
                move_config_cmd = f"mv {temp_config_path} {config_path}"
                instance.excute_command_with_sudo(move_config_cmd)

                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] HTTP Nginx configuration created: {config_path}")
                instance.add_to_log(f"[SUCCESS] HTTP Nginx configuration created: {config_path}")

                # Enable the site
                enable_cmd = f"ln -sf /etc/nginx/sites-available/{domain} /etc/nginx/sites-enabled/{domain}"

                try:
                    instance.excute_command_with_sudo(enable_cmd)
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Site enabled successfully")
                    instance.add_to_log(f"[SUCCESS] Site enabled successfully")
                except Exception as enable_error:
                    _logger.warning(f"[LAUNCHLY_SAAS - {instance.name}] Failed to enable site: {str(enable_error)}")
                    instance.add_to_log(f"[WARNING] Failed to enable site: {str(enable_error)}")

                # Test and reload Nginx with HTTP config
                try:
                    instance.excute_command_with_sudo("nginx -t")
                    instance.excute_command_with_sudo("systemctl reload nginx")
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Nginx reloaded successfully with HTTP config")
                    instance.add_to_log(f"[SUCCESS] Nginx reloaded successfully with HTTP config")
                except Exception as nginx_error:
                    _logger.error(
                        f"[LAUNCHLY_SAAS - {instance.name}] Nginx configuration test or reload failed: {str(nginx_error)}")
                    instance.add_to_log(f"[ERROR] Nginx configuration test or reload failed: {str(nginx_error)}")
                    # Don't raise exception here, continue with SSL generation

                # Now try to generate SSL certificate
                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Generating SSL certificate for {domain}")
                instance.add_to_log(f"[INFO] Generating SSL certificate for {domain}")

                certbot_cmd = f"certbot certonly --nginx -d {domain} --non-interactive --agree-tos --email {self.config_id.ssl_email}"

                ssl_success = False
                try:
                    instance.excute_command_with_sudo(certbot_cmd)
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] SSL certificate generated successfully")
                    instance.add_to_log(f"[SUCCESS] SSL certificate generated successfully")
                    ssl_success = True
                except Exception as cert_error:
                    _logger.warning(
                        f"[LAUNCHLY_SAAS - {instance.name}] SSL certificate generation failed: {str(cert_error)}")
                    instance.add_to_log(f"[WARNING] SSL certificate generation failed: {str(cert_error)}")
                    instance.add_to_log(f"[INFO] Subdomain will be available over HTTP only: http://{domain}")

                # If SSL was successful, update configuration to include HTTPS
                if ssl_success:
                    nginx_config_https = f"""# HTTP to HTTPS redirect
server {{
    listen 80;
    server_name {domain};
    return 301 https://{domain}$request_uri;
}}

# HTTPS server block
server {{
    listen 443 ssl;
    server_name {domain};

    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location / {{
        proxy_pass http://127.0.0.1:{instance.http_port};
        proxy_redirect http://{domain} https://{domain};
        proxy_redirect http://127.0.0.1:{instance.http_port} https://{domain};

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Port 443;

        add_header Content-Security-Policy "upgrade-insecure-requests" always;
    }}
}}"""

                    # Update to HTTPS configuration
                    try:
                        with open(temp_config_path, 'w') as temp_file:
                            temp_file.write(nginx_config_https)

                        move_config_cmd = f"mv {temp_config_path} {config_path}"
                        instance.excute_command_with_sudo(move_config_cmd)

                        # Test and reload with HTTPS config
                        instance.excute_command_with_sudo("nginx -t")
                        instance.excute_command_with_sudo("systemctl reload nginx")

                        _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] HTTPS configuration updated successfully")
                        instance.add_to_log(f"[SUCCESS] HTTPS configuration updated successfully")

                    except Exception as https_error:
                        _logger.warning(
                            f"[LAUNCHLY_SAAS - {instance.name}] Failed to update HTTPS config: {str(https_error)}")
                        instance.add_to_log(
                            f"[WARNING] Failed to update HTTPS config, keeping HTTP version: {str(https_error)}")

                # Enable the site
                enable_cmd = f"ln -sf /etc/nginx/sites-available/{domain} /etc/nginx/sites-enabled/{domain}"

                try:
                    instance.excute_command_with_sudo(enable_cmd)
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Site enabled successfully")
                    instance.add_to_log(f"[SUCCESS] Site enabled successfully")
                except Exception as enable_error:
                    _logger.warning(f"[LAUNCHLY_SAAS - {instance.name}] Failed to enable site: {str(enable_error)}")
                    instance.add_to_log(f"[WARNING] Failed to enable site: {str(enable_error)}")

                # Test and reload Nginx
                try:
                    instance.excute_command_with_sudo("nginx -t")
                    instance.excute_command_with_sudo("systemctl reload nginx")
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Nginx reloaded successfully")
                    instance.add_to_log(f"[SUCCESS] Nginx reloaded successfully")
                except Exception as nginx_error:
                    _logger.error(
                        f"[LAUNCHLY_SAAS - {instance.name}] Nginx configuration test or reload failed: {str(nginx_error)}")
                    instance.add_to_log(f"[ERROR] Nginx configuration test or reload failed: {str(nginx_error)}")

                # Refresh SSL certificate expiry
                instance._compute_ssl_certificate_expiry()

                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Subdomain configuration completed for {domain}")
                instance.add_to_log(f"[SUCCESS] Subdomain configuration completed: {domain}")

            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error creating subdomain configuration: {str(e)}")
                instance.add_to_log(f"[ERROR] Failed to create subdomain configuration: {str(e)}")

    def _remove_subdomain_config(self):
        """Remove Nginx configuration and SSL certificate for subdomain"""
        for instance in self:
            if not instance.subdomain_name:
                continue

            domain = f"{instance.subdomain_name}.{instance.config_id.domain}"
            _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Removing subdomain configuration for {domain}")
            instance.add_to_log(f"[INFO] Removing subdomain configuration for {domain}")

            try:
                # Disable the site
                disable_cmd = f"rm -f /etc/nginx/sites-enabled/{domain}"

                try:
                    instance.excute_command_with_sudo(disable_cmd)
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Site disabled successfully")
                    instance.add_to_log(f"[SUCCESS] Site disabled successfully")
                except Exception as disable_error:
                    _logger.warning(f"[LAUNCHLY_SAAS - {instance.name}] Failed to disable site: {str(disable_error)}")
                    instance.add_to_log(f"[WARNING] Failed to disable site: {str(disable_error)}")

                # Remove configuration file
                remove_config_cmd = f"rm -f /etc/nginx/sites-available/{domain}"

                try:
                    instance.excute_command_with_sudo(remove_config_cmd)
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Nginx configuration removed successfully")
                    instance.add_to_log(f"[SUCCESS] Nginx configuration removed successfully")
                except Exception as remove_error:
                    _logger.warning(f"[LAUNCHLY_SAAS - {instance.name}] Failed to remove config: {str(remove_error)}")
                    instance.add_to_log(f"[WARNING] Failed to remove config: {str(remove_error)}")

                # Remove SSL certificate
                remove_ssl_cmd = f"certbot delete --cert-name {domain} --non-interactive"

                try:
                    instance.excute_command_with_sudo(remove_ssl_cmd)
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] SSL certificate removed successfully")
                    instance.add_to_log(f"[SUCCESS] SSL certificate removed successfully")
                except Exception as ssl_error:
                    _logger.warning(
                        f"[LAUNCHLY_SAAS - {instance.name}] Failed to remove SSL certificate: {str(ssl_error)}")
                    instance.add_to_log(f"[WARNING] Failed to remove SSL certificate: {str(ssl_error)}")

                # Reload Nginx
                try:
                    instance.excute_command_with_sudo("systemctl reload nginx")
                    _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Nginx reloaded successfully")
                    instance.add_to_log(f"[SUCCESS] Nginx reloaded successfully")
                except Exception as reload_error:
                    _logger.warning(f"[LAUNCHLY_SAAS - {instance.name}] Failed to reload Nginx: {str(reload_error)}")
                    instance.add_to_log(f"[WARNING] Failed to reload Nginx: {str(reload_error)}")

                _logger.info(f"[LAUNCHLY_SAAS - {instance.name}] Subdomain configuration removed for {domain}")
                instance.add_to_log(f"[SUCCESS] Subdomain configuration removed: {domain}")

            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error removing subdomain configuration: {str(e)}")
                instance.add_to_log(f"[ERROR] Failed to remove subdomain configuration: {str(e)}")

    def open_domained_url(self):
        """Open the domained URL in a new tab"""
        for instance in self:
            if instance.domained_url:
                return {
                    'type': 'ir.actions.act_url',
                    'url': instance.domained_url,
                    'target': 'new',
                }

    def write(self, vals):
        allowed_users_old = {rec.id: rec.allowed_users_count for rec in self}
        # Store IDs instead of recordsets for correct comparison
        allowed_modules_old = {rec.id: set(rec.odoo_addon_line_ids.ids) for rec in self}
        res = super().write(vals)

        for rec in self:
            if 'allowed_users_count' in vals:
                rec.refresh_db_users()
                old = allowed_users_old.get(rec.id)
                new = rec.allowed_users_count
                if old != new:
                    rec._set_user_limit_in_instance()

            if 'odoo_addon_line_ids' in vals:
                rec.refresh_db_users()
                old_addons_ids = allowed_modules_old.get(rec.id, set())
                new_addons_ids = set(rec.odoo_addon_line_ids.ids)

                if old_addons_ids != new_addons_ids:
                    rec._set_module_limit_in_instance()

                    # Uninstall removed addons
                    removed_addon_ids = old_addons_ids - new_addons_ids
                    if removed_addon_ids:
                        removed_addons = self.env['odoo.addon.line'].browse(list(removed_addon_ids))
                        for addon in removed_addons:
                            rec.uninstall_addon_in_odoo([addon.name])

        return res

    def refresh_addons_list(self):
        """Refresh the list of all addons from the running Odoo instance."""
        for instance in self:
            if instance.state != 'running':
                instance.add_to_log("[ERROR] Instance must be running to refresh addons list")
                return False

            try:
                import os
                script_content = f'''#!/usr/bin/env python3
import os
import sys

# Add Odoo source path to Python path (from the source installation)
odoo_source_path = '{instance._get_source_path_from_template()}'
sys.path.insert(0, odoo_source_path)

import odoo
from odoo import api, SUPERUSER_ID
odoo.tools.config.parse_config(['-c', '/etc/{instance.name}.conf'])
try:
    from odoo.modules.registry import Registry
    registry = Registry.new('{instance.database_name}')
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {{}})
        modules = env['ir.module.module'].search([])
        for m in modules:
            print("ADDON|{{}}|{{}}|{{}}|{{}}|{{}}|{{}}|{{}}|{{}}".format(
                m.name,
                m.state,
                m.summary or '',
                m.latest_version or '',
                m.name,
                m.application,
                m.license or 'n/a',
                m.display_name or ''
            ))
except Exception as e:
    print("ERROR|{{}}".format(str(e)))
'''
                script_path = os.path.join(instance.instance_data_path, "odoo_init", "refresh_addons.py")
                instance.create_file(script_path, script_content)
                instance.chmod_with_sudo(script_path, 0o755)
                # Execute script directly using instance's Python environment
                venv_python = f"/opt/{instance.name}/venv/bin/python3"
                cmd = f"{venv_python} {script_path}"
                if instance.root_sudo_password:
                    result = instance.excute_command_with_sudo(cmd, shell=True, check=False)
                    instance.restart_odoo_service()

                else:
                    result = instance.excute_command(cmd, shell=True, check=False)
                if result.returncode == 0:
                    output = result.stdout.strip().splitlines()
                    if not output:
                        instance.add_to_log("[ERROR] No output returned from script")
                        return False

                    # Clear existing addon_line records for this instance
                    instance.addon_line.unlink()

                    # Dictionary to track addon states for custom addon line updates
                    addon_states = {}

                    for line in output:
                        if line.startswith("ADDON|"):
                            parts = line.split("|")
                            if len(parts) == 9:
                                addon_name = parts[1]
                                addon_state = parts[2]

                                # Store addon state for custom addon line updates
                                addon_states[addon_name] = addon_state

                                # Create odoo.addon.line record
                                instance.env['odoo.addon.line'].create({
                                    'instance_id': instance.id,
                                    'name': addon_name,
                                    'state': 'installed' if addon_state == 'installed' else 'uninstalled',
                                    'summary': parts[3],
                                    'latest_version': parts[4],
                                    'technical_name': parts[5],
                                    'application': parts[6].lower() == 'true',
                                    'license': parts[7],
                                    'display_name': parts[8],
                                })
                        elif line.startswith("ERROR|"):
                            instance.add_to_log(f"[ERROR] Script error: {line.split('|', 1)[1]}")
                            return False

                    # Update custom addon lines with real state from the running instance
                    for custom_addon in instance.custom_addon_line:
                        if custom_addon.addon_name in addon_states:
                            real_state = addon_states[custom_addon.addon_name]
                            if real_state == 'installed':
                                if custom_addon.state != 'installed':
                                    custom_addon.write({'state': 'installed'})
                                    instance.add_to_log(
                                        f"[INFO] Updated custom addon '{custom_addon.addon_name}' state to installed")
                            else:
                                # Addon is not installed (uninstalled, uninstallable, etc.)
                                if custom_addon.state == 'installed':
                                    # Determine appropriate state based on current state
                                    new_state = 'ready' if custom_addon.state in ['ready', 'uploaded'] else 'ready'
                                    custom_addon.write({'state': new_state})
                                    instance.add_to_log(
                                        f"[INFO] Updated custom addon '{custom_addon.addon_name}' state to {new_state} (was installed but now {real_state})")
                        else:
                            # Addon not found in the instance - might be a custom addon that's not yet applied
                            if custom_addon.state == 'installed':
                                custom_addon.write({'state': 'ready'})
                                instance.add_to_log(
                                    f"[INFO] Custom addon '{custom_addon.addon_name}' not found in instance, changed state from installed to ready")

                    instance.add_to_log("[SUCCESS] Refreshed addons list and updated custom addon states via script")
                    # Clean up
                    if os.path.exists(script_path):
                        os.remove(script_path)
                    return True
                else:
                    instance.add_to_log(f"[ERROR] Script failed: {result.stderr}")
                    return False
            except Exception as e:
                instance.add_to_log(f"[ERROR] Error refreshing addons: {str(e)}")
                return False

    def uninstall_addon_in_odoo(self, addon_names):
        """Uninstall specific addons in the running Odoo instance via superuser script."""
        for instance in self:
            if instance.state != 'running':
                instance.add_to_log("[ERROR] Instance must be running to uninstall addons")
                return False

            try:
                _logger.info(
                    f"[LAUNCHLY_SAAS - {instance.name}] Uninstalling addons {addon_names} via superuser script")
                instance.add_to_log(f"[INFO] Uninstalling addons {addon_names} via internal script...")

                # Convert addon_names to a Python list string for the script
                addon_names_str = str(addon_names) if isinstance(addon_names, list) else str([addon_names])

                script_content = f'''#!/usr/bin/env python3
import os
import sys

# Add Odoo source path to Python path (from the source installation)
odoo_source_path = '{instance._get_source_path_from_template()}'
sys.path.insert(0, odoo_source_path)

import odoo
from odoo import api, SUPERUSER_ID

# Configure Odoo
odoo.tools.config.parse_config(['-c', '/etc/{instance.name}.conf'])

try:
    from odoo.modules.registry import Registry
    registry = Registry.new('{instance.database_name}')

    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {{}})
        addon_names = {addon_names_str}

        for addon_name in addon_names:
            # Search for the module
            modules = env['ir.module.module'].search([('name', '=', addon_name)])

            if not modules:
                print("WARNING|Module '{{}}' not found".format(addon_name))
                continue

            module = modules[0]

            # Check if module is installed
            if module.state != 'installed':
                print("WARNING|Module '{{}}' is not installed (current state: {{}})".format(addon_name, module.state))
                continue

            # Uninstall the module
            try:
                module.button_immediate_uninstall()
                print("SUCCESS|Module '{{}}' uninstalled successfully".format(addon_name))
            except Exception as e:
                print("ERROR|Failed to uninstall module '{{}}': {{}}".format(addon_name, str(e)))
except Exception as e:
    print("ERROR|Script execution failed: {{}}".format(str(e)))
    '''

                script_path = os.path.join(instance.instance_data_path, "odoo_init", "uninstall_addons.py")
                instance.create_file(script_path, script_content)
                instance.chmod_with_sudo(script_path, 0o755)

                # Execute script directly using instance's Python environment
                venv_python = f"/opt/{instance.name}/venv/bin/python3"
                cmd = f"{venv_python} {script_path}"

                if instance.root_sudo_password:
                    result = instance.excute_command_with_sudo(cmd, shell=True, check=False)
                else:
                    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)

                if result.returncode == 0:
                    output = result.stdout.strip().splitlines()
                    if not output:
                        instance.add_to_log("[ERROR] No output returned from script")
                        return False

                    success_count = 0
                    warning_count = 0
                    error_count = 0

                    for line in output:
                        if line.startswith("SUCCESS|"):
                            message = line.split("|", 1)[1]
                            instance.add_to_log(f"[SUCCESS] {message}")
                            success_count += 1
                        elif line.startswith("WARNING|"):
                            message = line.split("|", 1)[1]
                            instance.add_to_log(f"[WARNING] {message}")
                            warning_count += 1
                        elif line.startswith("ERROR|"):
                            message = line.split("|", 1)[1]
                            instance.add_to_log(f"[ERROR] {message}")
                            error_count += 1

                    # Summary log
                    if success_count > 0:
                        instance.add_to_log(f"[INFO] Successfully uninstalled {success_count} addon(s)")
                    if warning_count > 0:
                        instance.add_to_log(f"[INFO] {warning_count} warning(s) encountered")
                    if error_count > 0:
                        instance.add_to_log(f"[INFO] {error_count} error(s) encountered")

                    _logger.info(
                        f"[LAUNCHLY_SAAS - {instance.name}] Uninstall completed: {success_count} success, {warning_count} warnings, {error_count} errors")

                    # Clean up
                    if os.path.exists(script_path):
                        os.remove(script_path)

                    return error_count == 0  # Return True only if no errors occurred

                else:
                    instance.add_to_log(f"[ERROR] Script failed: {result.stderr}")
                    return False

            except Exception as e:
                _logger.error(f"[LAUNCHLY_SAAS - {instance.name}] Error uninstalling addons: {str(e)}")
                instance.add_to_log(f"[ERROR] Error uninstalling addons: {str(e)}")
                return False

    def _set_user_limit_in_instance(self):
        """Set the user_limit_enforcer.allowed_users_count config parameter inside the managed instance's DB."""
        for instance in self:
            script_content = f'''#!/usr/bin/env python3
import os
import sys

# Add Odoo source path to Python path (from the source installation)
odoo_source_path = '{instance._get_source_path_from_template()}'
sys.path.insert(0, odoo_source_path)

import odoo
odoo.tools.config.parse_config(['-c', '/etc/{instance.name}.conf'])
from odoo import api, SUPERUSER_ID
registry = odoo.modules.registry.Registry('{instance.database_name}')
with registry.cursor() as cr:
    env = api.Environment(cr, SUPERUSER_ID, {{}})
    env['ir.config_parameter'].sudo().set_param(
        'user_limit_enforcer.allowed_users_count',
        '{instance.allowed_users_count}'
    )
    cr.commit()
    print("SUCCESS|Set user_limit_enforcer.allowed_users_count to {instance.allowed_users_count}")
'''
            script_path = os.path.join(instance.instance_data_path, "odoo_init", "set_user_limit.py")
            instance.create_file(script_path, script_content)
            instance.chmod_with_sudo(script_path, 0o755)
            # Execute script directly using instance's Python environment
            venv_python = f"/opt/{instance.name}/venv/bin/python3"
            cmd = f"{venv_python} {script_path}"
            if instance.root_sudo_password:
                result = instance.excute_command_with_sudo(cmd, shell=True, check=False)
            else:
                result = instance.excute_command(cmd, shell=True, check=False)
            if result.returncode == 0:
                output = result.stdout if result.stdout else ''
                if "SUCCESS|" in output:
                    instance.add_to_log(f"[SUCCESS] User limit set to {instance.allowed_users_count} in instance DB.")
                    instance.restart_odoo_service()

                else:
                    instance.add_to_log(f"[ERROR] User limit script output: {output}")
            else:
                error_output = result.stderr if result.stderr else 'Unknown error'
                instance.add_to_log(f"[ERROR] User limit script failed: {error_output}")

    def _set_module_limit_in_instance(self):
        """Set allowed module count and exact allowed module names in the instance DB."""
        for instance in self:
            # Get allowed technical module names from the Many2many field (odoo_addon_line_ids)
            allowed_names = ','.join(
                instance.odoo_addon_line_ids.mapped('name') +
                instance.custom_addon_line.mapped('addon_name')
            )
            # Python script to be executed directly on the host
            script_content = f'''#!/usr/bin/env python3
import os
import sys

# Add Odoo source path to Python path (from the source installation)
odoo_source_path = '{instance._get_source_path_from_template()}'
sys.path.insert(0, odoo_source_path)

import odoo
odoo.tools.config.parse_config(['-c', '/etc/{instance.name}.conf'])
from odoo import api, SUPERUSER_ID

registry = odoo.modules.registry.Registry('{instance.database_name}')
with registry.cursor() as cr:
    env = api.Environment(cr, SUPERUSER_ID, {{}})
    env['ir.config_parameter'].sudo().set_param(
        'module_install_limit.allowed_modules_count',
        '{instance.allowed_modules_count}'
    )
    env['ir.config_parameter'].sudo().set_param(
        'module_install_limit.allowed_module_names',
        "{allowed_names}"
    )
    cr.commit()
    print("SUCCESS|Module restrictions set")
    '''

            # Save script to instance path
            script_path = os.path.join(instance.instance_data_path, "odoo_init", "set_module_limit.py")
            instance.create_file(script_path, script_content)
            instance.chmod_with_sudo(script_path, 0o755)

            # Execute script directly using instance's Python environment
            venv_python = f"/opt/{instance.name}/venv/bin/python3"
            cmd = f"{venv_python} {script_path}"

            # Execute script directly on host
            if instance.root_sudo_password:
                result = instance.excute_command_with_sudo(cmd, shell=True, check=False)
            else:
                result = instance.excute_command(cmd, shell=True, check=False)

            # Log the result
            if result.returncode == 0:
                output = result.stdout or ''
                if "SUCCESS|" in output:
                    instance.add_to_log("[SUCCESS] Module limits set successfully in instance DB.")
                    instance.restart_odoo_service()
                else:
                    instance.add_to_log(f"[ERROR] Unexpected script output: {output}")
            else:
                error_output = result.stderr or 'Unknown error'
                instance.add_to_log(f"[ERROR] Script execution failed: {error_output}")




