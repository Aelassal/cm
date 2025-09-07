{
    'name': 'Hide Apps Menu',
    'version': '0.1',
    'author': 'Abdulrahman Elassal',
    'category': 'Hidden',
    'description': 'Hides the Apps menu for all users.',
    'depends': ['base'],
    'installable': True,
    'auto_install': False,
    'post_init_hook': 'hide_apps_menu',
    'uninstall_hook': 'restore_apps_menu',
}