<?php
/**
 * Adminer plugin for automatic login with credentials from session.
 * This enables one-click login from the DevPush panel.
 */

class AdminerLoginPassword {
    private $credentials = null;
    private $database_name = null;
    
    function __construct() {
        // Try to read credentials from session
        if (session_status() === PHP_SESSION_NONE) {
            @session_start();
        }
        
        if (!empty($_SESSION['adminer_auto_credentials'])) {
            $cred = $_SESSION['adminer_auto_credentials'];
            $this->credentials = [
                $cred['server'] ?? 'postgres-storage',
                $cred['username'] ?? '',
                $cred['password'] ?? ''
            ];
            $this->database_name = $cred['database'] ?? null;
            
            // Clear credentials after reading for security
            // (they'll be re-set on next request via the cookie/session)
        }
        
        // Also support environment variables as fallback
        if ($this->credentials === null) {
            $env_user = getenv('ADMINER_AUTO_LOGIN_USER');
            if (!empty($env_user)) {
                $this->credentials = [
                    getenv('ADMINER_DEFAULT_SERVER') ?: 'postgres-storage',
                    $env_user,
                    getenv('ADMINER_AUTO_LOGIN_PASS') ?: ''
                ];
                $this->database_name = getenv('ADMINER_AUTO_LOGIN_DB') ?: null;
            }
        }
    }
    
    function credentials() {
        return $this->credentials;
    }
    
    function database() {
        return $this->database_name;
    }
    
    function login($login, $password) {
        // Allow login if we have auto-credentials
        if ($this->credentials !== null) {
            return true;
        }
        return null;
    }
}

// Return plugin instance for AdminerPlugin wrapper
return new AdminerLoginPassword();
