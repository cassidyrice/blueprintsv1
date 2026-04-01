#!/usr/bin/env python3
"""
Card Blueprints Dashboard — Flask App
Stripe subscription gate + Blueprint calculator
"""

import os
import json
from datetime import date, datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
import stripe

load_dotenv()  # loads .env if present — no-op in production

import anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

from calculate_blueprint import calculate_blueprint

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

# ---------------------------------------------------------------------------
# Database — SQLite locally, Postgres on Railway/Render via DATABASE_URL
# ---------------------------------------------------------------------------
db_url = os.environ.get("DATABASE_URL", "sqlite:///cardblueprints.db")
# Railway Postgres URLs start with postgres:// — SQLAlchemy needs postgresql://
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Subscriber(db.Model):
    __tablename__ = "subscribers"
    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(255), unique=True, nullable=False, index=True)
    tier          = db.Column(db.String(32), nullable=False, default="consumer")
    stripe_customer_id     = db.Column(db.String(128), nullable=True)
    stripe_subscription_id = db.Column(db.String(128), nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    profiles      = db.relationship("Profile", backref="subscriber", lazy=True, cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "tier": self.tier,
            "profiles": [p.to_dict() for p in self.profiles]
        }


class Profile(db.Model):
    __tablename__ = "profiles"
    id            = db.Column(db.Integer, primary_key=True)
    subscriber_id = db.Column(db.Integer, db.ForeignKey("subscribers.id"), nullable=False)
    name          = db.Column(db.String(128), nullable=False)
    birth_month   = db.Column(db.Integer, nullable=False)
    birth_day     = db.Column(db.Integer, nullable=False)
    birth_year    = db.Column(db.Integer, nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "birth_month": self.birth_month,
            "birth_day": self.birth_day,
            "birth_year": self.birth_year,
        }


# Create tables on startup
with app.app_context():
    db.create_all()

# ---------------------------------------------------------------------------
# Stripe config
# ---------------------------------------------------------------------------
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")

PRICE_CONSUMER     = os.environ.get("STRIPE_PRICE_CONSUMER")
PRICE_PRACTITIONER = os.environ.get("STRIPE_PRICE_PRACTITIONER")
PRICE_DEEPREAD     = os.environ.get("STRIPE_PRICE_DEEPREAD")

DOMAIN = os.environ.get("DOMAIN", "http://localhost:5000")

# ---------------------------------------------------------------------------
# DB helpers (replace the old in-memory functions)
# ---------------------------------------------------------------------------

def get_subscriber(email):
    sub = Subscriber.query.filter_by(email=email).first()
    return sub.to_dict() if sub else None


def set_subscriber(email, tier, stripe_customer_id=None, stripe_subscription_id=None):
    sub = Subscriber.query.filter_by(email=email).first()
    if sub:
        sub.tier = tier
        if stripe_customer_id:
            sub.stripe_customer_id = stripe_customer_id
        if stripe_subscription_id:
            sub.stripe_subscription_id = stripe_subscription_id
    else:
        sub = Subscriber(
            email=email,
            tier=tier,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription_id,
        )
        db.session.add(sub)
    db.session.commit()


def delete_subscriber_by_subscription(subscription_id):
    sub = Subscriber.query.filter_by(stripe_subscription_id=subscription_id).first()
    if sub:
        db.session.delete(sub)
        db.session.commit()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", key=STRIPE_PUBLISHABLE_KEY)


@app.route("/dashboard")
def dashboard():
    email = session.get("email")
    if not email:
        return redirect(url_for("index"))
    sub = get_subscriber(email)
    if not sub:
        return redirect(url_for("index"))
    return render_template("dashboard.html",
                           email=email,
                           tier=sub["tier"],
                           profiles=sub["profiles"])


@app.route("/calculate", methods=["POST"])
def calculate():
    email = session.get("email")
    if not email or not get_subscriber(email):
        return jsonify({"error": "Not subscribed"}), 403

    data = request.json
    try:
        month = int(data["month"])
        day = int(data["day"])
        year = int(data["year"])
        target = date.fromisoformat(data["target_date"]) if data.get("target_date") else date.today()
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    result = calculate_blueprint(month, day, year, target)
    return jsonify(result)


@app.route("/compatibility", methods=["POST"])
def compatibility():
    email = session.get("email")
    if not email or not get_subscriber(email):
        return jsonify({"error": "Not subscribed"}), 403

    data = request.json
    try:
        p1 = calculate_blueprint(int(data["p1_month"]), int(data["p1_day"]), int(data["p1_year"]))
        p2 = calculate_blueprint(int(data["p2_month"]), int(data["p2_day"]), int(data["p2_year"]))
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    # Build compatibility overlay
    p1_a = p1["archetype"]
    p2_a = p2["archetype"]

    mirror = None
    if p1_a["birth_card"] == p2_a["prc"]:
        mirror = {"type": "P1 BC = P2 PRC", "card": p1_a["birth_card"]}
    elif p1_a["prc"] == p2_a["birth_card"]:
        mirror = {"type": "P1 PRC = P2 BC", "card": p1_a["prc"]}
    elif p1_a["birth_card"] == p2_a["birth_card"]:
        mirror = {"type": "SAME BC", "card": p1_a["birth_card"]}
    elif p1_a["prc"] == p2_a["prc"]:
        mirror = {"type": "SAME PRC", "card": p1_a["prc"]}

    p1_cards = set(p1["birth_card_spread"]["periods"].values()) | set(p1["prc_spread"]["periods"].values())
    p2_cards = set(p2["birth_card_spread"]["periods"].values()) | set(p2["prc_spread"]["periods"].values())
    shared_periods = list(p1_cards & p2_cards)

    p1_karma = p1["karma"]["bc_yearly"]
    p2_karma = p2["karma"]["bc_yearly"]
    karma_crossover = []
    if p1_karma and p2_karma:
        if p1_karma["environment"] == p2_karma["displacement"]:
            karma_crossover.append(f"P1 environment ({p1_karma['environment']}) = P2 displacement")
        if p2_karma["environment"] == p1_karma["displacement"]:
            karma_crossover.append(f"P2 environment ({p2_karma['environment']}) = P1 displacement")
        if p1_karma["environment"] == p2_karma["environment"]:
            karma_crossover.append(f"Shared environment card: {p1_karma['environment']}")

    return jsonify({
        "person1": p1,
        "person2": p2,
        "overlay": {
            "mirror": mirror,
            "shared_period_cards": shared_periods,
            "karma_crossover": karma_crossover,
            "p1_active": p1["active_period"],
            "p2_active": p2["active_period"],
        }
    })


# ---------------------------------------------------------------------------
# Stripe checkout
# ---------------------------------------------------------------------------

@app.route("/checkout/<tier>")
def checkout(tier):
    if tier == "consumer":
        price_id = PRICE_CONSUMER
    elif tier == "practitioner":
        price_id = PRICE_PRACTITIONER
    elif tier == "deepread":
        price_id = PRICE_DEEPREAD
    else:
        price_id = None

    if not price_id:
        return "Stripe not configured", 500
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=DOMAIN + "/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=DOMAIN + "/",
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        return str(e), 500


@app.route("/success")
def success():
    session_id = request.args.get("session_id")
    if not session_id:
        return redirect(url_for("index"))
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        email = checkout_session.customer_details.email
        customer_id = checkout_session.customer
        sub = stripe.Subscription.retrieve(checkout_session.subscription)
        price_id = sub["items"]["data"][0]["price"]["id"]
        tier = "practitioner" if price_id == PRICE_PRACTITIONER else "consumer"
        set_subscriber(email, tier,
                       stripe_customer_id=customer_id,
                       stripe_subscription_id=sub["id"])
        session["email"] = email
        return redirect(url_for("dashboard"))
    except Exception as e:
        return str(e), 500


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return "", 400

    if event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        delete_subscriber_by_subscription(sub["id"])

    return "", 200


# ---------------------------------------------------------------------------
# Profile management (practitioner tier)
# ---------------------------------------------------------------------------

@app.route("/profiles/save", methods=["POST"])
def save_profile():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Not logged in"}), 403

    sub = Subscriber.query.filter_by(email=email).first()
    if not sub:
        return jsonify({"error": "Not subscribed"}), 403

    # Consumer tier: max 1 profile
    if sub.tier == "consumer" and len(sub.profiles) >= 1:
        return jsonify({"error": "Consumer plan limited to 1 profile. Upgrade to Professional for unlimited."}), 403

    data = request.json
    try:
        profile = Profile(
            subscriber_id=sub.id,
            name=data["name"],
            birth_month=int(data["month"]),
            birth_day=int(data["day"]),
            birth_year=int(data["year"]),
        )
        db.session.add(profile)
        db.session.commit()
        return jsonify({"success": True, "profile": profile.to_dict()})
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/profiles/delete/<int:profile_id>", methods=["DELETE"])
def delete_profile(profile_id):
    email = session.get("email")
    if not email:
        return jsonify({"error": "Not logged in"}), 403

    sub = Subscriber.query.filter_by(email=email).first()
    if not sub:
        return jsonify({"error": "Not subscribed"}), 403

    profile = Profile.query.filter_by(id=profile_id, subscriber_id=sub.id).first()
    if not profile:
        return jsonify({"error": "Profile not found"}), 404

    db.session.delete(profile)
    db.session.commit()
    return jsonify({"success": True})


@app.route("/profiles/list")
def list_profiles():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Not logged in"}), 403
    sub = Subscriber.query.filter_by(email=email).first()
    if not sub:
        return jsonify({"error": "Not subscribed"}), 403
    return jsonify({"profiles": [p.to_dict() for p in sub.profiles]})



# ---------------------------------------------------------------------------
# AI Summary — Claude Haiku, deep_read tier
# ---------------------------------------------------------------------------
@app.route("/summary", methods=["POST"])
def summary():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Not logged in"}), 403

    sub = Subscriber.query.filter_by(email=email).first()
    if not sub or sub.tier != "deep_read":
        return jsonify({"error": "Deep Read tier required"}), 403

    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "API not configured"}), 500

    data = request.json
    blueprint = data.get("blueprint")
    if not blueprint:
        return jsonify({"error": "No blueprint data"}), 400

    a = blueprint.get("archetype", {})
    ap = blueprint.get("active_period", {})
    bc_spread = blueprint.get("birth_card_spread", {})
    karma = blueprint.get("karma", {}).get("bc_yearly", {})
    lr = blueprint.get("long_range", {}).get("bc", {})

    prompt = f"""You are a deadpan, actuarial pattern analyst. No mysticism. No wellness language. The math is the math.

Write a 3-paragraph spread summary. Dry, direct, pattern-recognition voice.

BIRTH CARD: {a.get("birth_card")} — {a.get("description", {}).get("title", "")}
PRC: {a.get("prc")}
ACTIVE PERIOD: {ap.get("planet")} — card {ap.get("bc_card")} ({ap.get("domain")})
ACTIVE LENS: {ap.get("interpretation_bc", {}).get("sweet_spot", "") if ap.get("interpretation_bc") else ""}
ENVIRONMENT: {karma.get("environment") if karma else "—"}
DISPLACEMENT: {karma.get("displacement") if karma else "—"}
LONG RANGE: {lr.get("card") if lr else "—"} ({lr.get("planet") if lr else "—"})
PERIODS: {", ".join([f"{p}:{c}" for p,c in bc_spread.get("periods", {}).items()])}

Paragraph 1: Birth card + PRC structural pattern.
Paragraph 2: Active period — what is being asked right now.
Paragraph 3: Yearly atmosphere + long range operating conditions.

2-3 sentences each. No bullets. No headers. No fluff."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text
        return jsonify({"summary": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# Dev login bypass (remove in production)
# ---------------------------------------------------------------------------
@app.route("/dev-login")
def dev_login():
    if os.environ.get("FLASK_ENV") != "production":
        session["email"] = "dev@test.com"
        set_subscriber("dev@test.com", "practitioner")
        return redirect(url_for("dashboard"))
    return "Not available", 404


if __name__ == "__main__":
    app.run(debug=True, port=5000)
