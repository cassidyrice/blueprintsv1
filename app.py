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
# PDF Blueprint Download
# ---------------------------------------------------------------------------
@app.route("/download-pdf", methods=["POST"])
def download_pdf():
    email = session.get("email")
    if not email or not get_subscriber(email):
        return jsonify({"error": "Not subscribed"}), 403

    from io import BytesIO
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib.colors import HexColor, white
    from reportlab.pdfgen import canvas as pdf_canvas

    data = request.json
    if not data or "blueprint" not in data:
        return jsonify({"error": "No blueprint data"}), 400

    bp = data["blueprint"]
    a = bp.get("archetype", {})
    ap = bp.get("active_period", {})
    bc = bp.get("birth_card_spread", {})
    karma = bp.get("karma", {}).get("bc_yearly", {})
    lr = bp.get("long_range", {}).get("bc", {})
    timing = bp.get("timing", {})

    # --- Brand colors ---
    PRUSSIAN   = HexColor("#1c3f6e")
    CREAM      = HexColor("#ede8dc")
    CRIMSON    = HexColor("#b5231a")
    INK        = HexColor("#1a1a1a")
    MUTED      = HexColor("#7a7060")
    HEART_RED  = HexColor("#b5231a")
    DIAMOND_RED = HexColor("#b5231a")
    BLACK      = HexColor("#1a1a1a")
    HIGHLIGHT  = HexColor("#f0d9d4")
    GRID_BG    = HexColor("#f5f1ea")
    GRID_BORDER = HexColor("#c8c0b0")

    def suit_symbol_color(card_str):
        if not card_str: return BLACK
        if "♥" in card_str: return HEART_RED
        if "♦" in card_str: return DIAMOND_RED
        return BLACK

    def card_parts(card_str):
        if not card_str or card_str == "—": return (card_str or "—", "")
        for s in ["♥", "♦", "♣", "♠"]:
            if s in card_str:
                return (card_str.replace(s, "").strip(), s)
        return (card_str, "")

    def draw_card_text(c, card_str, x, y, font_name, font_size, white_override=False):
        rank, suit = card_parts(card_str)
        rank_color = white if white_override else BLACK
        suit_clr = (white if white_override else suit_symbol_color(card_str))
        c.setFillColor(rank_color)
        c.setFont(font_name, font_size)
        c.drawString(x, y, rank)
        rank_w = c.stringWidth(rank, font_name, font_size)
        c.setFillColor(suit_clr)
        c.drawString(x + rank_w, y, suit)

    def draw_card_centered(c, card_str, cx, cy, font_name, font_size, white_override=False):
        rank, suit = card_parts(card_str)
        full = rank + suit
        total_w = c.stringWidth(full, font_name, font_size)
        x = cx - total_w / 2
        draw_card_text(c, card_str, x, cy, font_name, font_size, white_override)

    def wrap_text(c, text, x, y, max_w, font, size, max_lines=2):
        words = text.split()
        lines, line = [], ""
        for w in words:
            test = line + w + " "
            if c.stringWidth(test, font, size) > max_w:
                lines.append(line)
                line = w + " "
            else:
                line = test
        if line: lines.append(line)
        c.setFont(font, size)
        for i, ln in enumerate(lines[:max_lines]):
            c.drawString(x, y - i * (size + 2), ln)
        return min(len(lines), max_lines) * (size + 2)

    W, H = letter  # 612 x 792
    buf = BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=letter)
    L = 30  # left margin
    R = W - 30  # right margin

    # ── PAGE BACKGROUND ──
    c.setFillColor(CREAM)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    # ── TOP BAR (compact) ──
    c.setFillColor(PRUSSIAN)
    c.rect(0, H - 52, W, 52, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(L, H - 30, "THE ANALOG")
    c.setFillColor(CRIMSON)
    c.drawString(L + c.stringWidth("THE ANALOG ", "Helvetica-Bold", 9), H - 30, "ALGORITHM")
    c.setFillColor(HexColor("#ffffff80"))
    c.setFont("Helvetica", 6)
    c.drawRightString(R, H - 26, "theanalogalgorithm.com")
    c.setFont("Helvetica", 5.5)
    c.drawRightString(R, H - 35, "the math is the math")

    # ── BIRTH CARD + PRC (side by side, compact) ──
    y = H - 72

    c.setFillColor(MUTED)
    c.setFont("Helvetica", 5.5)
    c.drawString(L, y, "BIRTH CARD")

    bc_card = a.get("birth_card", "—")
    draw_card_text(c, bc_card, L, y - 18, "Helvetica-Bold", 22)
    title = a.get("description", {}).get("title", "")
    if title:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(L, y - 32, title)

    prc_card = a.get("prc", "—")
    mid = W / 2 + 10
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 5.5)
    c.drawString(mid, y, "PLANETARY RULING CARD")
    draw_card_text(c, prc_card, mid, y - 18, "Helvetica-Bold", 22)
    prc_title = a.get("prc_description", {}).get("title", "") if a.get("prc_description") else ""
    if prc_title:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(mid, y - 32, prc_title)

    # ── DIVIDER ──
    div1 = y - 42
    c.setStrokeColor(GRID_BORDER)
    c.setLineWidth(0.5)
    c.line(L, div1, R, div1)

    # ── ACTIVE PERIOD (compact) ──
    y2 = div1 - 12
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 5.5)
    c.drawString(L, y2, "ACTIVE PERIOD")

    planet_name = ap.get("planet", "—")
    ap_card = ap.get("bc_card", "—")
    c.setFillColor(PRUSSIAN)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(L, y2 - 16, f"{planet_name}  ·  {ap_card}")
    domain = ap.get("domain", "")
    if domain:
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 6.5)
        c.drawString(L, y2 - 26, domain)

    # Three-lens (single line each)
    ib = ap.get("interpretation_bc") or {}
    lens_y = y2 - 40
    for label_name, key in [("UNDER", "under"), ("SWEET SPOT", "sweet_spot"), ("OVER", "over")]:
        text = ib.get(key, "")
        if not text: continue
        c.setFillColor(CRIMSON)
        c.setFont("Helvetica-Bold", 5)
        c.drawString(L, lens_y, label_name)
        c.setFillColor(INK)
        used = wrap_text(c, text, L, lens_y - 9, R - L, "Helvetica", 6.5, max_lines=2)
        lens_y -= 9 + used + 3

    # ── DIVIDER ──
    div2 = lens_y - 3
    c.setStrokeColor(GRID_BORDER)
    c.line(L, div2, R, div2)

    # ── PLANETARY PERIODS + ATMOSPHERE (side by side) ──
    y3 = div2 - 12
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 5.5)
    c.drawString(L, y3, "PLANETARY PERIODS")

    planets = ["Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune"]
    periods = bc.get("periods", {})
    row_h = 14
    for i, p in enumerate(planets):
        py = y3 - 14 - i * row_h
        card = periods.get(p, "—")
        is_active = p == planet_name
        if is_active:
            c.setFillColor(PRUSSIAN)
            c.rect(L - 2, py - 3, 195, 13, fill=1, stroke=0)
        c.setFillColor(white if is_active else MUTED)
        c.setFont("Helvetica", 6.5)
        c.drawString(L, py, p.upper())
        draw_card_text(c, card, L + 75, py, "Helvetica-Bold", 8, white_override=is_active)
        if is_active:
            c.setFillColor(CRIMSON)
            c.setFont("Helvetica-Bold", 5)
            c.drawString(L + 75 + c.stringWidth(card + " ", "Helvetica-Bold", 8) + 2, py + 1, "NOW")

    pluto_y = y3 - 14 - 7 * row_h - 4
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 6)
    c.drawString(L, pluto_y, "Pluto:")
    draw_card_text(c, bc.get("pluto", "—"), L + 32, pluto_y, "Helvetica-Bold", 7)
    c.setFillColor(MUTED)
    c.drawString(L + 80, pluto_y, "Result:")
    draw_card_text(c, bc.get("result", "—"), L + 115, pluto_y, "Helvetica-Bold", 7)

    # Atmosphere on right side
    atm_x = W / 2 + 20
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 5.5)
    c.drawString(atm_x, y3, "YEARLY ATMOSPHERE")

    env_card = karma.get("environment", "—") if karma else "—"
    disp_card = karma.get("displacement", "—") if karma else "—"
    lr_card = lr.get("card", "—") if lr else "—"
    lr_planet_name = lr.get("planet", "—") if lr else "—"

    for i, (label, card_val) in enumerate([("Environment", env_card), ("Displacement", disp_card), ("Long Range", lr_card)]):
        ay = y3 - 14 - i * 28
        c.setFillColor(INK)
        c.setFont("Helvetica", 6)
        c.drawString(atm_x, ay, label)
        draw_card_text(c, card_val, atm_x, ay - 14, "Helvetica-Bold", 14)
    # Long range planet label
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 5.5)
    lr_x = atm_x + c.stringWidth(lr_card + " ", "Helvetica-Bold", 14) + 2
    c.drawString(lr_x, y3 - 14 - 2 * 28 - 12, lr_planet_name)

    # ── DIVIDER ──
    div3 = pluto_y - 10
    c.setStrokeColor(GRID_BORDER)
    c.line(L, div3, R, div3)

    # ══════════════════════════════════════════════════════════════════════
    # ── 7×7 GRID + CROWN ROW ──
    # ══════════════════════════════════════════════════════════════════════

    grid = timing.get("grid", [])
    crown = timing.get("crown", [])
    age = timing.get("age", "?")
    sy_nav = timing.get("sy_nav", "?")

    # Build a map: card -> (label_symbol, is_highlighted)
    PLANET_SYMBOLS = {
        "Mercury": "☿", "Venus": "♀", "Mars": "♂", "Jupiter": "♃",
        "Saturn": "♄", "Uranus": "♅", "Neptune": "♆"
    }
    card_labels = {}
    for p_name, card_val in periods.items():
        if card_val and card_val != "—":
            card_labels[card_val] = PLANET_SYMBOLS.get(p_name, "")
    if bc.get("pluto") and bc["pluto"] != "—":
        card_labels[bc["pluto"]] = "P"
    if bc.get("result") and bc["result"] != "—":
        card_labels[bc["result"]] = "R"
    if env_card and env_card != "—":
        card_labels.setdefault(env_card, "E")
    if disp_card and disp_card != "—":
        card_labels.setdefault(disp_card, "D")
    if lr_card and lr_card != "—":
        card_labels.setdefault(lr_card, "LR")
    # Mark birth card and PRC
    if bc_card and bc_card != "—":
        card_labels.setdefault(bc_card, "★")
    if prc_card and prc_card != "—":
        card_labels.setdefault(prc_card, "◆")

    grid_top = div3 - 10
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 5.5)
    c.drawString(L, grid_top, f"SOLAR SPREAD — AGE {age} (SPREAD {sy_nav})")

    # Grid dimensions
    grid_y_start = grid_top - 14
    cell_w = (R - L) / 7
    cell_h = 24
    crown_h = 22

    ROW_PLANETS = ["☿", "♀", "♂", "♃", "♄", "♅", "♆"]
    COL_PLANETS = ["☿", "♀", "♂", "♃", "♄", "♅", "♆"]

    # Column headers
    for col in range(7):
        cx = L + col * cell_w + cell_w / 2
        c.setFillColor(PRUSSIAN)
        c.setFont("Helvetica", 6)
        c.drawCentredString(cx, grid_y_start + 2, COL_PLANETS[col])

    grid_y_start -= 4

    # Draw crown row first (3 cards centered)
    crown_y = grid_y_start - crown_h
    crown_total_w = 3 * cell_w
    crown_x_start = L + (7 * cell_w - crown_total_w) / 2

    c.setFillColor(MUTED)
    c.setFont("Helvetica", 4.5)
    c.drawString(L, crown_y + crown_h / 2 - 2, "CROWN")

    for ci, cc in enumerate(crown):
        cx = crown_x_start + ci * cell_w
        cy = crown_y
        # Highlight if labeled
        if cc in card_labels:
            c.setFillColor(HexColor("#f0d9d4"))
            c.rect(cx, cy, cell_w, crown_h, fill=1, stroke=0)
        c.setStrokeColor(GRID_BORDER)
        c.setLineWidth(0.3)
        c.rect(cx, cy, cell_w, crown_h, fill=0, stroke=1)
        # Card text
        draw_card_centered(c, cc, cx + cell_w / 2, cy + 6, "Helvetica-Bold", 7.5)
        # Label in top-right corner
        if cc in card_labels:
            c.setFillColor(CRIMSON)
            c.setFont("Helvetica-Bold", 5)
            c.drawRightString(cx + cell_w - 2, cy + crown_h - 7, card_labels[cc])

    # Draw 7×7 grid
    for row in range(7):
        for col in range(7):
            if row >= len(grid) or col >= len(grid[row]):
                continue
            card_val = grid[row][col]
            cx = L + col * cell_w
            cy = crown_y - (row + 1) * cell_h

            # Highlight if labeled
            if card_val in card_labels:
                c.setFillColor(HexColor("#f0d9d4"))
                c.rect(cx, cy, cell_w, cell_h, fill=1, stroke=0)

            # Cell border
            c.setStrokeColor(GRID_BORDER)
            c.setLineWidth(0.3)
            c.rect(cx, cy, cell_w, cell_h, fill=0, stroke=1)

            # Card text centered
            draw_card_centered(c, card_val, cx + cell_w / 2, cy + 7, "Helvetica-Bold", 7.5)

            # Label in top-right corner
            if card_val in card_labels:
                c.setFillColor(CRIMSON)
                c.setFont("Helvetica-Bold", 5)
                c.drawRightString(cx + cell_w - 2, cy + cell_h - 7, card_labels[card_val])

        # Row planet label on left margin
        row_label_y = crown_y - (row + 1) * cell_h + cell_h / 2 - 3
        c.setFillColor(PRUSSIAN)
        c.setFont("Helvetica", 6)
        # Shift grid right to make room? No — put label in left gutter
        # Actually let's put it at the left edge of each row

    # ── LEGEND ──
    legend_y = crown_y - 8 * cell_h - 6
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 5)
    legend_items = [
        ("☿", "Mercury"), ("♀", "Venus"), ("♂", "Mars"), ("♃", "Jupiter"),
        ("♄", "Saturn"), ("♅", "Uranus"), ("♆", "Neptune"),
        ("P", "Pluto"), ("R", "Result"), ("E", "Environ."), ("D", "Displace."),
        ("LR", "Long Range"), ("★", "Birth Card"), ("◆", "PRC")
    ]
    lx = L
    for sym, label in legend_items:
        c.setFillColor(CRIMSON)
        c.setFont("Helvetica-Bold", 5)
        c.drawString(lx, legend_y, sym)
        sw = c.stringWidth(sym, "Helvetica-Bold", 5)
        c.setFillColor(MUTED)
        c.setFont("Helvetica", 4.5)
        c.drawString(lx + sw + 1, legend_y, label)
        lx += sw + c.stringWidth(label, "Helvetica", 4.5) + 8
        if lx > R - 30:
            lx = L
            legend_y -= 8

    # ── BOTTOM BAR ──
    c.setFillColor(PRUSSIAN)
    c.rect(0, 0, W, 32, fill=1, stroke=0)
    c.setStrokeColor(CRIMSON)
    c.setLineWidth(1.5)
    c.line(0, 32, W, 32)
    c.setFillColor(HexColor("#ffffff70"))
    c.setFont("Helvetica", 6)
    c.drawCentredString(W / 2, 13, "The Analog Algorithm  ·  Mathematical Pattern Recognition  ·  theanalogalgorithm.com")

    c.save()
    buf.seek(0)

    from flask import send_file
    card_name = bc_card.replace("♥", "H").replace("♦", "D").replace("♣", "C").replace("♠", "S")
    return send_file(
        buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"blueprint_{card_name}.pdf"
    )


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
