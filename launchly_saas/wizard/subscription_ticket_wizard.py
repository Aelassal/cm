from odoo import models, fields, api
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)

class SubscriptionTicketWizard(models.TransientModel):
    _name = 'subscription.ticket.wizard'
    _description = 'Create Subscription Ticket Wizard'

    subscription_id = fields.Many2one('launchly.subscription', string='Subscription', required=True)
    partner_id = fields.Many2one('res.partner', string='Customer', related='subscription_id.partner_id', readonly=True)
    partner_email = fields.Char(string='Customer Email', related='subscription_id.partner_email', readonly=True)
    
    subject = fields.Char(string='Subject', required=True)
    description = fields.Html(string='Description', required=True)
    attachment_ids = fields.Many2many('ir.attachment', string='Attachments')
    
    def action_create_ticket(self):
        """Create the ticket and send email"""
        self.ensure_one()
        
        # Create the ticket
        ticket_vals = {
            'subscription_id': self.subscription_id.id,
            'subject': self.subject,
            'description': self.description,
            'state': 'open',
        }
        
        ticket = self.env['subscription.ticket'].create(ticket_vals)
        
        # Attach files to the ticket's chatter
        if self.attachment_ids:
            # Update attachment records to point to the ticket
            for attachment in self.attachment_ids:
                attachment.write({
                    'res_model': 'subscription.ticket',
                    'res_id': ticket.id,
                })
            
            # Add attachments to the ticket's chatter
            ticket.message_post(
                body=f"Ticket created with {len(self.attachment_ids)} attachment(s)",
                attachment_ids=self.attachment_ids.ids,
                message_type='comment',
                subtype_id=self.env.ref('mail.mt_comment').id,
            )
        
        # Send email to company
        self._send_ticket_email(ticket)
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Success',
                'message': f'Ticket {ticket.name} has been created and sent successfully.',
                'type': 'success',
                'sticky': False,
            }
        }
    
    def _send_ticket_email(self, ticket):
        """Send email notification about the ticket"""
        try:
            # Get company email
            company = self.env.company
            company_email = company.email or company.partner_id.email
            
            if not company_email:
                _logger.warning('No company email found for ticket notification')
                return
            
            # Prepare email content
            subject = f'[Ticket {ticket.name}] {ticket.subject}'
            
            body = f"""
            <p>A new support ticket has been created:</p>
            <br/>
            <p><strong>Ticket Number:</strong> {ticket.name}</p>
            <p><strong>Customer:</strong> {ticket.partner_id.name}</p>
            <p><strong>Customer Email:</strong> {ticket.partner_email}</p>
            <p><strong>Subscription:</strong> {ticket.subscription_id.name}</p>
            <p><strong>Subject:</strong> {ticket.subject}</p>
            <br/>
            <p><strong>Description:</strong></p>
            {ticket.description}
            """
            
            # Create mail message
            mail_values = {
                'subject': subject,
                'body': body,
                'email_from': ticket.partner_email,
                'email_to': company_email,
                'model': 'subscription.ticket',
                'res_id': ticket.id,
                'message_type': 'email',
                'subtype_id': self.env.ref('mail.mt_comment').id,
            }
            
            # Send the email
            self.env['mail.mail'].create(mail_values).send()
            
            # Log the message in the ticket chatter
            ticket.message_post(
                body=f"Ticket notification sent to {company_email}",
                subject=subject,
                message_type='email',
                subtype_id=self.env.ref('mail.mt_comment').id,
            )
            
        except Exception as e:
            _logger.error(f'Failed to send ticket email: {str(e)}')
            raise ValidationError(f'Failed to send email notification: {str(e)}') 