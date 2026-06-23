import os
import re
import json
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory, abort
from dotenv import load_dotenv
import stripe

load_dotenv()

app = Flask(__name__, static_folder=None)
app.secret_key = os.urandom(32)

# ─── Configuration ──────────────────────────────────────
stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')
WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET', '')

PRICE_MAP = {
    'maps_monthly':      os.getenv('STRIPE_PRICE_MAPS_MONTHLY', ''),
    'maps_yearly':       os.getenv('STRIPE_PRICE_MAPS_YEARLY', ''),
    'instagram_monthly': os.getenv('STRIPE_PRICE_INSTA_MONTHLY', ''),
    'instagram_yearly':  os.getenv('STRIPE_PRICE_INSTA_YEARLY', ''),
    'bundle_monthly':    os.getenv('STRIPE_PRICE_BUNDLE_MONTHLY', ''),
    'bundle_yearly':     os.getenv('STRIPE_PRICE_BUNDLE_YEARLY', ''),
}

SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASS = os.getenv('SMTP_PASS', '')
CONTACT_EMAIL_TO = os.getenv('CONTACT_EMAIL_TO', 'support@clientsniper.com')
SITE_URL = os.getenv('SITE_URL', 'http://localhost:3000')

EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')

# ─── Rate Limiting (simple in-memory) ──────────────────
_rate_store = {}


