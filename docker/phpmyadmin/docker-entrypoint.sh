#!/bin/sh
set -e

# Single-file bind mounts can end up as directories; copy the login handler into docroot.
cp -f /devpush/phpmyadmin-bootstrap/devpush-login.php /var/www/html/devpush-login.php
chmod 644 /var/www/html/devpush-login.php

exec /docker-entrypoint.sh apache2-foreground
