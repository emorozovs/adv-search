# adv-search

Hourly GitHub Actions job that scans [ss.lv car listings](https://www.ss.lv/ru/transport/cars/)
and garage listings for ads matching the criteria in `config.yaml` and emails new matches
(one email per search `group`: cars, garages).

## Setup

1. Edit `config.yaml` (section path, fuel, year/price range, optional text regex).
2. Gmail: enable 2-Step Verification, then create an App Password at
   https://myaccount.google.com/apppasswords
3. Repo → Settings → Secrets and variables → Actions → add:

   | Secret          | Example                  |
   |-----------------|--------------------------|
   | `SMTP_USER`     | your Gmail address       |
   | `SMTP_PASSWORD` | the 16-char App Password |
   | `EMAIL_TO`      | where to send alerts     |
   | `SMTP_HOST`     | optional, default `smtp.gmail.com` |
   | `SMTP_PORT`     | optional, default `465` (SSL)      |

4. Actions tab → "ss.lv car search" → Run workflow (first test).

Each run writes all current matches to `results.md` and commits it (only when it changed).
Email goes out only for ads not notified before; IDs are kept in `state.json`.
Until the SMTP secrets are set, email is skipped with a warning and the run stays green.

## Local run

```
pip install -r requirements.txt
python search.py --dry-run
```
