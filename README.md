# Stock News Reel Bot (every 2 hours)

Every 2 hours (08:00 to 22:00 IST) GitHub Actions will:
1. fetch the latest Indian stock-market headlines (Google News RSS),
2. skip headlines already posted,
3. build a ~30 s vertical video: fresh gradient background, Nifty/Sensex card,
   4 news slides, outro + disclaimer, with background music,
4. publish it as an Instagram Reel through the official Graph API.

## Deploy to GitHub

```bash
cd insta-stock-bot
git init -b main
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

Create the empty **public** repo on github.com first. Public is required so
Instagram can download the video. Use `git` or GitHub Desktop rather than
drag-and-drop in the browser, because the hidden `.github` folder is often skipped.

Then, in the repo on github.com:

1. **Settings > Secrets and variables > Actions > New repository secret**
   - `IG_USER_ID`
   - `IG_ACCESS_TOKEN`
   - `ANTHROPIC_API_KEY` (optional)
2. **Settings > Actions > General > Workflow permissions** > *Read and write permissions* > Save.
3. **Actions tab** > *Stock news Reel (every 2 hours)* > **Run workflow**
   with *dry run* ticked. Check the log, then run again with dry run unticked to post for real.

## Getting the Instagram token (no password is ever used)
1. Instagram app: Settings > Account type and tools > switch to a **Professional** account (Creator is fine). No Facebook Page is needed.
2. Go to https://developers.facebook.com, log in with a Facebook account (one-time), register as a developer, then **Create app** (type: Business).
3. In the app dashboard add the product **Instagram**, then open **API setup with Instagram login**.
4. Add your Instagram account as a tester: App roles > Roles > add people > **Instagram Tester**, enter your username.
   Then accept the invite in Instagram (Settings > Apps and websites > Tester invites; menu names can vary). Do not skip this.
5. Back in **Instagram > API setup with Instagram login** click **Generate token** next to your account, grant `instagram_business_content_publish`, and copy the token.
6. GitHub secrets: `IG_ACCESS_TOKEN` = that token. `IG_USER_ID` = the account ID shown on that page (or leave it unset, the bot uses `me`).
7. The token lasts 60 days. Refresh it before expiry:
   `https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=YOUR_TOKEN`
   and update the GitHub secret with the new value.

## Music
- By default the bot **synthesizes an original ambient track** each run, so there are no copyright problems.
- To use your own, drop royalty-free `.mp3`/`.wav` files into the `music/` folder
  (for example from Pixabay Music or YouTube Audio Library, with licences that allow social media use).
  A random file is picked for each video.
- Do NOT add commercial songs. They get muted or taken down, and the API cannot
  attach Instagram's own trending-audio library.

## Run locally
Needs Python 3.10+ and ffmpeg.
```bash
pip install -r requirements.txt
DRY_RUN=1 python main.py all      # creates out/reel.mp4
```

## Change the schedule
Edit the `cron` line in `.github/workflows/post.yml` (times are UTC).
`30 2-16/2 * * *` = every 2 hours, 08:00-22:00 IST. Use `0 */2 * * *` for 24/7.
GitHub may start scheduled runs a few minutes late.

## Important
- **SEBI (India):** the videos are neutral news summaries with a disclaimer.
  Do not add buy/sell tips or targets unless you are SEBI-registered.
- **News credit:** headlines are summarised and attributed. Keep the source line.
- **Quality:** 12 near-identical Reels a day can look like spam and hurt reach.
  Start with 3 to 4 a day (edit the cron) and watch your analytics.
- Never commit tokens. Use GitHub Secrets only.
