"""Public article pages and the password-protected review queue (/newsroom) for newsroom.py's drafts.

The review queue uses HTTP Basic auth (user "editor", password from NEWSROOM_PASSWORD) and is disabled (404)
when the variable is unset. Review POSTs carry a CSRF token derived from the password.
"""
import hashlib
import hmac
import os
import re

from flask import Blueprint, Response, abort, redirect, render_template, request, url_for
from markupsafe import Markup, escape

import web_data

bp = Blueprint("newsroom_web", __name__)
PASSWORD = os.getenv("NEWSROOM_PASSWORD", "")
LEAGUES = {"nfl": "NFL", "cfb": "College Football"}


def render_body(text):
    """Article body (plain paragraphs, optional **bold**) as safe HTML. Model output is never trusted as markup."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    html = []
    for p in paras:
        safe = str(escape(p)).replace("\n", " ")
        safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
        html.append(f"<p>{safe}</p>")
    return Markup("\n".join(html))


bp.add_app_template_filter(render_body, "article_body")


@bp.route("/<league>/news")
def articles_page(league):
    if league not in LEAGUES:
        abort(404)
    kind = request.args.get("kind") if request.args.get("kind") in web_data.KIND_LABELS else None
    page = max(1, request.args.get("page", 1, type=int))
    rows = web_data.latest_articles(league, 31, kind, (page - 1) * 30)
    return render_template("articles.html", league=league, league_name=LEAGUES[league], articles=rows[:30],
                           more=len(rows) > 30, page=page, kind=kind, kinds=web_data.KIND_LABELS)


@bp.route("/<league>/news/<slug>")
def article_page(league, slug):
    a = web_data.article(slug=slug)
    if not a or a["league"] != league:
        abort(404)
    return render_article(a)


def render_article(a, review=False):
    from app import GAME_SQL, attach_predictions, charts, query  # noqa: PLC0415 (app imports this module)
    g, wp = None, None
    if a["game_id"]:
        rows = query(GAME_SQL + " WHERE g.league = %s AND g.game_id = %s", (a["league"], a["game_id"]))
        g = attach_predictions(a["league"], rows)[0] if rows else None
        if g and a["kind"] == "recap":
            plays = query("SELECT period, clock, text, play_type, home_score, away_score, scoring, home_win_prob "
                          "FROM live_plays WHERE league = %s AND game_id = %s ORDER BY sequence, play_id",
                          (a["league"], a["game_id"]))
            wp = charts.win_probability(plays, g["home_abbr"], g["away_abbr"])
    related = [r for r in web_data.latest_articles(a["league"], 5) if r["id"] != a["id"]][:4]
    return render_template("article.html", a=a, g=g, wp=wp, league=a["league"], league_name=LEAGUES[a["league"]],
                           related=related, review=review)


# --- review queue ------------------------------------------------------------------------------------------------

def csrf_token():
    return hmac.new(PASSWORD.encode(), b"newsroom-csrf", hashlib.sha256).hexdigest()


@bp.before_request
def guard():
    if not request.path.startswith("/newsroom"):
        return None
    if not PASSWORD:
        abort(404)
    auth = request.authorization
    if not auth or auth.username != "editor" or not hmac.compare_digest((auth.password or "").encode(), PASSWORD.encode()):
        return Response("Sign in required", 401, {"WWW-Authenticate": 'Basic realm="SidelineWire newsroom"'})
    if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", ""), csrf_token()):
        abort(400)
    return None


@bp.after_request
def no_store(response):
    if request.path.startswith("/newsroom"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex"
    return response


@bp.route("/newsroom")
def queue():
    return render_template("newsroom.html", queue=web_data.review_queue(), leagues=LEAGUES)


@bp.route("/newsroom/<int:article_id>", methods=["GET", "POST"])
def review(article_id):
    a = web_data.article(article_id=article_id, published_only=False)
    if not a:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        status = {"publish": "published", "reject": "rejected", "unpublish": "review"}.get(action)
        edits = {k: request.form.get(k) for k in ("headline", "dek", "body")} if action in ("save", "publish") else {}
        edits = {k: v.replace("\r\n", "\n").strip() for k, v in edits.items() if v is not None}
        web_data.update_article(article_id, status=status, **edits)
        return redirect(url_for("newsroom_web.queue") if status else url_for("newsroom_web.review", article_id=article_id))
    return render_template("newsroom_article.html", a=a, csrf=csrf_token(), leagues=LEAGUES,
                           facts_text=facts_text(a["facts"]))


def facts_text(facts):
    out = []
    for key, value in (facts or {}).items():
        if key.startswith("_"):
            continue
        out.append(f"{key.upper()}:")
        out += [f"- {v}" for v in value] if isinstance(value, list) else [f"- {value}"]
    return "\n".join(out)
