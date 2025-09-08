#!/bin/bash
################################################################################
# Odoo Instance Installer Script - Custom Addons Auto Path
# Usage:
#   ./install_odoo_instance.sh <name> <odoo_version> <source_path> <http_port> <db_name> <db_user> <db_password> <admin_password> <user_email> <user_phone> <country_code> <is_demo>
################################################################################

# Exit on error
set -e

# Arguments passed from Python
INSTANCE_NAME=$1
ODOO_VERSION=$2
SOURCE_PATH=$3
HTTP_PORT=$4
DB_NAME=$5
DB_USER=$6
DB_PASSWORD=$7
ADMIN_PASSWORD=$8
USER_EMAIL=$9
USER_PHONE=${10}
COUNTRY_CODE=${11}
IS_DEMO=${12}

# Derived vars
OE_USER=$INSTANCE_NAME
OE_HOME="/opt/$OE_USER"
OE_HOME_EXT="$SOURCE_PATH"   # <-- Use source path directly (no copy)
OE_CONFIG="/etc/${OE_USER}.conf"
OE_LOG="/var/log/${OE_USER}.log"
VENV_PATH="$OE_HOME/venv"
CUSTOM_ADDONS_PATH="$OE_HOME/custom-addons"   # <-- Auto created

echo "============================================================"
echo " Starting installation of Odoo instance: $INSTANCE_NAME"
echo " Version: $ODOO_VERSION"
echo " Source path: $SOURCE_PATH (used directly)"
echo " Custom addons: $CUSTOM_ADDONS_PATH"
echo " HTTP Port: $HTTP_PORT"
echo " Database: $DB_NAME (User: $DB_USER)"
echo " Admin Email: $USER_EMAIL"
echo " Country: $COUNTRY_CODE"
echo " Demo Data: $IS_DEMO"
echo "============================================================"

# Install dependencies
sudo apt-get install -y git python3.10 python3.10-venv python3.10-dev \
    libxml2-dev libxslt1-dev zlib1g-dev libsasl2-dev libldap2-dev \
    build-essential libssl-dev libffi-dev libjpeg-dev libpq-dev \
    xfonts-75dpi xfonts-base fontconfig acl

# Create system user
if ! id -u $OE_USER >/dev/null 2>&1; then
    sudo adduser --system --home=$OE_HOME --group $OE_USER
fi
sudo mkdir -p /var/log/$OE_USER
sudo chown $OE_USER:$OE_USER /var/log/$OE_USER

# PostgreSQL user setup
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
    echo "Creating PostgreSQL user $DB_USER..."
    sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD' CREATEDB;"
else
    echo "PostgreSQL user $DB_USER already exists."
fi

# Create database if it doesn't exist
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    echo "Creating database $DB_NAME..."
    sudo -u postgres createdb -O $DB_USER $DB_NAME
    echo "Database $DB_NAME created successfully."
    DB_NEEDS_INIT=true
    echo "DEBUG: DB_NEEDS_INIT set to true (new database)"
else
    echo "Database $DB_NAME already exists."
    # Check if database is initialized (has ir_module_module table)
    if ! sudo -u postgres psql -d $DB_NAME -tAc "SELECT 1 FROM information_schema.tables WHERE table_name='ir_module_module'" | grep -q 1; then
        echo "Database $DB_NAME exists but is not initialized."
        echo "Dropping and recreating database to ensure clean initialization..."
        sudo -u postgres dropdb $DB_NAME
        sudo -u postgres createdb -O $DB_USER $DB_NAME
        echo "Database $DB_NAME recreated successfully."
        DB_NEEDS_INIT=true
        echo "DEBUG: DB_NEEDS_INIT set to true (recreated database)"
    else
        echo "Database $DB_NAME is already initialized."
        DB_NEEDS_INIT=false
        echo "DEBUG: DB_NEEDS_INIT set to false (already initialized)"
    fi
fi

echo "DEBUG: Final DB_NEEDS_INIT value: $DB_NEEDS_INIT"

# Ensure source path is accessible to the instance user
if [ -d "$SOURCE_PATH" ]; then
    echo "Granting access to $SOURCE_PATH for user $OE_USER..."
    if command -v setfacl >/dev/null 2>&1; then
        sudo setfacl -R -m u:$OE_USER:rx $SOURCE_PATH
    else
        sudo chmod -R o+rx $SOURCE_PATH
    fi
fi

# Create custom addons path
sudo mkdir -p $CUSTOM_ADDONS_PATH
sudo chown -R $OE_USER:$OE_USER $CUSTOM_ADDONS_PATH

