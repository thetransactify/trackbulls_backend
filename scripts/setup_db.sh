#!/bin/bash
# scripts/setup_db.sh
# Run once to create PostgreSQL database and user
# Usage: bash scripts/setup_db.sh

set -e

echo "========================================"
echo "  TrackBulls — PostgreSQL Setup Script"
echo "========================================"

# Load .env if present
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

DB_NAME=${DB_NAME:-trackbulls}
DB_USER=${DB_USER:-trackbulls_user}
DB_PASSWORD=${DB_PASSWORD:-trackbulls_pass}
DB_HOST=${DB_HOST:-localhost}
DB_PORT=${DB_PORT:-5432}

echo ""
echo "▶ Creating PostgreSQL user: $DB_USER"
sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';" 2>/dev/null || \
  echo "  User may already exist — skipping"

echo "▶ Creating database: $DB_NAME"
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null || \
  echo "  Database may already exist — skipping"

echo "▶ Granting privileges"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"

echo ""
echo "✅ PostgreSQL setup complete"
echo ""
echo "Next steps:"
echo "  1. Copy .env.example to .env and fill in your values"
echo "  2. Run: pip install -r requirements.txt"
echo "  3. Run: python -m app.db.init_db"
echo "  4. Run: uvicorn app.main:app --reload --port 8000"
echo ""
