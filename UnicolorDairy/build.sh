
#!/usr/bin/env bash

set -o errexit

echo "Installing dependencies..."
python -m pip install -r requirements.txt

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Applying database migrations..."
python manage.py migrate --noinput

if [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
  echo "Creating admin user (skipped if it already exists)..."
  python manage.py createsuperuser --noinput || echo "Admin user already exists - skipping."
fi

echo "Build completed!"