# Create data directory for filestore and session data
sudo mkdir -p $OE_HOME/data
sudo chown -R $OE_USER:$OE_USER $OE_HOME/data

# Setup virtualenv
python3.10 -m venv $VENV_PATH
source $VENV_PATH/bin/activate
pip install --upgrade pip wheel setuptools
pip install -r $OE_HOME_EXT/requirements.txt
deactivate

# Determine demo data setting
if [ "$IS_DEMO" = "true" ]; then
    WITHOUT_DEMO="False"
    echo "Demo data will be installed (is_demo=true -> without_demo=False)"
else
    WITHOUT_DEMO="True"
    echo "Demo data will NOT be installed (is_demo=false -> without_demo=True)"
fi

# Create config file
sudo bash -c "cat > $OE_CONFIG <<EOF
[options]
admin_passwd = $ADMIN_PASSWORD
db_host = False
db_port = False
db_user = $DB_USER
db_password = $DB_PASSWORD
addons_path = $OE_HOME_EXT/addons,$CUSTOM_ADDONS_PATH
xmlrpc_port = $HTTP_PORT
logfile = $OE_LOG
data_dir = $OE_HOME/data
without_demo = $WITHOUT_DEMO
db_name = $DB_NAME
db_manager = False
list_db = False
EOF"

sudo chown $OE_USER:$OE_USER $OE_CONFIG
sudo chmod 640 $OE_CONFIG

# Create systemd service
SERVICE_FILE="/etc/systemd/system/${OE_USER}.service"
sudo bash -c "cat > $SERVICE_FILE <<EOF
[Unit]
Description=Odoo Service for $INSTANCE_NAME
After=postgresql.service

[Service]
Type=simple
User=$OE_USER
ExecStart=$VENV_PATH/bin/python3.10 $OE_HOME_EXT/odoo-bin -c $OE_CONFIG
Restart=always

[Install]
WantedBy=multi-user.target
EOF"

# Reload systemd and set ACLs
current="$SOURCE_PATH"
while [ "$current" != "/" ]; do
    sudo setfacl -m u:"$OE_USER":x "$current"
    current=$(dirname "$current")
done

# Give full rx permissions recursively to the source itself
sudo setfacl -R -m u:"$OE_USER":rx "$SOURCE_PATH"

# Ensure log directory exists and has correct permissions
sudo mkdir -p $(dirname $OE_LOG)
sudo touch $OE_LOG
sudo chown $OE_USER:$OE_USER $OE_LOG
sudo chmod 644 $OE_LOG

sudo systemctl daemon-reload
sudo systemctl enable ${OE_USER}.service

# Initialize database if needed
echo "DEBUG: Checking if database initialization is needed..."
echo "DEBUG: DB_NEEDS_INIT = $DB_NEEDS_INIT"
if [ "$DB_NEEDS_INIT" = "true" ]; then
    echo "Initializing database $DB_NAME with base modules..."
    echo "DEBUG: Running: sudo -u $OE_USER $VENV_PATH/bin/python3.10 $OE_HOME_EXT/odoo-bin -c $OE_CONFIG -d $DB_NAME -i base --stop-after-init"
    
    # Debug configuration before initialization
    echo "DEBUG: Checking configuration file..."
    echo "DEBUG: Config file path: $OE_CONFIG"
    echo "DEBUG: Config file contents:"
    cat $OE_CONFIG
    
    echo "DEBUG: Checking database connection..."
    if sudo -u postgres psql -d $DB_NAME -c "SELECT version();" >/dev/null 2>&1; then
        echo "DEBUG: Database connection test successful"
    else
        echo "ERROR: Cannot connect to database $DB_NAME"
        exit 1
    fi
    
    # Run initialization and capture output
    echo "DEBUG: Starting database initialization..."
    INIT_OUTPUT=$(sudo -u $OE_USER $VENV_PATH/bin/python3.10 $OE_HOME_EXT/odoo-bin -c $OE_CONFIG -d $DB_NAME -i base --stop-after-init 2>&1)
    INIT_RESULT=$?
    
    echo "DEBUG: Initialization command exit code: $INIT_RESULT"
    echo "DEBUG: Initialization output:"
    echo "$INIT_OUTPUT"
    
    if [ $INIT_RESULT -eq 0 ]; then
        echo "Database initialization command completed."
        
        # Verify initialization worked
        if sudo -u postgres psql -d $DB_NAME -tAc "SELECT 1 FROM information_schema.tables WHERE table_name='ir_module_module'" | grep -q 1; then
            echo "DEBUG: Verification successful - ir_module_module table exists"
            DB_INIT_SUCCESS=true
        else
            echo "ERROR: Verification failed - ir_module_module table not found"
            echo "DEBUG: Checking what tables exist in database..."
            sudo -u postgres psql -d $DB_NAME -c "\dt" | head -10
            exit 1
        fi
    else
        echo "ERROR: Database initialization failed with exit code: $INIT_RESULT"
        echo "ERROR: Output: $INIT_OUTPUT"
        exit 1
    fi
