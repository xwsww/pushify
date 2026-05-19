<?php

if (session_status() !== PHP_SESSION_ACTIVE) {
    session_start();
}

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

function devpush_signon_error(string $message): array
{
    if (session_status() === PHP_SESSION_ACTIVE) {
        $_SESSION['PMA_single_signon_error_message'] = $message;
    }

    return ['', ''];
}

function get_login_credentials(string $user): array
{
    $stored = $_SESSION['devpush_signon_credentials'] ?? null;
    if (is_array($stored)) {
        $username = (string) ($stored['u'] ?? '');
        $password = (string) ($stored['p'] ?? '');
        if ($username !== '') {
            return [$username, $password];
        }
    }

    $token = $_GET['devpush_token'] ?? ($_SESSION['devpush_signon_token'] ?? '');
    if ($token === '') {
        return ['', ''];
    }

    $secret = getenv('DEVPUSH_SIGNON_SECRET') ?: '';
    if ($secret === '') {
        return devpush_signon_error('Missing signon secret.');
    }

    $parts = explode('.', $token, 2);
    if (count($parts) !== 2) {
        return devpush_signon_error('Invalid signon token.');
    }

    [$payloadPart, $signaturePart] = $parts;
    $expectedSignature = devpush_b64url_encode(
        hash_hmac('sha256', $payloadPart, $secret, true)
    );
    if (!hash_equals($expectedSignature, $signaturePart)) {
        return devpush_signon_error('Invalid signon token.');
    }

    $payloadJson = devpush_b64url_decode($payloadPart);
    if ($payloadJson === false) {
        return devpush_signon_error('Invalid signon payload.');
    }

    $payload = json_decode($payloadJson, true);
    if (!is_array($payload)) {
        return devpush_signon_error('Invalid signon payload.');
    }

    $expiresAt = (int) ($payload['exp'] ?? 0);
    if ($expiresAt < time()) {
        return devpush_signon_error('Signon token expired.');
    }

    $username = (string) ($payload['u'] ?? '');
    $password = (string) ($payload['p'] ?? '');
    if ($username === '') {
        return devpush_signon_error('Missing signon username.');
    }

    $_SESSION['devpush_signon_token'] = $token;
    $_SESSION['devpush_signon_credentials'] = [
        'u' => $username,
        'p' => $password,
    ];

    return [$username, $password];
}
