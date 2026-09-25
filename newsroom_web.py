"""Public article pages, and the hidden admin section (/admin) for reviewing, editing, regenerating and removing
newsroom.py's articles. Admin is off (404) when NEWSROOM_PASSWORD is unset.
"""
import hashlib
import hmac
import os
import re
import time
from datetime import timedelta

from flask import Blueprint, abort, redirect, render_template, request, session, url_for
from markupsafe import Markup, escape

import web_data

bp = Blueprint("newsroom_web", __name__)
PASSWORD = os.getenv("NEWSROOM_PASSWORD", "")
LEAGUES = {"nfl": "NFL", "cfb": "College Football"}


def render_body(text, league=None, tags=None):
    """Article body (plain paragraphs, optional **bold**) as safe HTML. Model output is never trusted as markup.
    With tags, the first mention of each tagged team links to its team page."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    linkable = {t["team_id"] for t in (tags or [])}
    linked, html = set(), []
    for p in paras:
        pieces, pos = [], 0
        if league and linkable:
            for m in web_data._alias_pattern(web_data.team_aliases(league)).finditer(p):
                team = web_data.team_aliases(league).get(m.group(1))
                if team in linkable and team not in linked:
                    linked.add(team)
                    pieces.append(str(escape(p[pos:m.start(1)])))
                    pieces.append(f'<a class="team-mention" href="{url_for("team_page", league=league, team_id=team)}">'
                                  f"{escape(m.group(1))}</a>")
                    pos = m.end(1)
        pieces.append(str(escape(p[pos:])))
        safe = "".join(pieces).replace("\n", " ")
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
                           more=len(rows) > 30, page=page, kind=kind, kinds=web_data.KIND_PLURALS)


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
    tags = web_data.article_team_tags([a["id"]]).get(a["id"], [])
    related = [r for r in web_data.latest_articles(a["league"], 5) if r["id"] != a["id"]][:4]
    return render_template("article.html", a=a, g=g, wp=wp, league=a["league"], league_name=LEAGUES[a["league"]],
                           related=related, review=review, tags=tags)


# --- admin (hidden; not linked anywhere) --------------------------------------------------------------------------
#
# /admin: sign in with the newsroom password (Secret "newsroom", env NEWSROOM_PASSWORD; off when unset). A signed
# session cookie keeps you signed in for 12 hours; every POST carries a CSRF token. Actions per article: approve
# (publish), deny (reject), take down (back to review), edit, regenerate (the newsroom worker rewrites it, then it
# returns here for approve/deny) and delete.

SESSION_HOURS = 12
_attempts = {}  # ip -> recent failed sign-in times (per web process)


@bp.record_once
def _configure(state):
    app = state.app
    if PASSWORD and not app.secret_key:
        # stable across pods and restarts, and rotates with the password
        app.secret_key = hmac.new(PASSWORD.encode(), b"sidelinewire-admin-session", hashlib.sha256).hexdigest()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.getenv("SITE_ENV") in ("prod", "dev"),
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS))


def csrf_token():
    return hmac.new(PASSWORD.encode(), f"admin-csrf:{session.get('since', '')}".encode(), hashlib.sha256).hexdigest()


def signed_in():
    since = session.get("since")
    return bool(since and time.time() - since < SESSION_HOURS * 3600)


@bp.before_app_request
def guard():
    path = request.path
    if path.startswith("/newsroom"):  # old address
        return redirect("/admin" + path[len("/newsroom"):], 301) if PASSWORD else abort(404)
    if not path.startswith("/admin"):
        return None
    if not PASSWORD:
        abort(404)
    if path == "/admin/login":
        return None
    if not signed_in():
        return redirect(url_for("newsroom_web.login", next=request.full_path if request.method == "GET" else None))
    if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", ""), csrf_token()):
        abort(400)
    return None


@bp.after_app_request
def no_store(response):
    if request.path.startswith(("/admin", "/newsroom")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@bp.route("/admin/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        recent = [t for t in _attempts.get(ip, []) if time.time() - t < 300]
        if len(recent) >= 5:
            error = "Too many attempts. Try again in a few minutes."
        elif hmac.compare_digest(request.form.get("password", "").encode(), PASSWORD.encode()):
            _attempts.pop(ip, None)
            session.clear()
            session.permanent = True
            session["since"] = time.time()
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/admin") else url_for("newsroom_web.queue"))
        else:
            _attempts[ip] = recent + [time.time()]
            error = "Wrong password."
    return render_template("admin_login.html", error=error), (429 if error and "many" in error else 200)


@bp.route("/admin/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("newsroom_web.login"))


@bp.route("/admin")
def queue():
    return render_template("newsroom.html", queue=web_data.review_queue(), leagues=LEAGUES, csrf=csrf_token())


ACTIONS = {"publish": "published", "reject": "rejected", "takedown": "review"}


@bp.route("/admin/<int:article_id>", methods=["GET", "POST"])
def review(article_id):
    a = web_data.article(article_id=article_id, published_only=False)
    if not a:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        back = request.form.get("back") == "queue"
        if action == "delete":
            web_data.delete_article(article_id)
            web_data.clear_cache()
            return redirect(url_for("newsroom_web.queue"))
        if action == "regenerate":
            web_data.request_regeneration(article_id, (request.form.get("note") or "").strip()[:500])
        else:
            status = ACTIONS.get(action)
            edits = {k: request.form.get(k) for k in ("headline", "dek", "body")} if action in ("save", "publish") else {}
            edits = {k: v.replace("\r\n", "\n").strip() for k, v in edits.items() if v is not None}
            web_data.update_article(article_id, status=status, **edits)
        web_data.clear_cache()
        return redirect(url_for("newsroom_web.queue") if back or action in ("publish", "reject")
                        else url_for("newsroom_web.review", article_id=article_id))
    return render_template("newsroom_article.html", a=a, csrf=csrf_token(), leagues=LEAGUES,
                           facts_text=facts_text(a["facts"]))


@bp.route("/admin/<int:article_id>/preview")
def review_preview(article_id):
    a = web_data.article(article_id=article_id, published_only=False)
    if not a:
        abort(404)
    return render_article(a, review=True)


def facts_text(facts):
    out = []
    for key, value in (facts or {}).items():
        if key.startswith("_"):
            continue
        out.append(f"{key.upper()}:")
        out += [f"- {v}" for v in value] if isinstance(value, list) else [f"- {value}"]
    return "\n".join(out)