else
    echo "DEBUG: Database initialization not needed"
fi

# Only setup admin user if database was just initialized successfully
if [ "$DB_NEEDS_INIT" = "true" ] && [ "$DB_INIT_SUCCESS" = "true" ]; then
    echo "Setting up admin user with custom credentials..."
    # Create a temporary Python script to setup the admin user
    SETUP_SCRIPT="/tmp/setup_admin_${INSTANCE_NAME}.py"
    cat > $SETUP_SCRIPT <<EOF
#!/usr/bin/env python3
import sys
sys.path.insert(0, '$OE_HOME_EXT')

import odoo
from odoo import api, SUPERUSER_ID

# Configure Odoo
odoo.tools.config.parse_config(['-c', '$OE_CONFIG'])

try:
    from odoo.modules.registry import Registry
    import time
    
    # Retry logic for database connection
    max_attempts = 10
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"DEBUG: Attempt {attempt}/{max_attempts} - Connecting to database: $DB_NAME")
            
            # Wait a bit before each attempt
            if attempt > 1:
                wait_time = min(attempt * 2, 10)  # Progressive wait: 2s, 4s, 6s, 8s, 10s, 10s...
                print(f"DEBUG: Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
            
            registry = Registry.new('$DB_NAME')
            print("DEBUG: Registry created successfully")
            
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                print("DEBUG: Environment created successfully")
                
                # Verify basic models are available
                users_count = env['res.users'].search_count([])
                print(f"DEBUG: Found {users_count} users in database")
                
                # Update admin user
                admin_user = env['res.users'].search([('login', '=', 'admin')], limit=1)
                if admin_user:
                    admin_user.write({
                        'login': '$USER_EMAIL',
                        'email': '$USER_EMAIL', 
                        'password': '$USER_PHONE',
                        'name': 'Admin User'
                    })
                    print("SUCCESS: Admin user updated")
                else:
                    print("ERROR: Admin user not found")
                    
                # Update company
                company = env['res.company'].search([], limit=1)
                if company:
                    # Find country
                    country = env['res.country'].search([('code', '=', '$COUNTRY_CODE')], limit=1)
                    company_updates = {
                        'email': '$USER_EMAIL'
                    }
                    if country:
                        company_updates['country_id'] = country.id
                    company.write(company_updates)
                    print("SUCCESS: Company updated")
                    
                cr.commit()
                print("SUCCESS: Database setup completed")
                break  # Success, exit the retry loop
                
        except Exception as attempt_error:
            print(f"DEBUG: Attempt {attempt} failed: {str(attempt_error)}")
            if attempt == max_attempts:
                print(f"ERROR: All {max_attempts} attempts failed")
                raise
            else:
                print(f"DEBUG: Retrying... ({max_attempts - attempt} attempts remaining)")
        
except Exception as e:
    print(f"ERROR: {str(e)}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
EOF

    # Execute the setup script
    sudo -u $OE_USER $VENV_PATH/bin/python3.10 $SETUP_SCRIPT
    
    # Clean up the temporary script
    rm -f $SETUP_SCRIPT
    
    echo "Admin user setup completed."
fi

sudo systemctl restart ${OE_USER}.service

echo "============================================================"
echo "✓ Odoo $ODOO_VERSION instance installed successfully!"
echo "✓ Instance name: $INSTANCE_NAME"
echo "✓ Port: $HTTP_PORT"
echo "✓ DB: $DB_NAME (User: $DB_USER)"
echo "✓ Config: $OE_CONFIG"
echo "✓ Logs: $OE_LOG"
echo "✓ Custom Addons: $CUSTOM_ADDONS_PATH"
echo "✓ Admin Login: $USER_EMAIL"
echo "✓ Admin Password: $USER_PHONE"
echo "============================================================"
echo "ADMIN_PASSWORD: $ADMIN_PASSWORD"
echo "USER_EMAIL: $USER_EMAIL"
echo "USER_PHONE: $USER_PHONE"

