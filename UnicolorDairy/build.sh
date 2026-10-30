
#!/usr/bin/env bash

set -o errexit

echo "Installing dependencies..."
python -m pip install -r requirements.txt

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Applying database migrations..."
python manage.py migrate --noinput

echo "Setting up admin user..."
python manage.py ensure_admin

echo "Build completed!"
