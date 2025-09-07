import os
import subprocess
import tempfile
import zipfile
import shutil
from datetime import datetime
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import json
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
import smtplib
import logging

_logger = logging.getLogger(__name__)

class OdooInstanceBackup(models.Model):
    _name = 'odoo.instance.backup'
    _description = 'Odoo Instance Backup'

    config_id = fields.Many2one('saas.config', string='Configuration', required=True, default=lambda self: self.env['saas.config'].search([], limit=1))
    name = fields.Char(string='Backup Name', required=True, default=lambda self: _('Backup %s') % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    backup_date = fields.Datetime(string='Backup Date', default=fields.Datetime.now)
    # backup_file = fields.Binary(string='Backup File', readonly=True)  # Commented out: not storing backups in DB
    # backup_filename = fields.Char(string='Backup Filename', readonly=True)  # Commented out: not storing backups in DB
    status = fields.Selection([
        ('draft', 'Draft'),
        ('success', 'Success'),
        ('failed', 'Failed')
    ], string='Status', default='draft')
    log = fields.Text(string='Log', readonly=True)
    instance_ids = fields.Many2many('odoo.instance', string='Instances to Backup')
    backup_path = fields.Char(string='Backup Directory', required=True, help='Directory on the server where backup files will be saved')
    backup_full_path = fields.Char(string='Backup File Path', readonly=True)
    auto_remove = fields.Boolean(string='Remove Old Backups', help='Automatically remove old backups')
    days_to_remove = fields.Integer(string='Remove After (days)', help='Delete backups older than this number of days')
    backup_frequency = fields.Selection([
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
    ], default='daily', string='Backup Frequency', help='Frequency of Backup Scheduling')
    confirmation_code = fields.Char(string='Portal Confirmation Code')
    backup_file_snapshot = fields.Text(string='Backup File Snapshot')

    def action_backup_instances(self):
        import time
        for rec in self:
            logs = []
            backup_files = []
            for instance in rec.instance_ids:
                try:
                    # Direct systemd service backup (no odoo containers)
                    db_name = instance.database_name
                    # Database user is derived from company name (same as in installation script)
                    import re
                    company_name = instance.company_name or ""
                    db_user = re.sub(r'[^0-9A-Za-z]+', '', company_name).lower()
                    # Database password is standardized as 'adminpwd' in installation script
                    db_password = 'adminpwd'
                    # Filestore path for direct installation (include database name subfolder)
                    filestore_base_path = f'/opt/{instance.name}/data/filestore'
                    filestore_path = f'{filestore_base_path}/{db_name}'
                    now = datetime.now().strftime('%Y%m%d_%H%M%S')
                    backup_base = f"{instance.name}_backup_{now}"
                    backup_dir = rec.backup_path or '/tmp'
                    os.makedirs(backup_dir, exist_ok=True)
                    backup_format = 'zip'  # Only zip supported for now
                    with tempfile.TemporaryDirectory() as dump_dir:
                        # 1. Dump the database using direct PostgreSQL access
                        dump_sql_path = os.path.join(dump_dir, 'dump.sql')

                        # Set up environment with PostgreSQL password
                        env = os.environ.copy()
                        env['PGPASSWORD'] = db_password

                        dump_cmd = [
                            'pg_dump',
                            '-h', 'localhost',  # Explicit host
                            '-p', '5432',       # Explicit port
                            '-U', db_user,      # Database user
                            '-d', db_name,      # Database name
                            '--no-owner',       # Don't include ownership commands
                            '--no-privileges',  # Don't include privilege commands
                            '-F', 'p',          # Plain SQL format
                            '-f', dump_sql_path # Output file
                        ]

                        logs.append(f"Dumping database for {instance.name} directly from PostgreSQL...")
                        logs.append(f"Company name: '{company_name}' -> DB user: '{db_user}'")
                        logs.append(f"Using database user: {db_user}, database: {db_name}, password: adminpwd")
                        logs.append(f"Command: {' '.join(dump_cmd)}")

                        # Test database connection first
                        test_cmd = [
                            'psql',
                            '-h', 'localhost',
                            '-p', '5432',
                            '-U', db_user,
                            '-d', db_name,
                            '-c', 'SELECT version();'
                        ]

                        try:
                            test_result = subprocess.run(test_cmd, env=env, capture_output=True, text=True, timeout=30)
                            if test_result.returncode != 0:
                                logs.append(f"Database connection test failed: {test_result.stderr}")
                                logs.append(f"Test command: {' '.join(test_cmd)}")
                                raise Exception(f"Cannot connect to database {db_name} as user {db_user}")
                            else:
                                logs.append("Database connection test successful")
                        except subprocess.TimeoutExpired:
                            raise Exception("Database connection test timed out")

                        # Proceed with actual dump
                        try:
                            result = subprocess.run(dump_cmd, env=env, capture_output=True, text=True, timeout=300)
                            if result.returncode != 0:
                                logs.append(f"pg_dump failed with return code: {result.returncode}")
                                logs.append(f"pg_dump stderr: {result.stderr}")
                                logs.append(f"pg_dump stdout: {result.stdout}")
                                raise subprocess.CalledProcessError(result.returncode, dump_cmd, result.stdout, result.stderr)
                            else:
                                logs.append(f"Database dump completed successfully, size: {os.path.getsize(dump_sql_path)} bytes")
                        except subprocess.TimeoutExpired:
                            raise Exception("Database dump timed out after 5 minutes")
                        # 2. Copy the filestore using sudo if password available, otherwise try direct copy
                        filestore_tmp = os.path.join(dump_dir, 'filestore')
                        _logger.info(f"Starting filestore copy for {instance.name}")
                        _logger.info(f"Source path: {filestore_path}")
                        _logger.info(f"Destination path: {filestore_tmp}")
                        
                        # Create destination directory
                        os.makedirs(filestore_tmp, exist_ok=True)
                        
                        if instance.root_sudo_password:
                            # Use sudo from the beginning if we have the password
                            _logger.info(f"Using sudo to copy filestore for {instance.name}")
                            try:
                                # Check if database-specific filestore exists with sudo
                                sudo_test_cmd = ['sudo', '-S', 'test', '-d', filestore_path]
                                test_result = subprocess.run(
                                    sudo_test_cmd,
                                    input=instance.root_sudo_password + '\n',
                                    text=True,
                                    capture_output=True,
                                    timeout=30
                                )
                                
                                if test_result.returncode == 0:
                                    _logger.info(f"Database-specific filestore exists at {filestore_path}, copying with sudo...")
                                    
                                    # Copy with sudo (this will flatten the structure)
                                    sudo_copy_cmd = ['sudo', '-S', 'cp', '-r', f'{filestore_path}/.', filestore_tmp]
                                    copy_result = subprocess.run(
                                        sudo_copy_cmd,
                                        input=instance.root_sudo_password + '\n',
                                        text=True,
                                        capture_output=True,
                                        timeout=300
                                    )
                                    
                                    if copy_result.returncode != 0:
                                        raise Exception(f"Sudo copy failed: {copy_result.stderr}")
                                    
                                    _logger.info(f"Filestore copied successfully with sudo")
                    
                                else:
                                    # Check if base filestore directory exists and list contents
                                    _logger.info(f"Database-specific filestore not found, checking base filestore directory...")
                                    sudo_test_base_cmd = ['sudo', '-S', 'test', '-d', filestore_base_path]
                                    base_test_result = subprocess.run(
                                        sudo_test_base_cmd,
                                        input=instance.root_sudo_password + '\n',
                                        text=True,
                                        capture_output=True,
                                        timeout=30
                                    )
                                    
                                    if base_test_result.returncode == 0:
                                        # List contents of base filestore to see available databases
                                        sudo_ls_cmd = ['sudo', '-S', 'ls', '-la', filestore_base_path]
                                        ls_result = subprocess.run(
                                            sudo_ls_cmd,
                                            input=instance.root_sudo_password + '\n',
                                            text=True,
                                            capture_output=True,
                                            timeout=30
                                        )
                                        
                                        if ls_result.returncode == 0:
                                            _logger.info(f"Base filestore directory contents:\n{ls_result.stdout}")
                                        
                                        _logger.info(f"No filestore found for database '{db_name}' at {filestore_path}")
                                    else:
                                        _logger.info(f"Base filestore directory does not exist at {filestore_base_path}")
                                
                                # Fix ownership regardless of copy result
                                if os.path.exists(filestore_tmp) and os.listdir(filestore_tmp):
                                    import pwd
                                    current_user = pwd.getpwuid(os.getuid()).pw_name
                                    chown_cmd = ['sudo', '-S', 'chown', '-R', f'{current_user}:{current_user}', filestore_tmp]
                                    chown_result = subprocess.run(
                                        chown_cmd,
                                        input=instance.root_sudo_password + '\n',
                                        text=True,
                                        capture_output=True,
                                        timeout=60
                                    )
                                    
                                    if chown_result.returncode == 0:
                                        _logger.info(f"Fixed ownership of copied filestore files")
                                    else:
                                        _logger.info(f"Warning: Could not fix ownership: {chown_result.stderr}")
                                        
                            except subprocess.TimeoutExpired:
                                raise Exception("Filestore copy with sudo timed out")
                            except Exception as e:
                                _logger.info(f"Sudo copy failed: {str(e)}")
                                raise Exception(f"Failed to copy filestore: {str(e)}")
                        else:
                            # Try direct copy without sudo
                            _logger.info(f"No sudo password available, trying direct copy...")
                            try:
                                if os.path.exists(filestore_path):
                                    shutil.copytree(filestore_path, filestore_tmp, dirs_exist_ok=True)
                                    _logger.info(f"Filestore copied successfully without sudo")
                                else:
                                    _logger.info(f"Filestore does not exist at {filestore_path}")
                            except PermissionError:
                                raise Exception("Permission denied and no sudo password available")
                            except Exception as e:
                                raise Exception(f"Failed to copy filestore: {str(e)}")
                        # 3. Create manifest.json (literal copy of module logic)
                        manifest_path = os.path.join(dump_dir, 'manifest.json')
                        # Get installed modules from the DB using direct PostgreSQL access
                        try:
                            query_cmd = [
                                'psql',
                                '-h', 'localhost',
                                '-p', '5432',
                                '-U', db_user,
                                '-d', db_name,
                                '-t', '-c', "SELECT name, latest_version FROM ir_module_module WHERE state = 'installed';"
                            ]
                            result = subprocess.run(query_cmd, env=env, capture_output=True, text=True, timeout=60)
                            if result.returncode == 0:
                                modules = {}
                                for line in result.stdout.strip().split('\n'):
                                    if line.strip():
                                        parts = [p.strip() for p in line.split('|')]
                                        if len(parts) == 2:
                                            modules[parts[0]] = parts[1]
                            else:
                                logs.append(f"Could not fetch installed modules (psql failed): {result.stderr}")
                                modules = {}
                        except Exception as e:
                            logs.append(f"Could not fetch installed modules: {e}")
                            modules = {}
                        manifest = {
                            'odoo_dump': '1',
                            'db_name': db_name,
                            'version': '18.0',  # You can make this dynamic if needed
                            'version_info': [18, 0, 0, 'final', 0],
                            'major_version': '18.0',
                            'pg_version': '13',  # You can make this dynamic if needed
                            'modules': modules,
                        }
                        with open(manifest_path, 'w') as mf:
                            json.dump(manifest, mf, indent=4)
                        # 4. Zip the dump_dir (literal copy of module logic)
                        zip_filename = f'{backup_base}.zip'
                        zip_path = os.path.join(backup_dir, zip_filename)
                        logs.append(f"Zipping backup for {instance.name} at {zip_path} ...")
                        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                            for root, dirs, files in os.walk(dump_dir):
                                for file in files:
                                    abs_path = os.path.join(root, file)
                                    rel_path = os.path.relpath(abs_path, dump_dir)
                                    zf.write(abs_path, rel_path)
                        # Read zip as binary for UI download
                        with open(zip_path, 'rb') as f:
                            backup_data = f.read()
                        backup_files.append((zip_filename, backup_data, zip_path))
                        logs.append(f"Backup for {instance.name} complete at {zip_path}.")
                        # --- Per-instance backup retention logic ---
                        import fnmatch
                        pattern = f'{instance.name}_backup_*.zip'
                        files = [os.path.join(backup_dir, f) for f in os.listdir(backup_dir) if fnmatch.fnmatch(f, pattern)]
                        files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
                        keep_count = rec.days_to_remove if rec.days_to_remove > 0 else 1
                        for file_path in files[keep_count:]:
                            try:
                                os.remove(file_path)
                                logs.append(f"Removed old backup: {file_path}")
                            except Exception as e:
                                logs.append(f"Failed to remove backup {file_path}: {e}")
                except Exception as e:
                    logs.append(f"Backup failed for {instance.name}: {e}")
                    rec.status = 'failed'
                    rec.log = '\n'.join(logs)
                    return
            # If all backups succeeded, attach the first backup file (or all, if you want to extend)
            if backup_files:
                # rec.backup_file = backup_files[0][1]  # Commented out: not storing backups in DB
                # rec.backup_filename = backup_files[0][0]  # Commented out: not storing backups in DB
                rec.backup_full_path = backup_files[0][2]
                # Save all backup files to the backup_file field (commented out for now)
                # If you want to store all backups in the DB, uncomment the following lines:
                # import base64
                # for zip_filename, backup_data, zip_path in backup_files:
                #     rec.backup_file = base64.b64encode(backup_data)
                #     rec.backup_filename = zip_filename
                #     # For multiple files, consider using a one2many or ir.attachment
                # Remove old backups if enabled
                if rec.auto_remove and rec.days_to_remove and rec.backup_path:
                    now = time.time()
                    for filename in os.listdir(rec.backup_path):
                        file_path = os.path.join(rec.backup_path, filename)
                        if os.path.isfile(file_path):
                            file_age_days = (now - os.path.getctime(file_path)) / 86400
                            if file_age_days >= rec.days_to_remove:
                                os.remove(file_path)
                                logs.append(f"Removed old backup: {file_path}")
            else:
                rec.log = '\n'.join(logs)

    def action_show_backup_files(self):
        """
        Open a wizard to list all backup files for this backup's path and instances.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Backup Files',
            'res_model': 'instance.backup.file.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_backup_id': self.id,
            },
        }

    @api.model
    def cron_auto_backup(self, frequency='daily'):
        # Find all draft backup records with a backup_path set and matching frequency
        backups = self.search([
            ('status', '=', 'draft'),
            ('backup_path', '!=', False),
            ('backup_frequency', '=', frequency)
        ])
        for backup in backups:
            backup.action_backup_instances()

