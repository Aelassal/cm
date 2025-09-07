from odoo import http
from odoo.http import request

class SaasLoginAsUser(http.Controller):
    @http.route('/saas/login_as_user', type='http', auth='none', methods=['GET'], csrf=False)
    def login_as_user(self, login=None, token=None, redirect='/web'):
        """
        Secure endpoint to login as any user. Intended for SaaS management use only.
        - login: the user's login (email or username)
        - token: static secret for authentication (replace with dynamic token for production)
        - redirect: where to go after login (default: /web)
        """
        SECRET = 'LAUNCHLY_SAAS_TOKEN'  # TODO: Replace with a secure, configurable secret or dynamic token
        if token != SECRET:
            return request.not_found()

        if not login:
            return "Missing login", 400

        # Find the user
        user = request.env['res.users'].sudo().search([('login', '=', login)], limit=1)
        if not user:
            return "User not found", 404

        # Authenticate as the user (bypass password, superuser script style)
        from odoo.modules.registry import Registry
        import odoo
        registry = Registry(request.db)
        pre_uid = user.id
        request.session.uid = None
        request.session.pre_login = login
        request.session.pre_uid = pre_uid
        with registry.cursor() as cr:
            env = odoo.api.Environment(cr, pre_uid, {})
            # If 2FA is disabled we finalize immediately
            if not user._mfa_url():
                request.session.should_rotate = True
                request.session.update({
                    'db': request.db,
                    'login': login,
                    'uid': pre_uid,
                    'context': env['res.users'].context_get(),
                    'session_token': env.user._compute_session_token(request.session.sid),
                })
        if request and request.session and request.db == request.session.db:
            request.env = odoo.api.Environment(request.env.cr, pre_uid, request.session.context)
            request.update_context(**request.session.context)

        return request.redirect(redirect) 