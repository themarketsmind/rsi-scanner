# RSI 50 Scanner

Scans US and UK stocks plus the top 150 crypto coins every day for three RSI(14) setups on the daily, weekly and monthly charts, and shows them on a dashboard. You can also track positions and get take-profit / accept-loss alerts.

**Scanned:** S&P 500, FTSE 100, FTSE 250 (about 850 stocks), the Nasdaq-100 names that aren't in the S&P 500, anything you add to `extra_tickers.txt`, and the top 150 coins by volume on Binance (shown as `BTC-USD`, `SOL-USD` and so on). Use the market filter to show just one.

## Setups

| Setup | Rule |
|---|---|
| Tap | RSI is 48–52 right now |
| Bounce | RSI touched 46–53 in the recent bars, and is now 55–60 and rising |
| Reclaim | RSI was below 45 in the recent bars and has just crossed above 50 |

"Recent" means the last 10 daily bars, 8 weekly bars, or 6 monthly bars. Weekly and monthly only use closed candles. Stocks trading less than 2 million a day (in $ or £) are hidden unless you tick "Include illiquid". Every number is a setting at the top of `scanner.py`.

## Position alerts

Add a position (or tap **Track** on any stock) with the timeframe that gave you the signal, your entry price and number of shares. The alert uses the RSI on that same timeframe:

- **Take profit**: RSI reaches 70 or above (labelled "overextended" above 75)
- **Accept loss**: RSI falls below 50
- **Holding**: anything in between

Profit and loss is shown in pounds. US positions store the GBP/USD rate at entry, so the figure includes currency moves like your broker's does.

Positions are saved in your browser. Use "Back up or move your positions" to copy them to another device.

## Washouts tab

Looks for capitulation flushes that often mark reversal entries.

- **Market meter**: the share of stocks with daily RSI under 30. Above 10% is stressed; above 25% is a market-wide liquidation event (Covid March 2020, April 2025 tariff crash). The chart shows 10 years of this, and the table shows what the S&P 500 did 1, 3 and 6 months after each past event.
- **Stocks flushing now**: fell 15%+ from the 20-day high, daily RSI hit 25 or lower, and volume spiked 2.5× normal, all in the last 5 days. **Reversal trigger** means today closed above yesterday's high, printed a long lower wick, or RSI climbed back over 30. **Still flushing** means no turn yet.

**Crypto** (toggle at the top of the tab): the meter is the median liquidation wick, meaning how far the typical coin's low fell below the previous close. Cascades like 12 Mar 2020, 19 May 2021 and 10 Oct 2025 show up as most coins wicking 12%+ on the same day, and the table shows what Bitcoin did afterwards. Coins flush at a 25%+ fall rather than 15%. Leverage comes from Hyperliquid perps: funding (annualised) and the 3-day change in open interest. A 15%+ drop in open interest on a flushing coin counts as a reversal trigger. Open interest history starts from your first run, so the 3-day change appears after three days.

The breadth history only uses stocks in the indexes today, so stocks that went bust in past crashes are missing. Past events will read slightly milder than they really were.

## One-time setup (about 10 minutes, free)

1. Create a free account at github.com if you don't have one.
2. Click **+** (top right) → **New repository**. Name it `rsi-scanner`, set it to **Public**, and click **Create repository**.
3. On the new repo page click **uploading an existing file**. Drag in everything from this folder, **including the `.github` folder**. Click **Commit changes**.
   - If your computer hides the `.github` folder: on the repo page click **Add file → Create new file**, type the name `.github/workflows/scan.yml`, paste in the contents of that file, and commit.
4. Go to **Settings → Actions → General**, scroll to **Workflow permissions**, choose **Read and write permissions**, and click **Save**.
5. Go to **Settings → Pages**. Under **Branch** choose `main` and `/ (root)`, and click **Save**.
6. Go to the **Actions** tab → **Daily RSI scan** → **Run workflow**. The first run takes a few minutes.
7. Your dashboard is at `https://YOUR-USERNAME.github.io/rsi-scanner/`. Add it to your phone's home screen.

After that it runs on its own at 00:15 UTC every day, after the US close and the daily crypto candle close (01:15 UK time in summer).

## Changing things

- **Thresholds, markets, liquidity filter**: edit the settings block at the top of `scanner.py` on GitHub (pencil icon), then commit.
- **Extra stocks**: add them to `extra_tickers.txt`. US: `PLTR`. UK: `RR.L`.
- **Test without internet**: `python scanner.py --demo` writes fake data.

## Telegram later

The scanner already produces everything needed. Adding Telegram means a bot token stored as a GitHub secret and positions moved into a `positions.json` file in the repo, so the scanner can see them.
