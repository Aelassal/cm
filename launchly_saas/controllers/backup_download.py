from odoo import http
from odoo.http import request
import os
from werkzeug.utils import secure_filename
from werkzeug.exceptions import NotFound, Forbidden

class MicroSaasBackupDownloadController(http.Controller):
    @http.route('/launchly_saas/download_backup', type='http', auth='user')
    def download_backup(self, file=None, backup_id=None, **kwargs):
        if not file or not backup_id:
            return NotFound()
        backup = request.env['odoo.instance.backup'].sudo().browse(int(backup_id))
        if not backup.exists():
            return NotFound()
        # Security: check user has access to this backup
        if not request.env.user.has_group('base.group_system') and backup.create_uid != request.env.user:
            return Forbidden()
        backup_path = backup.backup_path
        if not backup_path or not os.path.isdir(backup_path):
            return NotFound()
        # Only allow files in the backup_path and matching *_backup_*.zip
        filename = secure_filename(file)
        abs_path = os.path.abspath(os.path.join(backup_path, filename))
        if not abs_path.startswith(os.path.abspath(backup_path)):
            return Forbidden()
        if not os.path.isfile(abs_path):
            return NotFound()
        # Stream the file
        return request.make_response(
            open(abs_path, 'rb').read(),
            headers=[
                ('Content-Type', 'application/zip'),
                ('Content-Disposition', f'attachment; filename="{filename}"'),
            ]
        ) 