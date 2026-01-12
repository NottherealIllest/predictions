#!/bin/bash

# Predictions Bot - Quick Deploy Script
# This script helps automate the Heroku deployment process

set -e

echo "🎯 Predictions Bot - Quick Deploy"
echo "================================="

# Check if Heroku CLI is installed
if ! command -v heroku &> /dev/null; then
    echo "❌ Heroku CLI not found. Please install it first:"
    echo "   https://devcenter.heroku.com/articles/heroku-cli"
    exit 1
fi

# Check if git is initialized
if [ ! -d .git ]; then
    echo "📦 Initializing git repository..."
    git init
    git add .
    git commit -m "Initial commit"
fi

# Get app name
read -p "Enter your Heroku app name (e.g., my-predictions-bot): " APP_NAME

if [ -z "$APP_NAME" ]; then
    echo "❌ App name is required"
    exit 1
fi

echo "🚀 Creating Heroku app: $APP_NAME"
heroku create "$APP_NAME"

echo "🐘 Adding PostgreSQL database..."
heroku addons:create heroku-postgresql:essential-0

echo "🔧 Setting up environment variables..."

# Generate a random secret for tasks
TASK_SECRET=$(openssl rand -hex 32)
heroku config:set TASK_SECRET="$TASK_SECRET" --app "$APP_NAME"

# Set other config vars
heroku config:set \
    STARTING_BALANCE=1000 \
    DAILY_TOPUP=200 \
    BALANCE_CAP=2000 \
    DEFAULT_LOCK="10m" \
    LMSR_B=100 \
    APP_URL="https://$APP_NAME.herokuapp.com" \
    --app "$APP_NAME"

echo "📤 Deploying to Heroku..."
git push heroku main

echo "🗄️ Initializing database..."
heroku run python -c "from app import app, db; app.app_context().push(); db.create_all()" --app "$APP_NAME"

echo ""
echo "✅ Deployment complete!"
echo ""
echo "📋 Next steps:"
echo "1. Go to https://api.slack.com/apps and create a new app"
echo "2. Add a slash command '/predict' with URL: https://$APP_NAME.herokuapp.com/"
echo "3. Get your verification token and run:"
echo "   heroku config:set SLACK_TOKEN='your-token-here' --app $APP_NAME"
echo "4. Install the app to your workspace"
echo "5. Test with '/predict help' in Slack"
echo ""
echo "📊 Optional - Set up scheduled tasks:"
echo "   heroku addons:create scheduler:standard --app $APP_NAME"
echo "   heroku addons:open scheduler --app $APP_NAME"
echo ""
echo "🔗 Your app URL: https://$APP_NAME.herokuapp.com"
echo "📝 View logs: heroku logs --tail --app $APP_NAME"