def rate_limit(max_requests, window_seconds):
    """Simple per-IP rate limiter."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.remote_addr or '0.0.0.0'
            key = f'{f.__name__}:{ip}'
            now = time.time()
            window = _rate_store.get(key, [])
            window = [t for t in window if now - t < window_seconds]
            if len(window) >= max_requests:
                return jsonify({'error': 'Too many requests. Please try again later.'}), 429
            window.append(now)
            _rate_store[key] = window
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ─── Email Helper ───────────────────────────────────────
def send_email(to, subject, text_body, html_body=None, reply_to=None):
    """Send email via SMTP."""
    if not SMTP_USER or not SMTP_PASS:
        print(f'[EMAIL SKIPPED - no SMTP configured] To: {to}, Subject: {subject}')
        return True

    msg = MIMEMultipart('alternative')
    msg['From'] = f'"ClientSniper" <{SMTP_USER}>'
    msg['To'] = to
    msg['Subject'] = subject
    if reply_to:
        msg['Reply-To'] = reply_to

    msg.attach(MIMEText(text_body, 'plain'))
    if html_body:
        msg.attach(MIMEText(html_body, 'html'))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
    return True


# ─── Static Pages ──────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


# API and webhook routes are defined below, then the static catch-all at the end


# ─── Stripe Checkout ───────────────────────────────────
@app.route('/create-checkout-session', methods=['POST'])
@rate_limit(max_requests=30, window_seconds=900)
def create_checkout_session():
    # Support both JSON and form-encoded
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form.to_dict()

    lookup_key = data.get('lookup_key', '').strip()
    email = data.get('email', '').strip()
    name = data.get('name', '').strip()

    if not lookup_key or not email or not name:
        return jsonify({'error': 'Missing required fields.'}), 400

    if not EMAIL_RE.match(email):
        return jsonify({'error': 'Invalid email address.'}), 400

    price_id = PRICE_MAP.get(lookup_key)
    if not price_id:
        return jsonify({'error': 'Invalid plan selected.'}), 400

    try:
        session = stripe.checkout.Session.create(
            mode='subscription',
            payment_method_types=['card'],
            customer_email=email,
            metadata={'name': name[:100], 'lookup_key': lookup_key},
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            subscription_data={
                'trial_period_days': 30,
                'metadata': {'name': name[:100], 'lookup_key': lookup_key},
            },
            success_url=f'{SITE_URL}/payment.html?session_id={{CHECKOUT_SESSION_ID}}&status=success',
            cancel_url=f'{SITE_URL}/payment.html?status=cancelled',
        )
        return jsonify({'url': session.url})
    except stripe.error.StripeError as e:
        print(f'Stripe error: {e}')
        return jsonify({'error': 'Could not create checkout session.'}), 500


# ─── Stripe Webhook ────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature', '')

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        print(f'Webhook error: {e}')
        return jsonify({'error': str(e)}), 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        print(f"✅ Payment successful for {session.get('customer_email')} — Session: {session['id']}")
        # TODO: Provision user access, store in DB, send welcome email

    elif event['type'] == 'customer.subscription.deleted':
        sub = event['data']['object']
        print(f"❌ Subscription cancelled: {sub['id']}")
        # TODO: Revoke access

    else:
        print(f"Unhandled event: {event['type']}")

    return jsonify({'received': True})


# ─── Contact Form ──────────────────────────────────────
@app.route('/api/contact', methods=['POST'])
@rate_limit(max_requests=5, window_seconds=3600)
def contact():
    data = request.get_json(silent=True) or {}

    name = data.get('name', '').strip()[:100]
    email = data.get('email', '').strip()
    subject = data.get('subject', '').strip()[:200] or 'No subject'
    message = data.get('message', '').strip()[:5000]

    if not name or not email or not message:
        return jsonify({'error': 'Name, email, and message are required.'}), 400

    if not EMAIL_RE.match(email):
        return jsonify({'error': 'Invalid email address.'}), 400

    try:
        # Send to support
        send_email(
            to=CONTACT_EMAIL_TO,
            subject=f'[ClientSniper Contact] {subject}',
            text_body=f'Name: {name}\nEmail: {email}\nSubject: {subject}\n\nMessage:\n{message}',
            html_body=f'''
                <div style="font-family:sans-serif;max-width:600px">
                  <h2 style="color:#C9A465;border-bottom:1px solid #eee;padding-bottom:8px">New Contact Message</h2>
                  <p><strong>Name:</strong> {name}</p>
                  <p><strong>Email:</strong> {email}</p>
                  <p><strong>Subject:</strong> {subject}</p>
                  <hr style="border:none;border-top:1px solid #eee;margin:16px 0">
                  <p style="white-space:pre-wrap">{message}</p>
                </div>
            ''',
            reply_to=email,
        )

        # Auto-reply to sender
        send_email(
            to=email,
            subject='We got your message — ClientSniper',
            text_body=f'Hi {name},\n\nThanks for reaching out! We\'ve received your message and will get back to you within 24 hours.\n\nBest,\nThe ClientSniper Team',
            html_body=f'''
                <div style="font-family:sans-serif;max-width:600px;color:#333">
                  <h2 style="color:#C9A465">Thanks for reaching out!</h2>
                  <p>Hi {name},</p>
                  <p>We\'ve received your message and will get back to you within 24 hours.</p>
                  <p style="margin-top:24px">Best,<br><strong>The ClientSniper Team</strong></p>
                </div>
            ''',
        )

        return jsonify({'success': True, 'message': 'Message sent successfully.'})
    except Exception as e:
        print(f'Contact form error: {e}')
        return jsonify({'error': 'Could not send message. Please try again later.'}), 500


# ─── Newsletter ────────────────────────────────────────
@app.route('/api/newsletter', methods=['POST'])
@rate_limit(max_requests=10, window_seconds=3600)
def newsletter():
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip()

    if not email or not EMAIL_RE.match(email):
        return jsonify({'error': 'Valid email is required.'}), 400

    # Log for now; in production integrate with email marketing service
    print(f'📬 Newsletter signup: {email}')
    return jsonify({'success': True, 'message': 'Subscribed!'})


# ─── Static File Catch-All (must be AFTER all API routes) ──
@app.route('/<path:filename>')
def static_files(filename):
    allowed_ext = {'.html', '.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp', '.woff', '.woff2', '.ttf', '.mp4', '.webm'}
    ext = os.path.splitext(filename)[1].lower()
    if ext in allowed_ext or '.' not in filename:
        filepath = os.path.join('.', filename)
        if os.path.isfile(filepath):
            return send_from_directory('.', filename)
    abort(404)


# ─── Error Handlers ────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return send_from_directory('.', 'index.html'), 404


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({'error': 'Too many requests. Please try again later.'}), 429


# ─── Start ─────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.getenv('PORT', 3000))
    print(f'\n🎯 ClientSniper server running on http://localhost:{port}\n')
    app.run(host='0.0.0.0', port=port, debug=True)
