from odoo import models, fields, api, _
import os
from datetime import datetime
import fnmatch

class InstanceBackupFileWizard(models.TransientModel):
    _name = 'instance.backup.file.wizard'
    _description = 'Backup Files Wizard'

    backup_id = fields.Many2one('odoo.instance.backup', string='Backup')
    file_ids = fields.One2many('instance.backup.file.line', 'wizard_id', string='Backup Files', compute='_compute_file_ids')

    @api.depends('backup_id')
    def _compute_file_ids(self):
        for wizard in self:
            files = []
            backup_path = wizard.backup_id.backup_path
            if backup_path and os.path.isdir(backup_path):
                file_list = []
                for fname in os.listdir(backup_path):
                    if fnmatch.fnmatch(fname, '*_backup_*.zip'):
                        fpath = os.path.join(backup_path, fname)
                        file_list.append((fname, fpath, os.path.getmtime(fpath)))
                # Sort by newest first
                file_list.sort(key=lambda x: x[2], reverse=True)
                for fname, fpath, mtime in file_list:
                    files.append((0, 0, {
                        'name': fname,
                        'size': os.path.getsize(fpath),
                        'date': datetime.fromtimestamp(mtime),
                        'download_url': f'/launchly_saas/download_backup?file={fname}&backup_id={wizard.backup_id.id}'
                    }))
            wizard.file_ids = files

class InstanceBackupFileLine(models.TransientModel):
    _name = 'instance.backup.file.line'
    _description = 'Backup File Line'

    wizard_id = fields.Many2one('instance.backup.file.wizard', string='Wizard')
    name = fields.Char('Filename')
    size = fields.Integer('Size (bytes)')
    date = fields.Datetime('Date Modified')
    download_url = fields.Char('Download URL')
    download_html = fields.Html('Download', compute='_compute_download_html')

    def _compute_download_html(self):
        for rec in self:
            url = rec.download_url or '#'
            rec.download_html = (
                f'<a href="{url}" target="_blank" class="btn btn-link" title="Download">'
                f'<i class="fa fa-download"></i> Download</a>'
            )

    # def download_file(self):
    #     self.ensure_one()
    #     return {
    #         'type': 'ir.actions.act_url',
    #         'url': self.download_url,
    #         'target': 'self',
    #     }