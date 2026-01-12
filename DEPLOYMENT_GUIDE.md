# Slack Predictions Bot - Deployment Guide

This guide will walk you through hosting your Slack prediction market bot on Heroku and configuring it with Slack.

## Prerequisites

1. **Heroku Account**: Sign up at [heroku.com](https://heroku.com)
2. **Heroku CLI**: Install from [devcenter.heroku.com/articles/heroku-cli](https://devcenter.heroku.com/articles/heroku-cli)
3. **Git**: Ensure git is installed on your machine
4. **Slack Admin Access**: You need permission to add apps to your Slack workspace

## Step 1: Set Up the Code

1. **Create a new directory and initialize git:**
   ```bash
   mkdir predictions-bot
   cd predictions-bot
   git init
   ```

2. **Add the files** (app.py, requirements.txt, Procfile, runtime.txt from this conversation)

3. **Commit the code:**
   ```bash
   git add .
   git commit -m "Initial commit"
   ```

## Step 2: Deploy to Heroku

1. **Create a Heroku app:**
   ```bash
   heroku create your-predictions-bot-name
   ```
   (Replace `your-predictions-bot-name` with a unique name)

2. **Add PostgreSQL database:**
   ```bash
   heroku addons:create heroku-postgresql:mini
   ```

3. **Set environment variables:**
   ```bash
   # Required - you'll get this from Slack in Step 3
   heroku config:set SLACK_TOKEN="your-slack-verification-token"
   
   # Optional - customize these if desired
   heroku config:set STARTING_BALANCE=1000
   heroku config:set DAILY_TOPUP=200
   heroku config:set BALANCE_CAP=2000
   heroku config:set DEFAULT_LOCK="10m"
   heroku config:set LMSR_B=100
   
   # Security for scheduled tasks (optional but recommended)
   heroku config:set TASK_SECRET="$(openssl rand -hex 32)"
   ```

4. **Deploy:**
   ```bash
   git push heroku main
   ```

5. **Initialize the database:**
   ```bash
   heroku run python -c "from app import app, db; app.app_context().push(); db.create_all()"
   ```

## Step 3: Configure Slack App

1. **Go to Slack API**: Visit [api.slack.com/apps](https://api.slack.com/apps)

2. **Create New App**:
   - Click "Create New App" → "From scratch"
   - App Name: "Predictions Bot" (or your choice)
   - Choose your workspace
   - Click "Create App"

3. **Add Slash Command**:
   - In the left sidebar, click "Slash Commands"
   - Click "Create New Command"
   - Command: `/predict`
   - Request URL: `https://your-predictions-bot-name.herokuapp.com/`
   - Short Description: "Prediction market trading"
   - Usage Hint: `/predict help`
   - Click "Save"

4. **Get Verification Token**:
   - In the left sidebar, click "Basic Information"
   - Scroll down to "App Credentials"
   - Copy the "Verification Token"

5. **Update Heroku with the token**:
   ```bash
   heroku config:set SLACK_TOKEN="xoxp-your-verification-token-here"
   ```

6. **Install App to Workspace**:
   - In the left sidebar, click "Install App"
   - Click "Install to Workspace"
   - Click "Allow"

## Step 4: Test the Bot

1. **In any Slack channel, try:**
   ```
   /predict help
   ```

2. **Create your first market:**
   ```
   /predict create test_market "Will it rain tomorrow?" "tomorrow 6pm" 15m "YES,NO"
   ```

3. **Place a bet:**
   ```
   /predict buy test_market YES 100
   ```

4. **Check your balance:**
   ```
   /predict balance
   ```

## Step 5: Optional - Set Up Scheduled Tasks

To automatically handle daily top-ups and monthly cycles:

1. **Install Heroku Scheduler:**
   ```bash
   heroku addons:create scheduler:standard
   ```

2. **Open scheduler:**
   ```bash
   heroku addons:open scheduler
   ```

3. **Add daily job** (runs every day at midnight UTC):
   - Command: `curl "$APP_URL/tasks/daily_topup?secret=$TASK_SECRET"`
   - Frequency: Daily
   - Time: 00:00 UTC

4. **Add monthly job** (runs on 1st of each month):
   - Command: `curl "$APP_URL/tasks/monthly_close?secret=$TASK_SECRET"`
   - Frequency: Monthly
   - Day: 1
   - Time: 01:00 UTC

5. **Set required environment variables:**
   ```bash
   heroku config:set APP_URL="https://your-predictions-bot-name.herokuapp.com"
   ```

## Step 6: Admin Features (Optional)

To enable admin-only month closing:

1. **Get your Slack user ID:**
   - In Slack, right-click on your profile → "Copy member ID"
   
2. **Set admin config:**
   ```bash
   heroku config:set ADMIN_SLACK_IDS="U01234567,U09876543"
   ```
   (Replace with actual Slack user IDs, comma-separated for multiple admins)

## Monitoring & Maintenance

- **View logs**: `heroku logs --tail`
- **Check app status**: `heroku ps`
- **Scale up/down**: `heroku ps:scale web=1`
- **Database access**: `heroku pg:psql`

## Troubleshooting

**Bot not responding:**
- Check logs: `heroku logs --tail`
- Verify SLACK_TOKEN is set: `heroku config:get SLACK_TOKEN`
- Ensure Request URL in Slack matches your Heroku URL

**Database errors:**
- Run migrations: `heroku run python -c "from app import app, db; app.app_context().push(); db.create_all()"`

**Slash command not working:**
- Verify the app is installed to your workspace
- Check that Request URL is correct
- Ensure the app has proper permissions

## Usage Examples

```bash
# Create markets
/predict create game_night "Who will win game night?" "tomorrow 8pm" 30m "ALICE,BOB,CHARLIE"

# Trade
/predict buy game_night ALICE 150
/predict sell game_night ALICE 50

# Check status
/predict show game_night
/predict balance
/predict leaderboard

# Resolve (creator only)
/predict resolve game_night ALICE
```

## Costs

- **Heroku**: Free tier available (sleeps after 30 min inactivity), or $7/month for hobby tier
- **PostgreSQL**: Free tier available (10k rows), or $9/month for basic
- **Total**: Can run for free with limitations, or ~$16/month for always-on

Your bot is now ready for your team to start making predictions!
