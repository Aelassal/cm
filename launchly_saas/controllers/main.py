import logging
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SubscriptionAPI(http.Controller):

    @http.route('/api/create_subscription', type='json', auth='public', methods=['POST'], csrf=False)
    def create_subscription(self, **kwargs):
        # Log content type
        content_type = request.httprequest.content_type
        _logger.info("Incoming Content-Type: %s", content_type)

        # Log raw body before Odoo parses it
        raw_body = request.httprequest.get_data(as_text=True)
        _logger.info("Raw request body: %s", raw_body)

        # Log whether kwargs is empty or populated
        if kwargs:
            _logger.info("Received request to create subscription with data: %s", kwargs)
        else:
            _logger.warning("No JSON data parsed by Odoo (kwargs is empty)")

        company_name = kwargs.get('company_name')
        country_name = kwargs.get('country_name')  # Changed from country_id to country_name
        email = kwargs.get('email')
        phone = kwargs.get('phone')
        plan_id = kwargs.get('plan_id')
        subdomain = kwargs.get('subdomain')
        subscription_period = kwargs.get('subscription_period')

        if not all([company_name, country_name, email, phone]):
            _logger.warning("Missing required fields. Data received: %s", kwargs)
            return {"error": "Missing required fields: company_name, country_name, email, phone"}

        # Find country by name
        country = request.env['res.country'].sudo().search([('name', '=ilike', country_name)], limit=1)
        if not country:
            return {"error": f"Country not found: {country_name}"}

        partner = request.env['res.partner'].sudo().search([('email', '=', email)], limit=1)
        if not partner:
            partner = request.env['res.partner'].sudo().create({
                'name': company_name,
                'country_id': country.id,  # Use the found country's ID
                'email': email,
                'phone': phone,
            })

        subscription = request.env['launchly.subscription'].sudo().create({
            'partner_id': partner.id,
            'plan_id': plan_id,
            'subdomain': subdomain,
            'subscription_period': subscription_period,
        })

        return {
            "message": "Subscription created successfully",
            "subscription_id": subscription.id,
            "partner_id": partner.id
        }

    @http.route('/api/create_demo', type='json', auth='public', methods=['POST'], csrf=False)
    def create_demo(self, **kwargs):
        # Log content type
        content_type = request.httprequest.content_type
        _logger.info("Incoming Content-Type: %s", content_type)

        # Log raw body before Odoo parses it
        raw_body = request.httprequest.get_data(as_text=True)
        _logger.info("Raw request body: %s", raw_body)

        # Handle JSON-RPC format
        params = kwargs.get('params', kwargs)

        if params:
            _logger.info("Received request to create demo with data: %s", params)
        else:
            _logger.warning("No JSON data parsed by Odoo")
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": 400,
                    "message": "No data received"
                }
            }

        # Extract required fields (making subdomain optional)
        required_fields = {
            'company_name': params.get('company_name'),
            'country_name': params.get('country_name'),
            'user_email': params.get('user_email'),
            'user_phone': params.get('user_phone'),
            'plan_id': params.get('plan_id')
        }

        # Check for missing required fields
        missing_fields = [field for field, value in required_fields.items() if not value]
        if missing_fields:
            _logger.warning("Missing required fields: %s", missing_fields)
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": 400,
                    "message": f"Missing required fields: {', '.join(missing_fields)}"
                }
            }

        try:
            # Find country by name
            country = request.env['res.country'].sudo().search([('name', '=ilike', params['country_name'])], limit=1)
            if not country:
                return {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": 404,
                        "message": f"Country not found: {params['country_name']}"
                    }
                }

            # Find or create partner
            partner = request.env['res.partner'].sudo().search([('email', '=', params['user_email'])], limit=1)
            if not partner:
                partner = request.env['res.partner'].sudo().create({
                    'name': params['company_name'],
                    'country_id': country.id,
                    'email': params['user_email'],
                    'phone': params['user_phone'],
                })

            # Prepare data for demo instance creation
            post_data = {
                'user_email': params['user_email'],
                'user_phone': params['user_phone'],
                'country_id': country.id,
                'company_name': params['company_name'],
                'plan_id': params['plan_id'],
                'subdomain_name': params.get('subdomain', ''),  # Optional field
            }

            # Create demo instance
            instance = request.env['odoo.instance'].sudo().create_demo_instance_after_delay(post_data,
                                                                                                   partner.id)

            return {
                "jsonrpc": "2.0",
                "result": {
                    "message": "Demo instance creation initiated successfully",
                    "instance_id": instance.id if instance else None,
                    "partner_id": partner.id,
                    "activation_code": instance.name if instance else None
                }
            }

        except Exception as e:
            _logger.error("Failed to create demo instance: %s", str(e))
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": 500,
                    "message": f"Failed to create demo instance: {str(e)}"
                }
            }