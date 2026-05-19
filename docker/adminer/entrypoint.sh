#!/bin/sh
set -e

# DevPush Adminer Entrypoint
# Handles auto-login setup and plugin configuration

# Create plugins directory if not exists
mkdir -p /var/www/html/plugins

# Copy the plugin loader
if [ -f /var/www/html/plugins/login-password.php ]; then
    # Plugin is already mounted, make sure plugin.php exists
    if [ ! -f /var/www/html/plugins/plugin.php ]; then
        # Download Adminer's plugin.php if not present
        curl -fsSL -o /var/www/html/plugins/plugin.php \
            https://raw.githubusercontent.com/vrana/adminer/master/plugins/plugin.php \
            2>/dev/null || true
    fi
fi

# If plugin.php doesn't exist, create a minimal one
if [ ! -f /var/www/html/plugins/plugin.php ]; then
cat > /var/www/html/plugins/plugin.php << 'PLUGINPHP'
<?php
class AdminerPlugin extends Adminer {
    private $plugins;
    
    function __construct($plugins) {
        $this->plugins = $plugins;
        foreach ($plugins as $plugin) {
            $plugin->adminer = $this;
        }
    }
    
    function credentials() {
        foreach ($this->plugins as $plugin) {
            $cred = $plugin->credentials();
            if ($cred !== null) return $cred;
        }
        return parent::credentials();
    }
    
    function database() {
        foreach ($this->plugins as $plugin) {
            $db = $plugin->database();
            if ($db !== null) return $db;
        }
        return parent::database();
    }
    
    function login($login, $password) {
        foreach ($this->plugins as $plugin) {
            $result = $plugin->login($login, $password);
            if ($result !== null) return $result;
        }
        return parent::login($login, $password);
    }
}
PLUGINPHP
fi

# Ensure config.php includes plugins
if [ -f /var/www/html/config.php ]; then
    # Use our custom config which handles token auth
    echo "Using DevPush custom adminer config"
else
    # Create default config that loads plugins
    cat > /var/www/html/config.php << 'CONFIGPHP'
<?php
function adminer_object() {
    include_once __DIR__ . '/plugins/plugin.php';
    include_once __DIR__ . '/plugins/login-password.php';
    return new AdminerPlugin([new AdminerLoginPassword()]);
}
include __DIR__ . '/adminer.php';
CONFIGPHP
fi

# Start PHP built-in server
exec php -S 0.0.0.0:8080 -t /var/www/html /var/www/html/config.php
