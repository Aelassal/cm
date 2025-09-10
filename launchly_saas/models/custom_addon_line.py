import os
import logging
import base64
import zipfile
import tempfile
import shutil
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class CustomAddonLine(models.Model):
    _name = 'custom.addon.line'
    _description = 'Custom Addon Line'
    _rec_name = 'addon_name'

    instance_id = fields.Many2one('odoo.instance', string='Instance', required=True, ondelete='cascade')
    addon_name = fields.Char(string='Addon Name', readonly=True, help="Auto-detected from manifest file")
    
    # Upload methods
    upload_method = fields.Selection([
        ('file', 'Upload ZIP File'),
        ('folder', 'Upload Folder'),
        ('path', 'Server Path')
    ], string='Upload Method', default='path', required=True)
    
    # File upload (ZIP)
    addon_file = fields.Binary(string='Addon ZIP File', help="Upload addon as ZIP file")
    addon_filename = fields.Char(string='Filename')
    
    # Folder upload (multiple files)
    addon_folder_files = fields.One2many('custom.addon.file', 'addon_line_id', string='Addon Files')
    
    # Server path
    server_path = fields.Char(string='Server Path', help="Full path to addon folder on server")
    
    # Common fields
    addon_path = fields.Char(string='Local Path', readonly=True, help="Path where addon is stored")
    is_extracted = fields.Boolean(string='Ready', default=False, readonly=True)
    addon_version = fields.Char(string='Version', readonly=True, help="Auto-detected from manifest")
    addon_description = fields.Text(string='Description', readonly=True, help="Auto-detected from manifest")
    addon_author = fields.Char(string='Author', readonly=True, help="Auto-detected from manifest")
    addon_summary = fields.Char(string='Summary', readonly=True, help="Auto-detected from manifest")
    addon_depends = fields.Char(string='Dependencies', readonly=True, help="Auto-detected from manifest")
    last_updated = fields.Datetime(string='Last Updated', readonly=True, help="When the addon was last updated")
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('uploaded', 'Uploaded'),
        ('ready', 'Ready'),
        ('installed', 'Installed'),
        ('error', 'Error')
    ], string='State', default='draft')
    error_message = fields.Text(string='Error Message', readonly=True)
    is_installed = fields.Boolean(string='Is Installed', compute='_compute_is_installed', store=False)

    @api.model
    def create(self, vals):
        """Override create to handle different upload methods"""
        record = super().create(vals)
        # Only process addon if instance is not in draft state (already created)
        if record.instance_id.state != 'draft':
            record._process_addon()
        else:
            record.instance_id.add_to_log(f"[INFO] Custom addon '{record.addon_name or 'Unknown'}' added. Will be processed after instance creation.")
        return record

    def write(self, vals):
        """Override write to handle changes"""
        result = super().write(vals)
        
        # If upload method or content changed, reprocess
        if any(key in vals for key in ['upload_method', 'addon_file', 'server_path']):
            # If server_path changed and addon is already ready, ask user if they want to update
            if 'server_path' in vals and self.state in ['ready', 'installed']:
                self.instance_id.add_to_log(f"[INFO] Server path changed for addon '{self.addon_name}'. Use 'Update Code' or 'Sync with Server' to apply changes.")
            else:
                self._process_addon()
            
        return result

    def action_check_for_updates(self):
        """Check if updates are available from server path"""
        for record in self:
            if record.upload_method != 'path':
                raise UserError(_("Update checking is only available for addons uploaded via server path."))
            
            if not record.server_path or not os.path.exists(record.server_path):
                raise UserError(_("Server path is not accessible: %s") % record.server_path)
            
            try:
                record.instance_id.add_to_log(f"[INFO] Checking for updates for addon '{record.addon_name}'...")
                
                # Compare with server path
                differences = record._compare_with_server_path()
                
                if not differences:
                    record.instance_id.add_to_log(f"[INFO] No updates available. Addon is up to date.")
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'title': _('No Updates'),
                            'message': _('Addon is already up to date.'),
                            'type': 'info'
                        }
                    }
                
                # Show available updates
                record.instance_id.add_to_log(f"[INFO] Updates available! Found {len(differences)} changes:")
                for diff in differences[:10]:  # Show first 10 differences
                    record.instance_id.add_to_log(f"[INFO] - {diff}")
                if len(differences) > 10:
                    record.instance_id.add_to_log(f"[INFO] ... and {len(differences) - 10} more changes")
                
                record.instance_id.add_to_log(f"[INFO] Use 'Update Code' or 'Sync with Server' to apply these changes.")
                
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Updates Available'),
                        'message': _('Found %s changes. Check logs for details.') % len(differences),
                        'type': 'warning'
                    }
                }
                
            except Exception as e:
                error_msg = f"Failed to check for updates: {str(e)}"
                record.instance_id.add_to_log(f"[ERROR] {error_msg}")
                raise UserError(_(error_msg))

    def _get_custom_addons_directory(self):
        """Get the correct custom addons directory based on installation method"""
        # With bash script approach, custom addons are stored at /opt/{instance_name}/custom-addons/
        return f"/opt/{self.instance_id.name}/custom-addons"

    def _process_addon(self):
        """Process addon based on upload method"""
        try:
            if self.upload_method == 'file' and self.addon_file:
                self._process_zip_file()
            elif self.upload_method == 'folder' and self.addon_folder_files:
                self._process_folder_files()
            elif self.upload_method == 'path' and self.server_path:
                self._process_server_path()
        except Exception as e:
            self._set_error(str(e))

    def _process_zip_file(self):
        """Process uploaded ZIP file"""
        if not self.addon_file:
            return

        # Create instance addons directory
        addons_dir = self._get_custom_addons_directory()
        if not os.path.exists(addons_dir):
            # Use sudo to create directory if needed
            if self.instance_id.root_sudo_password:
                result = self.instance_id.excute_command_with_sudo(f"mkdir -p {addons_dir}")
                if result.returncode != 0:
                    raise Exception(f"Failed to create addons directory: {result.stderr}")
                # Set proper ownership
                self.instance_id.excute_command_with_sudo(f"chown -R {self.instance_id.name}:{self.instance_id.name} {addons_dir}")
            else:
                os.makedirs(addons_dir, exist_ok=True)

        # Decode and extract
        file_data = base64.b64decode(self.addon_file)
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.zip') as temp_file:
            temp_file.write(file_data)
            temp_file_path = temp_file.name

        try:
            with zipfile.ZipFile(temp_file_path, 'r') as zip_ref:
                # Check for manifest files
                file_list = zip_ref.namelist()
                manifest_files = [f for f in file_list if f.endswith('__manifest__.py') or f.endswith('__openerp__.py')]
                
                if not manifest_files:
                    raise UserError(_("Invalid addon: No manifest file found"))
                
                # Extract to temporary directory first
                temp_extract_dir = tempfile.mkdtemp()
                zip_ref.extractall(temp_extract_dir)
                
                # Find the actual addon directory
                addon_dir = self._find_addon_directory(temp_extract_dir)
                if not addon_dir:
                    raise UserError(_("Could not find addon directory with manifest file"))
                
                # Read manifest and get addon name
                manifest_data = self._read_manifest(addon_dir)
                addon_name = manifest_data.get('name', os.path.basename(addon_dir))
                
                # Final destination
                final_addon_path = os.path.join(addons_dir, addon_name.replace(' ', '_').lower())
                
                # Remove existing and copy new
                if os.path.exists(final_addon_path):
                    try:
                        shutil.rmtree(final_addon_path)
                    except PermissionError:
                        # Try with sudo if removal fails due to permissions
                        if self.instance_id.root_sudo_password:
                            self.instance_id.add_to_log(f"[INFO] Permission denied, using sudo to remove existing addon...")
                            self._sudo_remove_directory(final_addon_path)
                            _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Removed existing addon using sudo: {final_addon_path}")
                        else:
                            raise UserError(_("Permission denied removing existing addon and no sudo password available. Please provide sudo password in instance settings."))
                
                # Copy addon to final location (may need sudo for system directory)
                try:
                    shutil.copytree(addon_dir, final_addon_path)
                except PermissionError:
                    # Use sudo if permission denied
                    if self.instance_id.root_sudo_password:
                        self.instance_id.add_to_log(f"[INFO] Permission denied, using sudo to copy addon...")
                        self._sudo_copy_directory(addon_dir, final_addon_path)
                    else:
                        raise UserError(_("Permission denied copying addon and no sudo password available. Please provide sudo password in instance settings."))
                
                # Update record
                self._update_from_manifest(manifest_data, final_addon_path)
                
                # Clean up
                shutil.rmtree(temp_extract_dir)
                
        finally:
            os.unlink(temp_file_path)

    def _process_folder_files(self):
        """Process uploaded folder files"""
        if not self.addon_folder_files:
            return
            
        # Create temporary directory to reconstruct folder structure
        temp_dir = tempfile.mkdtemp()
        
        try:
            # Recreate folder structure from uploaded files
            for file_record in self.addon_folder_files:
                file_path = os.path.join(temp_dir, file_record.file_path)
                file_dir = os.path.dirname(file_path)
                
                if not os.path.exists(file_dir):
                    os.makedirs(file_dir, exist_ok=True)
                
                # Write file content
                if file_record.file_content:
                    file_data = base64.b64decode(file_record.file_content)
                    with open(file_path, 'wb') as f:
                        f.write(file_data)
            
            # Find addon directory and process
            addon_dir = self._find_addon_directory(temp_dir)
            if not addon_dir:
                raise UserError(_("Could not find addon directory with manifest file"))
            
            # Read manifest
            manifest_data = self._read_manifest(addon_dir)
            addon_name = manifest_data.get('name', 'custom_addon')
            
            # Copy to final location
            addons_dir = self._get_custom_addons_directory()
            if not os.path.exists(addons_dir):
                # Use sudo to create directory if needed
                if self.instance_id.root_sudo_password:
                    result = self.instance_id.excute_command_with_sudo(f"mkdir -p {addons_dir}")
                    if result.returncode != 0:
                        raise Exception(f"Failed to create addons directory: {result.stderr}")
                    # Set proper ownership
                    self.instance_id.excute_command_with_sudo(f"chown -R {self.instance_id.name}:{self.instance_id.name} {addons_dir}")
                else:
                    os.makedirs(addons_dir, exist_ok=True)
                
            final_addon_path = os.path.join(addons_dir, addon_name.replace(' ', '_').lower())
            
            if os.path.exists(final_addon_path):
                try:
                    shutil.rmtree(final_addon_path)
                except PermissionError:
                    # Try with sudo if removal fails due to permissions
                    if self.instance_id.root_sudo_password:
                        self.instance_id.add_to_log(f"[INFO] Permission denied, using sudo to remove existing addon...")
                        self._sudo_remove_directory(final_addon_path)
                        _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Removed existing addon using sudo: {final_addon_path}")
                    else:
                        raise UserError(_("Permission denied removing existing addon and no sudo password available. Please provide sudo password in instance settings."))
            
            # Copy addon to final location (may need sudo for system directory)
            try:
                shutil.copytree(addon_dir, final_addon_path)
            except PermissionError:
                # Use sudo if permission denied
                if self.instance_id.root_sudo_password:
                    self.instance_id.add_to_log(f"[INFO] Permission denied, using sudo to copy addon...")
                    self._sudo_copy_directory(addon_dir, final_addon_path)
                else:
                    raise UserError(_("Permission denied copying addon and no sudo password available. Please provide sudo password in instance settings."))
            
            # Update record
            self._update_from_manifest(manifest_data, final_addon_path)
            
        finally:
            shutil.rmtree(temp_dir)

    def _process_server_path(self):
        """Process addon from server path"""
        if not self.server_path or not os.path.exists(self.server_path):
            raise UserError(_("Server path does not exist: %s") % self.server_path)

        if not os.path.isdir(self.server_path):
            raise UserError(_("Server path is not a directory: %s") % self.server_path)

        # Check if it's a valid addon directory
        manifest_files = ['__manifest__.py', '__openerp__.py']
        manifest_path = None

        for manifest_file in manifest_files:
            full_path = os.path.join(self.server_path, manifest_file)
            if os.path.exists(full_path):
                manifest_path = full_path
                break

        if not manifest_path:
            raise UserError(_("No manifest file found in: %s") % self.server_path)

        # Read manifest
        manifest_data = self._read_manifest(self.server_path)
        addon_name = manifest_data.get('name', os.path.basename(self.server_path))

        # Copy to instance addons directory
        addons_dir = self._get_custom_addons_directory()
        if not os.path.exists(addons_dir):
            # Use sudo to create directory if needed
            if self.instance_id.root_sudo_password:
                result = self.instance_id.excute_command_with_sudo(f"mkdir -p {addons_dir}")
                if result.returncode != 0:
                    raise Exception(f"Failed to create addons directory: {result.stderr}")
                # Set proper ownership
                self.instance_id.excute_command_with_sudo(f"chown -R {self.instance_id.name}:{self.instance_id.name} {addons_dir}")
            else:
                os.makedirs(addons_dir, exist_ok=True)

        final_addon_path = os.path.join(addons_dir, addon_name.replace(' ', '_').lower())

        if os.path.exists(final_addon_path):
            try:
                shutil.rmtree(final_addon_path)
            except PermissionError:
                # Try with sudo if removal fails due to permissions
                if self.instance_id.root_sudo_password:
                    self.instance_id.add_to_log(f"[INFO] Permission denied, using sudo to remove existing addon...")
                    self._sudo_remove_directory(final_addon_path)
                    _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Removed existing addon using sudo: {final_addon_path}")
                else:
                    raise UserError(_("Permission denied removing existing addon and no sudo password available. Please provide sudo password in instance settings."))
        
        # Copy addon to final location (may need sudo for system directory)
        try:
            shutil.copytree(self.server_path, final_addon_path)
        except PermissionError:
            # Use sudo if permission denied
            if self.instance_id.root_sudo_password:
                self.instance_id.add_to_log(f"[INFO] Permission denied, using sudo to copy addon...")
                self._sudo_copy_directory(self.server_path, final_addon_path)
            else:
                raise UserError(_("Permission denied copying addon and no sudo password available. Please provide sudo password in instance settings."))

        # Update record
        self._update_from_manifest(manifest_data, final_addon_path)

    def _find_addon_directory(self, base_dir):
        """Find directory containing manifest file"""
        manifest_files = ['__manifest__.py', '__openerp__.py']
        
        # Check base directory first
        for manifest_file in manifest_files:
            if os.path.exists(os.path.join(base_dir, manifest_file)):
                return base_dir
        
        # Check subdirectories
        for item in os.listdir(base_dir):
            item_path = os.path.join(base_dir, item)
            if os.path.isdir(item_path):
                for manifest_file in manifest_files:
                    if os.path.exists(os.path.join(item_path, manifest_file)):
                        return item_path
        
        return None

    def _read_manifest(self, addon_path):
        """Read and parse manifest file"""
        manifest_files = ['__manifest__.py', '__openerp__.py']
        manifest_data = {}
        
        for manifest_file in manifest_files:
            manifest_path = os.path.join(addon_path, manifest_file)
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, 'r', encoding='utf-8') as f:
                        manifest_content = f.read()
                    
                    # Execute manifest file to get data
                    exec(compile(manifest_content, manifest_path, 'exec'), {}, manifest_data)
                    break
                    
                except Exception as e:
                    _logger.warning(f"Could not parse manifest {manifest_path}: {str(e)}")
        
        return manifest_data

    def _update_from_manifest(self, manifest_data, addon_path):
        """Update record fields from manifest data"""
        # Get addon technical name from path as fallback
        addon_technical_name = os.path.basename(addon_path)
        
        # Use manifest name if available, otherwise use technical name
        addon_name = manifest_data.get('name', addon_technical_name)
        if not addon_name or addon_name == 'Unknown Addon':
            addon_name = addon_technical_name
        
        update_vals = {
            'addon_path': addon_path,
            'is_extracted': True,
            'state': 'ready',
            'error_message': False,
            'addon_name': addon_name,
            'addon_version': manifest_data.get('version', ''),
            'addon_author': manifest_data.get('author', ''),
            'addon_description': manifest_data.get('description', ''),
            'addon_summary': manifest_data.get('summary', ''),
            'addon_depends': ', '.join(manifest_data.get('depends', [])) if manifest_data.get('depends') else '',
            'last_updated': fields.Datetime.now()
        }
        
        self.write(update_vals)
        
        _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Custom addon ready: {update_vals['addon_name']} at {addon_path}")
        
        # Apply changes if instance is not in draft state
        if self.instance_id.state != 'draft':
            self.instance_id.add_to_log(f"[INFO] New custom addon '{update_vals['addon_name']}' is ready - applying changes...")
            self._apply_addon_changes()

    def _set_error(self, error_msg):
        """Set error state"""
        self.write({
            'state': 'error',
            'error_message': error_msg,
            'is_extracted': False
        })
        _logger.error(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Custom addon error: {error_msg}")

    def action_remove_addon(self):
        """Remove addon files and record using sudo if needed"""
        for record in self:
            try:
                if record.addon_path and os.path.exists(record.addon_path):
                    # Try normal removal first
                    try:
                        shutil.rmtree(record.addon_path)
                        _logger.info(f"[LAUNCHLY_SAAS - {record.instance_id.name}] Removed custom addon directory: {record.addon_path}")
                    except PermissionError as pe:
                        # Permission denied - try with sudo if password is available
                        if record.instance_id.root_sudo_password:
                            _logger.info(f"[LAUNCHLY_SAAS - {record.instance_id.name}] Permission denied, trying with sudo...")
                            record.instance_id.add_to_log(f"[INFO] Permission denied, using sudo to remove addon '{record.addon_name}'...")
                            
                            try:
                                import subprocess
                                # Use sudo to change ownership and then remove
                                sudo_commands = [
                                    f"sudo -S chown -R {os.getuid()}:{os.getgid()} {record.addon_path}",
                                    f"sudo -S chmod -R 755 {record.addon_path}",
                                    f"sudo -S rm -rf {record.addon_path}"
                                ]
                                
                                for cmd in sudo_commands:
                                    _logger.info(f"[LAUNCHLY_SAAS - {record.instance_id.name}] Executing: {cmd}")
                                    result = subprocess.run(
                                        cmd,
                                        shell=True,
                                        input=record.instance_id.root_sudo_password + '\n',
                                        text=True,
                                        capture_output=True,
                                        timeout=30
                                    )
                                    
                                    if result.returncode != 0:
                                        _logger.warning(f"[LAUNCHLY_SAAS - {record.instance_id.name}] Sudo command failed: {cmd}, Error: {result.stderr}")
                                        raise Exception(f"Sudo command failed: {result.stderr}")
                                    else:
                                        _logger.info(f"[LAUNCHLY_SAAS - {record.instance_id.name}] Sudo command successful: {cmd}")
                                
                                _logger.info(f"[LAUNCHLY_SAAS - {record.instance_id.name}] Custom addon removed using sudo: {record.addon_path}")
                                record.instance_id.add_to_log(f"[SUCCESS] Custom addon '{record.addon_name}' removed using sudo")
                                
                            except Exception as sudo_error:
                                _logger.error(f"[LAUNCHLY_SAAS - {record.instance_id.name}] Sudo removal failed: {str(sudo_error)}")
                                raise UserError(_(f"Failed to remove addon with sudo: {str(sudo_error)}"))
                        else:
                            # No sudo password available
                            _logger.error(f"[LAUNCHLY_SAAS - {record.instance_id.name}] Permission denied and no sudo password available")
                            raise UserError(_("Permission denied: Cannot remove files created by odoo . Please provide sudo password in instance settings."))
                
                # Remove the record
                record.unlink()
                
            except Exception as e:
                if "Permission denied" not in str(e):
                    error_msg = f"Failed to remove addon: {str(e)}"
                    _logger.error(f"[LAUNCHLY_SAAS - {record.instance_id.name}] Error removing custom addon: {str(e)}")
                    raise UserError(_(error_msg))
                else:
                    raise  # Re-raise permission errors as they are already handled

    def action_reinstall_addon(self):
        """Reinstall addon by reprocessing"""
        for record in self:
            record._process_addon()

    def _apply_addon_changes(self):
        """Apply addon changes to the instance"""
        try:
            # Update addons path computation
            self.instance_id._compute_addons_path()
            
            # Update odoo.conf with new addons path
            self.instance_id._update_odoo_conf_addons_path()
            
            # If instance is running, offer to restart or update addons list
            if self.instance_id.state == 'running':
                self.instance_id.add_to_log(f"[INFO] Custom addon '{self.addon_name}' is ready. You can:")
                self.instance_id.add_to_log("[INFO] 1. Use 'Update Addons List' to refresh available addons")
                self.instance_id.add_to_log("[INFO] 2. Use 'Apply Custom Addons Changes' to restart Odoo service with new addons")
                self.instance_id.add_to_log("[INFO] 3. Install the addon from Apps menu in your Odoo instance")
            else:
                self.instance_id.add_to_log(f"[INFO] Custom addon '{self.addon_name}' is ready. Start the instance to use it.")
                
        except Exception as e:
            _logger.error(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Error applying addon changes: {str(e)}")
            self.instance_id.add_to_log(f"[ERROR] Error applying addon changes: {str(e)}")

    def action_apply_addon_changes(self):
        """Manual action to apply addon changes"""
        for record in self:
            if record.instance_id.state == 'draft':
                raise UserError(_("Instance is in draft state. Please create odoo environment first."))
            
            record.instance_id.apply_custom_addons_changes()
        
        # return {
        #     'type': 'ir.actions.client',
        #     'tag': 'reload',
        # }

    def action_install_addon(self):
        """Install addon in the running Odoo instance via API"""
        for record in self:
            if record.instance_id.state != 'running':
                raise UserError(_("Instance must be running to install addons. Please start the instance first."))

            if not record.is_extracted or record.state != 'ready':
                raise UserError(
                    _("Addon is not ready for installation. Please ensure the addon is properly uploaded and extracted."))

            try:
                record.instance_id.install_custom_addon_in_odoo([record.addon_name])
                record.instance_id.add_to_log(f"[SUCCESS] Addon '{record.addon_name}' installed successfully!")
                record.write({'state': 'installed'})
            except Exception as e:
                error_message = f"Failed to install addon '{record.addon_name}': {str(e)}"
                record.instance_id.add_to_log(f"[ERROR] {error_message}")
                record.state = 'error'
                record.error_message = error_message
                _logger.error(f"[LAUNCHLY_SAAS - {record.instance_id.name}] {error_message}")

    def action_uninstall_addon(self):
        """Uninstall addon from the running Odoo instance via API"""
        for record in self:
            if record.instance_id.state != 'running':
                raise UserError(_("Instance must be running to uninstall addons. Please start the instance first."))

            if record.state != 'installed':
                raise UserError(_("Addon is not installed. Only installed addons can be uninstalled."))

            try:
                _logger.info(
                    f"[LAUNCHLY_SAAS - {record.instance_id.name}] Uninstalling addon '{record.addon_name}' via API")
                record.instance_id.add_to_log(f"[INFO] Uninstalling addon '{record.addon_name}' via Odoo API...")

                # Call the uninstall method
                record.instance_id.uninstall_addon_in_odoo([record.addon_name])

                # Log success and update state
                record.instance_id.add_to_log(f"[SUCCESS] Addon '{record.addon_name}' uninstalled successfully!")
                _logger.info(
                    f"[LAUNCHLY_SAAS - {record.instance_id.name}] Addon '{record.addon_name}' uninstalled successfully")
                record.write({'state': 'ready'})


            except Exception as e:
                error_msg = f"Failed to uninstall addon '{record.addon_name}': {str(e)}"
                _logger.error(f"[LAUNCHLY_SAAS - {record.instance_id.name}] {error_msg}")
                record.instance_id.add_to_log(f"[ERROR] {error_msg}")

    def action_upgrade_addon(self):
        """Upgrade addon in the running Odoo instance via API"""
        for record in self:
            if record.instance_id.state != 'running':
                raise UserError(_("Instance must be running to upgrade addons. Please start the instance first."))

            if record.state != 'installed':
                raise UserError(_("Addon is not installed. Only installed addons can be upgraded."))

            try:
                _logger.info(f"[LAUNCHLY_SAAS - {record.instance_id.name}] Upgrading addon '{record.addon_name}' via API")
                record.instance_id.add_to_log(f"[INFO] Upgrading addon '{record.addon_name}' via Odoo API...")

                # Call the upgrade method
                record.instance_id.upgrade_custom_addon_in_odoo([record.addon_name])

                # Log success and update state
                record.instance_id.add_to_log(f"[SUCCESS] Addon '{record.addon_name}' upgraded successfully!")
                _logger.info(
                    f"[LAUNCHLY_SAAS - {record.instance_id.name}] Addon '{record.addon_name}' upgraded successfully")
                record.write({'state': 'installed'})

            except Exception as e:
                error_msg = f"Failed to upgrade addon '{record.addon_name}': {str(e)}"
                _logger.error(f"[LAUNCHLY_SAAS - {record.instance_id.name}] {error_msg}")
                record.instance_id.add_to_log(f"[ERROR] {error_msg}")
                record.state = 'error'
                record.error_message = error_msg

    def action_update_addon_code(self):
        """Update addon code from server path or re-upload"""
        for record in self:
            if not record.addon_path or not os.path.exists(record.addon_path):
                raise UserError(_("Addon path not found. Please ensure the addon is properly uploaded first."))
            
            try:
                record.instance_id.add_to_log(f"[INFO] Updating addon '{record.addon_name}' code...")
                
                # Store current installation state
                was_installed = record.state == 'installed'
                
                # Update based on upload method
                if record.upload_method == 'path' and record.server_path:
                    record._update_from_server_path()
                elif record.upload_method == 'file' and record.addon_file:
                    record._process_zip_file()
                elif record.upload_method == 'folder' and record.addon_folder_files:
                    record._process_folder_files()
                else:
                    raise UserError(_("No source available for update. Please ensure upload method and source are properly configured."))
                
                # If addon was installed, upgrade it in Odoo
                if was_installed and record.instance_id.state == 'running':
                    record.instance_id.add_to_log(f"[INFO] Addon was installed, upgrading in Odoo...")
                    record.instance_id.upgrade_custom_addon_in_odoo([record.addon_name])
                    record.write({'state': 'installed'})
                
                record.instance_id.add_to_log(f"[SUCCESS] Addon '{record.addon_name}' code updated successfully!")
                
            except Exception as e:
                error_msg = f"Failed to update addon code: {str(e)}"
                record._set_error(error_msg)
                record.instance_id.add_to_log(f"[ERROR] {error_msg}")
                raise UserError(_(error_msg))

    def _update_from_server_path(self):
        """Update addon from server path with backup and merge capability"""
        if not self.server_path or not os.path.exists(self.server_path):
            raise UserError(_("Server path does not exist: %s") % self.server_path)

        if not os.path.isdir(self.server_path):
            raise UserError(_("Server path is not a directory: %s") % self.server_path)

        # Validate source has manifest
        manifest_files = ['__manifest__.py', '__openerp__.py']
        source_manifest_path = None
        for manifest_file in manifest_files:
            full_path = os.path.join(self.server_path, manifest_file)
            if os.path.exists(full_path):
                source_manifest_path = full_path
                break

        if not source_manifest_path:
            raise UserError(_("No manifest file found in server path: %s") % self.server_path)

        # Read source manifest
        source_manifest_data = self._read_manifest(self.server_path)
        
        # Create backup of current addon
        backup_path = None
        if self.addon_path and os.path.exists(self.addon_path):
            backup_path = f"{self.addon_path}_backup_{fields.Datetime.now().strftime('%Y%m%d_%H%M%S')}"
            try:
                shutil.copytree(self.addon_path, backup_path)
                _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Created backup: {backup_path}")
            except PermissionError:
                # Try with sudo if backup fails due to permissions
                if self.instance_id.root_sudo_password:
                    self._sudo_copy_directory(self.addon_path, backup_path)
                    _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Created backup using sudo: {backup_path}")
                else:
                    raise UserError(_("Permission denied creating backup and no sudo password available. Please provide sudo password in instance settings."))

        try:
            # Remove current addon directory
            if self.addon_path and os.path.exists(self.addon_path):
                try:
                    shutil.rmtree(self.addon_path)
                except PermissionError:
                    # Try with sudo if removal fails due to permissions
                    if self.instance_id.root_sudo_password:
                        self.instance_id.add_to_log(f"[INFO] Permission denied, using sudo to remove addon directory...")
                        self._sudo_remove_directory(self.addon_path)
                        _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Removed addon directory using sudo: {self.addon_path}")
                    else:
                        raise UserError(_("Permission denied removing addon directory and no sudo password available. Please provide sudo password in instance settings."))

            # Copy updated addon from server path
            addon_name = source_manifest_data.get('name', os.path.basename(self.server_path))
            addons_dir = self._get_custom_addons_directory()
            final_addon_path = os.path.join(addons_dir, addon_name.replace(' ', '_').lower())
            
            # Ensure addons directory exists
            if not os.path.exists(addons_dir):
                # Use sudo to create directory if needed
                if self.instance_id.root_sudo_password:
                    result = self.instance_id.excute_command_with_sudo(f"mkdir -p {addons_dir}")
                    if result.returncode != 0:
                        raise Exception(f"Failed to create addons directory: {result.stderr}")
                    # Set proper ownership
                    self.instance_id.excute_command_with_sudo(f"chown -R {self.instance_id.name}:{self.instance_id.name} {addons_dir}")
                else:
                    os.makedirs(addons_dir, exist_ok=True)
            
            # Copy addon to final location (may need sudo for system directory)
            try:
                shutil.copytree(self.server_path, final_addon_path)
            except PermissionError:
                # Use sudo if permission denied
                if self.instance_id.root_sudo_password:
                    self.instance_id.add_to_log(f"[INFO] Permission denied, using sudo to copy addon...")
                    self._sudo_copy_directory(self.server_path, final_addon_path)
                else:
                    raise UserError(_("Permission denied copying addon and no sudo password available. Please provide sudo password in instance settings."))
            
            # Update record with new manifest data
            self._update_from_manifest(source_manifest_data, final_addon_path)
            
            # Clean up backup if update successful
            if backup_path and os.path.exists(backup_path):
                try:
                    shutil.rmtree(backup_path)
                    _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Removed backup after successful update")
                except PermissionError:
                    # Try with sudo to remove backup
                    if self.instance_id.root_sudo_password:
                        self._sudo_remove_directory(backup_path)
                        _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Removed backup using sudo after successful update")
                    else:
                        _logger.warning(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Could not remove backup due to permissions: {backup_path}")
            
            _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Successfully updated addon from server path: {self.server_path}")
            
        except Exception as e:
            # Restore from backup if update failed
            if backup_path and os.path.exists(backup_path):
                try:
                    if self.addon_path and os.path.exists(self.addon_path):
                        shutil.rmtree(self.addon_path)
                    shutil.move(backup_path, self.addon_path)
                    _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Restored addon from backup due to error")
                except PermissionError:
                    # Try with sudo to restore backup
                    if self.instance_id.root_sudo_password:
                        self.instance_id.add_to_log(f"[INFO] Using sudo to restore backup...")
                        if self.addon_path and os.path.exists(self.addon_path):
                            self._sudo_remove_directory(self.addon_path)
                        self._sudo_move_directory(backup_path, self.addon_path)
                        _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Restored addon from backup using sudo due to error")
                    else:
                        _logger.error(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Could not restore backup due to permissions and no sudo password")
            raise e

    def _sudo_remove_directory(self, directory_path):
        """Remove directory using sudo"""
        try:
            import subprocess
            
            sudo_commands = [
                f"sudo -S chown -R {os.getuid()}:{os.getgid()} {directory_path}",
                f"sudo -S chmod -R 755 {directory_path}",
                f"sudo -S rm -rf {directory_path}"
            ]
            
            for cmd in sudo_commands:
                _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Executing: {cmd}")
                result = subprocess.run(
                    cmd,
                    shell=True,
                    input=self.instance_id.root_sudo_password + '\n',
                    text=True,
                    capture_output=True,
                    timeout=30
                )
                
                if result.returncode != 0:
                    _logger.warning(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Sudo command failed: {cmd}, Error: {result.stderr}")
                    raise Exception(f"Sudo command failed: {result.stderr}")
                else:
                    _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Sudo command successful: {cmd}")
                    
        except Exception as e:
            _logger.error(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Sudo removal failed: {str(e)}")
            raise Exception(f"Failed to remove directory with sudo: {str(e)}")

    def _sudo_copy_directory(self, source_path, dest_path):
        """Copy directory using sudo to system location"""
        try:
            import subprocess
            
            # Use sudo cp command for system directories
            cmd = f"cp -r {source_path} {dest_path}"
            result = self.instance_id.excute_command_with_sudo(cmd)
            
            if result.returncode != 0:
                raise Exception(f"Sudo copy failed: {result.stderr}")
            
            # Set proper ownership to the instance user
            chown_cmd = f"chown -R {self.instance_id.name}:{self.instance_id.name} {dest_path}"
            chown_result = self.instance_id.excute_command_with_sudo(chown_cmd)
            
            if chown_result.returncode != 0:
                _logger.warning(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Failed to set ownership: {chown_result.stderr}")
                # Don't fail the operation, just warn
            
            _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Successfully copied directory with sudo: {source_path} -> {dest_path}")
                
        except Exception as e:
            _logger.error(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Sudo copy failed: {str(e)}")
            raise Exception(f"Failed to copy directory with sudo: {str(e)}")

    def _sudo_move_directory(self, source_path, dest_path):
        """Move directory using sudo"""
        try:
            import subprocess
            
            cmd = f"sudo -S mv {source_path} {dest_path}"
            _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Executing: {cmd}")
            result = subprocess.run(
                cmd,
                shell=True,
                input=self.instance_id.root_sudo_password + '\n',
                text=True,
                capture_output=True,
                timeout=30
            )
            
            if result.returncode != 0:
                _logger.warning(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Sudo command failed: {cmd}, Error: {result.stderr}")
                raise Exception(f"Sudo command failed: {result.stderr}")
            else:
                _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Sudo command successful: {cmd}")
                
            # Fix ownership after move
            cmd = f"sudo -S chown -R {os.getuid()}:{os.getgid()} {dest_path}"
            _logger.info(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Executing: {cmd}")
            result = subprocess.run(
                cmd,
                shell=True,
                input=self.instance_id.root_sudo_password + '\n',
                text=True,
                capture_output=True,
                timeout=30
            )
            
            if result.returncode != 0:
                _logger.warning(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Sudo ownership change failed: {cmd}, Error: {result.stderr}")
                # Don't raise exception here as the move was successful
                
        except Exception as e:
            _logger.error(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Sudo move failed: {str(e)}")
            raise Exception(f"Failed to move directory with sudo: {str(e)}")

    def action_sync_with_server_path(self):
        """Sync addon with server path - merge changes intelligently"""
        for record in self:
            if record.upload_method != 'path':
                raise UserError(_("This action is only available for addons uploaded via server path."))
            
            if not record.server_path or not os.path.exists(record.server_path):
                raise UserError(_("Server path is not accessible: %s") % record.server_path)
            
            try:
                record.instance_id.add_to_log(f"[INFO] Syncing addon '{record.addon_name}' with server path...")
                
                # Check if there are differences
                differences = record._compare_with_server_path()
                
                if not differences:
                    record.instance_id.add_to_log(f"[INFO] No differences found. Addon is already up to date.")
                    return
                
                # Show differences in log
                record.instance_id.add_to_log(f"[INFO] Found {len(differences)} differences:")
                for diff in differences[:5]:  # Show first 5 differences
                    record.instance_id.add_to_log(f"[INFO] - {diff}")
                if len(differences) > 5:
                    record.instance_id.add_to_log(f"[INFO] ... and {len(differences) - 5} more differences")
                
                # Perform the sync
                record._update_from_server_path()
                
                record.instance_id.add_to_log(f"[SUCCESS] Addon '{record.addon_name}' synced successfully!")
                
            except Exception as e:
                error_msg = f"Failed to sync addon: {str(e)}"
                record.instance_id.add_to_log(f"[ERROR] {error_msg}")
                raise UserError(_(error_msg))

    def _compare_with_server_path(self):
        """Compare current addon with server path and return list of differences"""
        if not self.server_path or not self.addon_path:
            return []
        
        differences = []
        
        try:
            import filecmp
            
            # Compare directories
            dcmp = filecmp.dircmp(self.addon_path, self.server_path)
            
            # Files only in server path (new files)
            for file in dcmp.right_only:
                differences.append(f"New file: {file}")
            
            # Files only in current addon (deleted files)
            for file in dcmp.left_only:
                differences.append(f"Deleted file: {file}")
            
            # Different files
            for file in dcmp.diff_files:
                differences.append(f"Modified file: {file}")
            
            # Recursively check subdirectories
            for subdir in dcmp.common_dirs:
                subdiff = self._compare_subdirectories(
                    os.path.join(self.addon_path, subdir),
                    os.path.join(self.server_path, subdir),
                    subdir
                )
                differences.extend(subdiff)
                
        except Exception as e:
            _logger.warning(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Error comparing directories: {str(e)}")
            differences.append(f"Comparison error: {str(e)}")
        
        return differences

    def _compare_subdirectories(self, current_dir, server_dir, relative_path):
        """Recursively compare subdirectories"""
        differences = []
        
        try:
            import filecmp
            
            dcmp = filecmp.dircmp(current_dir, server_dir)
            
            for file in dcmp.right_only:
                differences.append(f"New file: {relative_path}/{file}")
            
            for file in dcmp.left_only:
                differences.append(f"Deleted file: {relative_path}/{file}")
            
            for file in dcmp.diff_files:
                differences.append(f"Modified file: {relative_path}/{file}")
            
            for subdir in dcmp.common_dirs:
                subdiff = self._compare_subdirectories(
                    os.path.join(current_dir, subdir),
                    os.path.join(server_dir, subdir),
                    f"{relative_path}/{subdir}"
                )
                differences.extend(subdiff)
                
        except Exception as e:
            differences.append(f"Subdirectory comparison error in {relative_path}: {str(e)}")
        
        return differences

    @api.depends('addon_name', 'instance_id.state')
    def _compute_is_installed(self):
        """Check if addon is installed in the running Odoo instance"""
        for record in self:
            record.is_installed = False
            
            if not record.addon_name or record.instance_id.state != 'running':
                continue
                
            try:
                # Get addon technical name (folder name)
                addon_technical_name = os.path.basename(record.addon_path) if record.addon_path else record.addon_name.replace(' ', '_').lower()
                
                # Check installation status via API
                is_installed = record._check_addon_installation_status(addon_technical_name)
                record.is_installed = is_installed
                
                # Update state based on installation status
                if is_installed and record.state == 'ready':
                    record.write({'state': 'installed'})
                elif not is_installed and record.state == 'installed':
                    record.write({'state': 'ready'})
                    
            except Exception as e:
                _logger.warning(f"[LAUNCHLY_SAAS - {record.instance_id.name}] Could not check installation status for {record.addon_name}: {str(e)}")
                record.is_installed = False

    def _check_addon_installation_status(self, addon_technical_name):
        """Check if addon is installed via API call"""
        try:
            import requests
            
            # Get instance details
            url = self.instance_id.instance_url
            db_name = self.instance_id.database_name
            username = self.instance_id.user_email
            password = self.instance_id.user_phone if self.instance_id.user_phone else self.instance_id.user_password
            
            # Login to get session
            login_url = f"{url}/web/session/authenticate"
            login_data = {
                'jsonrpc': '2.0',
                'method': 'call',
                'params': {
                    'db': db_name,
                    'login': username,
                    'password': password
                },
                'id': 1
            }
            
            session = requests.Session()
            login_response = session.post(login_url, json=login_data, timeout=15)
            
            if login_response.status_code != 200:
                return False
            
            login_result = login_response.json()
            if not login_result.get('result') or login_result['result'].get('uid') is None:
                return False
            
            # Search for the addon module and check its state
            api_url = f"{url}/web/dataset/call_kw"
            search_data = {
                'jsonrpc': '2.0',
                'method': 'call',
                'params': {
                    'model': 'ir.module.module',
                    'method': 'search_read',
                    'args': [[['name', '=', addon_technical_name]], ['state']],
                    'kwargs': {}
                },
                'id': 2
            }
            
            search_response = session.post(api_url, json=search_data, timeout=15)
            if search_response.status_code == 200:
                search_result = search_response.json()
                modules = search_result.get('result', [])
                
                if modules:
                    module_state = modules[0].get('state', 'uninstalled')
                    return module_state == 'installed'
            
            return False
            
        except Exception as e:
            _logger.debug(f"[LAUNCHLY_SAAS - {self.instance_id.name}] Error checking installation status: {str(e)}")
            return False


class CustomAddonFile(models.Model):
    _name = 'custom.addon.file'
    _description = 'Custom Addon File'

    addon_line_id = fields.Many2one('custom.addon.line', string='Addon Line', required=True, ondelete='cascade')
    file_path = fields.Char(string='File Path', required=True, help="Relative path within addon folder")
    file_content = fields.Binary(string='File Content', required=True)
    filename = fields.Char(string='Filename', required=True) 