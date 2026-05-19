<?php
/**
 * DevPush Adminer Auto-Login Configuration
 * 
 * This script handles auto-login via signed tokens (same format as phpMyAdmin).
 * Expected URL format: /?devpush_token=<base64url_payload>.<base64url_signature>
 * 
 * The signed token contains: u (username), p (password), db (database), host, port, exp (expires)
 */

function devpush_b64url_decode(string $value)
{
    $padding = strlen($value) % 4;
    if ($padding > 0) {
        $value .= str_repeat('=', 4 - $padding);
    }
    return base64_decode(strtr($value, '-_', '+/'), true);
}

function devpush_b64url_encode(string $value): string
{
    return rtrim(strtr(base64_encode($value), '+/', '-_'), '=');
}

// Start session for storing auto-login credentials
if (session_status() === PHP_SESSION_NONE) {
    session_start();
}

// Check for DevPush auto-login token
$token = $_GET['devpush_token'] ?? '';
if (!empty($token)) {
    $secret = getenv('DEVPUSH_SIGNON_SECRET') ?: '';
    
    if (!empty($secret)) {
        $parts = explode('.', $token, 2);
        if (count($parts) === 2) {
            [$payloadPart, $signaturePart] = $parts;
            
            // Verify signature using HMAC-SHA256 (same as Python)
            $expectedSignature = devpush_b64url_encode(
                hash_hmac('sha256', $payloadPart, $secret, true)
            );
            
            if (hash_equals($expectedSignature, $signaturePart)) {
                // Decode payload
                $payloadJson = devpush_b64url_decode($payloadPart);
                if ($payloadJson !== false) {
                    $payload = json_decode($payloadJson, true);
                    if (is_array($payload)) {
                        // Check expiration
                        $expiresAt = (int) ($payload['exp'] ?? 0);
                        if ($expiresAt > time()) {
                            // Store credentials in session for the plugin
                            $_SESSION['adminer_auto_credentials'] = [
                                'server' => $payload['host'] ?? 'postgres-storage',
                                'port' => (int) ($payload['port'] ?? 5432),
                                'username' => $payload['u'] ?? '',
                                'password' => $payload['p'] ?? '',
                                'database' => $payload['db'] ?? ''
                            ];
                            
                            // Redirect to clean URL (without token)
                            header('Location: /');
                            exit;
                        }
                    }
                }
            }
        }
    }
}

// Include the original adminer
require __DIR__ . '/adminer.php';
