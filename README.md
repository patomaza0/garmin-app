# Pulse

A private training and recovery app for your Garmin watch. It shows recovery, Body Battery, strain, sleep, a lap-by-lap breakdown of your last run and your weekly training plan.

App: **https://patomaza0.github.io/garmin-app/**

Your data never goes to anyone else. It lives in your own private GitHub repository, and the app on your phone reads it with a key only you have.

## Set it up (about 15 minutes, once)

You need a free GitHub account (github.com/signup) and your Garmin Connect login.

### 1. Copy this project into your own private repository

1. At the top of this page click **Use this template → Create a new repository**.
2. Name it **garmin-data**, choose **Private** and click **Create repository**.

### 2. Get your Garmin API key

1. Log in at **connect.garmin.com** on a computer.
2. Go to **Settings → Personal API** and create a key. Choose the longest expiry offered.
3. Copy the key. Garmin shows it only once.

### 3. Store the key in your repository

1. In your new `garmin-data` repository open **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**.
3. Name: `GARMIN_PERSONAL_API_KEY`. Secret: paste the key. Click **Add secret**.

Never paste the key into a file. It only goes in the secret.

### 4. Set your own details

In your repository click **config.json**, then the pencil icon, and change:

- `timezone`: where you live, for example `America/New_York` or `America/Mexico_City`
- `race_name` and `race_date`: your goal race (date as `YYYY-MM-DD`)
- `sleep_target_h`: hours of sleep you aim for

Click **Commit changes**.

### 5. Download your history

1. Open the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. Click **Sync Garmin data** on the left, then **Run workflow → Run workflow**.
3. The first run downloads a year of history and takes 10–20 minutes. When it shows a green tick, it worked. A red cross usually means the secret's name or value is wrong.

After this it syncs on its own every day, and also each time you open the app.

### 6. Make the app's access key

1. Go to **github.com/settings/personal-access-tokens/new**.
2. Name: `Pulse app`. Expiration: 1 year.
3. Repository access: **Only select repositories → garmin-data**.
4. Permissions: **Contents → Read-only** and **Actions → Read and write**. Nothing else.
5. Click **Generate token** and copy it.

### 7. Open the app

Open this link, with your GitHub username in place of `YOUR-USERNAME`:

`https://patomaza0.github.io/garmin-app/?repo=YOUR-USERNAME/garmin-data`

Paste the token and tap **Save and load**.

**iPhone:** open the link in Safari → Share → **Add to Home Screen**. Open it from the home screen and paste the token once more, because the installed app has its own storage.

**Mac:** Safari → **File → Add to Dock**.

## Good to know

- The app only shows what your watch has already uploaded. If numbers look old, open Garmin Connect on your phone so the watch syncs, then tap the badge at the top of the app.
- **Training plan:** add your weekly plan as `plan/week.json` in your repository and it appears on the Training page. Copy the format from someone who has one, or ask Claude to write it from your coach's plan.
- **Expiry:** the Garmin key and the GitHub token both expire. Garmin emails you before its key does. When either one expires, make a new one and replace the old one: the secret for Garmin, or paste the new token into the app.
- Recovery and Strain are this app's own scores in the style of WHOOP, built from resting heart rate, sleep, Body Battery and Garmin's training load. They are not medical measurements.
