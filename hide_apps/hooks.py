def hide_apps_menu(env):
    menu = env.ref('base.menu_apps', raise_if_not_found=False)
    if menu:
        menu.active = False

def restore_apps_menu(env):
    menu = env.ref('base.menu_apps', raise_if_not_found=False)
    if menu:
        menu.active = True 