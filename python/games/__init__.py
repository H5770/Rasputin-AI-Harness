"""games/ - registry for the Games tab. Each game is a module exposing:
ID, NAME, status() -> dict, power(on: bool), log(src, since) -> (lines, next), clear(src), command(text), shutdown().
To add a game: create games/<name>.py with that interface, add it to GAMES below, and register a panel in static/js/<name>.js.
"""
from functools import wraps

from flask import Response, jsonify, request

from . import mindcraft, flybridge

GAMES = {g.ID: g for g in (mindcraft, flybridge)}


def register(app, guard=None):
    """guard = accounts._staff_only (Admins and Owner only, same as the Discord tab)."""
    if guard is None:
        guard = lambda fn: wraps(fn)(lambda *a, **k: fn(None, *a, **k))

    def game_or_404(game):
        g = GAMES.get(game)
        if not g:
            return None, (jsonify({"error": "Unknown game."}), 404)
        return g, None

    @app.route("/games/api/list")
    @guard
    def games_list(me):
        return jsonify({"games": [{"id": g.ID, "name": g.NAME} for g in GAMES.values()]})

    @app.route("/games/api/<game>/status")
    @guard
    def games_status(me, game):
        g, err = game_or_404(game)
        return err or jsonify(g.status())

    @app.route("/games/api/<game>/power", methods=["POST"])
    @guard
    def games_power(me, game):
        g, err = game_or_404(game)
        if err:
            return err
        g.power(bool((request.get_json(silent=True) or {}).get("on")))
        return jsonify(g.status())

    @app.route("/games/api/<game>/log")
    @guard
    def games_log(me, game):
        g, err = game_or_404(game)
        if err:
            return err
        try:
            since = int(request.args.get("since", 0))
        except ValueError:
            since = 0
        lines, nxt = g.log(request.args.get("src", ""), since)
        return jsonify({"lines": lines, "next": nxt})

    @app.route("/games/api/<game>/clear", methods=["POST"])
    @guard
    def games_clear(me, game):
        g, err = game_or_404(game)
        if err:
            return err
        g.clear((request.get_json(silent=True) or {}).get("src", ""))
        return jsonify({"ok": True})

    @app.route("/games/api/<game>/frame")
    @guard
    def games_frame(me, game):
        g, err = game_or_404(game)
        if err:
            return err
        data = g.frame() if hasattr(g, "frame") else None
        if not data:
            return Response(status=204)
        return Response(data, mimetype="image/png", headers={"Cache-Control": "no-store"})

    @app.route("/games/api/<game>/command", methods=["POST"])
    @guard
    def games_command(me, game):
        g, err = game_or_404(game)
        if err:
            return err
        ok = g.command(str((request.get_json(silent=True) or {}).get("text", ""))[:300])
        return jsonify({"ok": ok}) if ok else (jsonify({"error": "Not running (FlyBridge only understands: restart)."}), 409)


def shutdown():
    for g in GAMES.values():
        try:
            g.shutdown()
        except Exception as exc:
            print("[GAMES] shutdown error:", exc)
