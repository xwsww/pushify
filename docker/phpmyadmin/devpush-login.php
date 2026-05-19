<?php

const DEVPUSH_SIGNON_SESSION = 'devpush_signon';

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

function devpush_fail(string $message, int $status = 400): void
{
    http_response_code($status);
    header('Content-Type: text/plain; charset=utf-8');
    echo $message;
    exit;
}

function devpush_read_token_payload(string $token): array
{
    if ($token === '') {
        devpush_fail('Missing signon token.');
    }

    $secret = getenv('DEVPUSH_SIGNON_SECRET') ?: '';
    if ($secret === '') {
        devpush_fail('Missing signon secret.', 500);
    }

    $parts = explode('.', $token, 2);
    if (count($parts) !== 2) {
        devpush_fail('Invalid signon token.');
    }

    [$payloadPart, $signaturePart] = $parts;
    $expectedSignature = devpush_b64url_encode(
        hash_hmac('sha256', $payloadPart, $secret, true)
    );
    if (!hash_equals($expectedSignature, $signaturePart)) {
        devpush_fail('Invalid signon token.');
    }

    $payloadJson = devpush_b64url_decode($payloadPart);
    if ($payloadJson === false) {
        devpush_fail('Invalid signon payload.');
    }

    $payload = json_decode($payloadJson, true);
    if (!is_array($payload)) {
        devpush_fail('Invalid signon payload.');
    }

    $expiresAt = (int) ($payload['exp'] ?? 0);
    if ($expiresAt < time()) {
        devpush_fail('Signon token expired.', 401);
    }

    $username = (string) ($payload['u'] ?? '');
    if ($username === '') {
        devpush_fail('Missing signon username.');
    }

    return $payload;
}

$cookieParams = [
    'path' => '/',
    'httponly' => true,
    'secure' => !empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off',
    'samesite' => 'Lax',
];

session_name(DEVPUSH_SIGNON_SESSION);
session_set_cookie_params($cookieParams);
session_start();
$_SESSION = [];

foreach (array_keys($_COOKIE) as $cookieName) {
    if (strncmp($cookieName, 'pmaAuth-', 8) === 0) {
        setcookie($cookieName, '', time() - 42000, '/');
    }
}

$payload = devpush_read_token_payload((string) ($_GET['devpush_token'] ?? ''));
$_SESSION['PMA_single_signon_user'] = (string) $payload['u'];
$_SESSION['PMA_single_signon_password'] = (string) ($payload['p'] ?? '');
$_SESSION['PMA_single_signon_host'] = (string) (getenv('PMA_HOST') ?: 'mariadb');
$_SESSION['PMA_single_signon_port'] = (string) (getenv('PMA_PORT') ?: '3306');
$_SESSION['PMA_single_signon_HMAC_secret'] = bin2hex(random_bytes(16));
session_write_close();

$query = [];
foreach (['db', 'route'] as $key) {
    if (isset($_GET[$key])) {
        $query[$key] = (string) $_GET[$key];
    }
}

$location = '/index.php';
if ($query !== []) {
    $location .= '?' . http_build_query($query);
}

header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
header('Location: ' . $location, true, 302);
exit;
