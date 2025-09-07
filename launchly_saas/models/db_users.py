from odoo import models, fields, api
class OdooDbUser(models.Model):
    _name = 'odoo.db.user'
    _description = 'Odoo Database User'
    _rec_name = 'name'

    instance_id = fields.Many2one('odoo.docker.instance', string='Instance', required=True, ondelete='cascade')
    user_id = fields.Integer(string='User ID', help="User ID in the database")
    login = fields.Char(string='Login', required=True)
    email = fields.Char(string='Email')
    phone = fields.Char(string='Phone')
    name = fields.Char(string='Name', required=True)
    active = fields.Boolean(string='Active', default=True)
    login_date = fields.Datetime(string='Last Login Date', help="Last time the user logged into the system")
    current_password = fields.Char(string='Current Password', help="Current password (for reference)")
    new_password = fields.Char(string='New Password', help="Enter new password to change it")
    password_state = fields.Selection([
        ('draft', 'Stable'),
        ('waiting', 'Waiting Approve'),
        ('changed', 'Changed'),
        ('failed', 'Failed')
    ], string='Password Status', default='draft', help="Status of password change operation")
    instance_state = fields.Selection( related='instance_id.state', string='Instance State', readonly=True,
                                       help="State of the instance this user belongs to")
    @api.onchange('new_password')
    def _onchange_new_password(self):
        """Ensure new password is not empty"""
        if self.new_password:
            self.password_state = 'waiting'
        else:
            self.password_state = 'draft'

    def change_password(self):
        """Change password for this user"""
        for user in self:
            if not user.new_password:
                raise ValueError("Please enter a new password")

            # Commit the state change so it's visible in UI immediately
            self.env.cr.commit()

            if user.instance_id.change_user_password_with_sudo(user.login, user.new_password):
                user.current_password = user.new_password
                user.new_password = ''  # Clear the new password field
                user.password_state = 'changed'

            else:
                user.password_state = 'failed'

    def refresh_from_db(self):
        """Refresh user information from the database"""
        instance = self.instance_id
        if instance:
            instance.refresh_db_users()
            return {
                'type': 'ir.actions.client',
                'tag': 'reload',
            }

    def action_login_as_user(self):
        """
        Redirects to the managed instance's login_as_user endpoint for this user.
        """
        self.ensure_one()
        instance_url = self.instance_id.domained_url if self.instance_id.domained_url else self.instance_id.instance_url
        secret = 'LAUNCHLY_SAAS_TOKEN'  # TODO: Store this securely or fetch from config
        login = self.login
        if not instance_url:
            raise ValueError("Instance URL is not set.")
        url = f"{instance_url.rstrip('/')}/saas/login_as_user?login={login}&token={secret}"
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'new',
        }