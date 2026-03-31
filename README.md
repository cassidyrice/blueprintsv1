# Card Blueprints Dashboard

Flask app wrapping the `calculate_blueprint.py` engine with Stripe subscription gating.

## Local dev

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your values
flask run
# visit http://localhost:5000/dev-login to bypass Stripe in dev
```

## Deploy to Railway (recommended — free tier)

1. Push this folder to a GitHub repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Select your repo
4. Add environment variables (from .env.example) in Railway dashboard
5. Railway auto-detects Procfile and deploys

## Deploy to Render (alternative)

1. Push to GitHub
2. render.com → New → Web Service → Connect repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`
5. Add env vars in Render dashboard

## Point your domain

In SiteGround DNS, add a CNAME record:
- Name: `app`
- Value: your Railway/Render app URL
- This routes `app.cardblueprints.com` to the Flask app

## Stripe setup

1. Create two recurring prices in Stripe dashboard:
   - $9/month → copy Price ID → STRIPE_PRICE_CONSUMER
   - $29/month → copy Price ID → STRIPE_PRICE_PRACTITIONER
2. Set up webhook endpoint: `https://app.cardblueprints.com/webhook`
   - Event: `customer.subscription.deleted`
   - Copy webhook secret → STRIPE_WEBHOOK_SECRET

## Production notes

- Current subscriber storage is in-memory (resets on redeploy)
- For production: replace `SUBSCRIBERS` dict with SQLite or Postgres
- Add `flask-sqlalchemy` + a users table: email, tier, created_at
- Railway and Render both offer free Postgres add-ons
