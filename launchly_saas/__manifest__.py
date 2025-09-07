{
    "name": "Launchly Saas",
    "category": "Tools",
    "summary": "Full odoo saas kit",
    "description": """
        This module allows you to fully manage your Odoo instances with full control over it.
    """,
    "author": "Abdulrahman Elassal",
    "website": "https://launchlyclub.com/",
    "license": "AGPL-3",
    "version": "18.0.1.2",
    "depends": ["base", "product", "sale_management" , "project"],
    "data": [
        "data/ir_sequence.xml",
        "views/subscription_views.xml",
        "views/menu.xml",
        "security/ir.model.access.csv",
        "security/security.xml",
        "views/odoo_docker_instance.xml",
        "views/docker_compose_template.xml",
        "views/config_views.xml",
        "views/instance_plan_views.xml",
        "views/instance_backup_views.xml",
        "views/instance_backup_file_wizard.xml",
        "views/subscription_renewal_history_views.xml",
        "data/subscription_data.xml",
        "data/mail_template_data.xml",
        "wizard/custom_addon_installer_wizard.xml",
        "data/data.xml",
        "data/instance_backup_cron.xml",
        "views/Project_views.xml",
    ],
    "images": ["static/icon.png"],
    "demo": [],
    "installable": True,
    "application": True,
    "auto_install": False,

}


