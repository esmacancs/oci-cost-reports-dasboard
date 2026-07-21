#!/bin/sh
set -e

# Fix key_file path for container
sed -i 's|key_file=.*|key_file=/root/.oci/oci_api_key.pem|' /root/.oci/config

exec python app.py
