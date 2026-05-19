<?php

define('DEVPUSH_SIGNON_SESSION', 'devpush_signon');

$hasDevpushSignon = !empty($_GET['devpush_token']) || !empty($_COOKIE[DEVPUSH_SIGNON_SESSION]);

if ($hasDevpushSignon) {
    $cfg['Servers'][$i]['auth_type'] = 'signon';
    $cfg['Servers'][$i]['SignonURL'] = '/devpush-login.php';
    $cfg['Servers'][$i]['SignonSession'] = DEVPUSH_SIGNON_SESSION;
    $cfg['Servers'][$i]['SignonCookieParams'] = [
        'path' => '/',
        'httponly' => true,
        'secure' => !empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off',
        'samesite' => 'Lax',
    ];
    $cfg['Servers'][$i]['AllowNoPassword'] = false;
}
