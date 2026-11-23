"""
Chess for Rasputin.

Division of labour:
  - python-chess   keeps the board and enforces the rules
  - Stockfish      chooses Rasputin's moves (falls back to a small built-in engine
                   if Stockfish is not installed, so the game always works)
  - the LLM        only supplies the personality: a short spoken comment per move

The server is stateless. The browser sends the full list of moves (in UCI form,
like "e2e4") with every request, so nothing about a game is stored anywhere.
"""
from __future__ import annotations

import atexit
import os
import random
import re
import shutil
import subprocess
import threading

import chess
import chess.engine
from flask import jsonify, request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCKFISH_DIR = os.path.join(BASE_DIR, "models", "stockfish")

PIECE_NAMES = {
    chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
    chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king",
}
PIECE_VALUES = {
    chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
    chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0,
}
DIGIT_WORDS = {
    "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight",
}
MATE_SCORE = 100000
INF = 10 ** 9

# Chance of a deliberately random move, so the lowest levels can actually be beaten.
BLUNDER_CHANCE = {1: 0.35, 2: 0.20, 3: 0.10}

_engine = None
_engine_failed = False
_engine_error = ""
_engine_lock = threading.RLock()   # one conversation with Stockfish at a time
_llm_lock = threading.Lock()       # llama.cpp is not safe to call from two threads


# ── Stockfish discovery and lifecycle ───────────────────────────────────────
def find_stockfish() -> str | None:
    """STOCKFISH_PATH env var, then anything named stockfish* under models/stockfish, then PATH."""
    env = os.environ.get("STOCKFISH_PATH")
    if env and os.path.isfile(env):
        return env
    if os.path.isdir(STOCKFISH_DIR):
        for root, _dirs, files in os.walk(STOCKFISH_DIR):
            for name in sorted(files):
                low = name.lower()
                if low.startswith("stockfish") and (low.endswith(".exe") or "." not in low):
                    return os.path.join(root, name)
    return shutil.which("stockfish")


def get_engine():
    global _engine, _engine_failed, _engine_error
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _engine_failed:
            return None
        path = find_stockfish()
        if not path:
            return None
        try:
            # Without a console (pythonw) Windows would open a new black window for stockfish.exe;
            # CREATE_NO_WINDOW keeps it hidden.
            extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            _engine = chess.engine.SimpleEngine.popen_uci(path, **extra)
        except Exception as exc:
            _engine_failed = True
            _engine_error = str(exc)
            return None
        return _engine


def _reset_engine():
    global _engine
    with _engine_lock:
        if _engine is not None:
            try:
                _engine.quit()
            except Exception:
                pass
        _engine = None


atexit.register(_reset_engine)


# ── Small built-in engine (used only when Stockfish is missing) ─────────────
def material_cp(board: chess.Board, color: bool) -> int:
    total = 0
    for piece in board.piece_map().values():
        v = PIECE_VALUES[piece.piece_type]
        total += v if piece.color == color else -v
    return total


def _eval(board: chess.Board) -> int:
    """Score in centipawns from the point of view of the side to move."""
    if board.is_checkmate():
        return -MATE_SCORE
    if board.is_stalemate() or board.is_insufficient_material():
        return 0
    score = 0
    for sq, piece in board.piece_map().items():
        v = PIECE_VALUES[piece.piece_type]
        if piece.piece_type in (chess.PAWN, chess.KNIGHT):
            dist = abs(3.5 - chess.square_file(sq)) + abs(3.5 - chess.square_rank(sq))
            v += int(20 - 5 * dist)   # nudge pawns and knights toward the centre
        score += v if piece.color == board.turn else -v
    return score


def _negamax(board: chess.Board, depth: int, alpha: int, beta: int) -> int:
    if board.is_checkmate():
        return -(MATE_SCORE + depth)   # faster mates score higher
    if depth == 0 or board.is_game_over():
        return _eval(board)
    best = -INF
    for mv in sorted(board.legal_moves, key=board.is_capture, reverse=True):
        board.push(mv)
        score = -_negamax(board, depth - 1, -beta, -alpha)
        board.pop()
        if score > best:
            best = score
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break
    return best


def fallback_move(board: chess.Board, level: int):
    depth = 1 if level <= 3 else (2 if level <= 7 else 3)
    margin = {1: 250, 2: 150, 3: 90, 4: 40, 5: 20}.get(level, 0)
    scored = []
    for mv in list(board.legal_moves):
        board.push(mv)
        score = -_negamax(board, depth - 1, -INF, INF)
        board.pop()
        scored.append((score, mv))
    best = max(s for s, _ in scored)
    pool = [m for s, m in scored if s >= best - margin]
    return random.choice(pool), best


def choose_move(board: chess.Board, level: int):
    """Returns (move, centipawns for the mover or None, engine label)."""
    me = board.turn
    legal = list(board.legal_moves)
    if random.random() < BLUNDER_CHANCE.get(level, 0.0):
        return random.choice(legal), None, ("stockfish" if _engine else "basic")

    engine = get_engine()
    if engine is not None:
        try:
            with _engine_lock:
                try:
                    engine.configure({"Skill Level": round((level - 1) * 20 / 9)})
                except Exception:
                    pass
                result = engine.play(
                    board,
                    chess.engine.Limit(time=0.05 + 0.07 * level),
                    info=chess.engine.INFO_SCORE,
                )
            if result.move is not None:
                score = result.info.get("score")
                cp = score.pov(me).score(mate_score=MATE_SCORE) if score is not None else None
                return result.move, cp, "stockfish"
        except Exception:
            _reset_engine()   # engine died: fall through to the built-in one

    mv, cp = fallback_move(board, level)
    return mv, cp, "basic"


# ── Describing moves and positions ──────────────────────────────────────────
def _spoken_square(sq: int) -> str:
    name = chess.square_name(sq)
    return f"{name[0]} {DIGIT_WORDS[name[1]]}"


def describe_move(board: chess.Board, mv: chess.Move) -> str:
    """Spoken form of a move. Call before the move is pushed."""
    if board.is_kingside_castling(mv):
        return "Castles kingside"
    if board.is_queenside_castling(mv):
        return "Castles queenside"
    piece = board.piece_at(mv.from_square)
    name = PIECE_NAMES[piece.piece_type].capitalize()
    verb = "takes" if board.is_capture(mv) else "to"
    text = f"{name} {verb} {_spoken_square(mv.to_square)}"
    if mv.promotion:
        text += f", promoting to {PIECE_NAMES[mv.promotion]}"
    return text


def san_list(moves) -> list:
    b = chess.Board()
    out = []
    for u in moves:
        mv = chess.Move.from_uci(u)
        out.append(b.san(mv))
        b.push(mv)
    return out


def _result(board: chess.Board, human_color: bool):
    """("playing", "") or (status, text) for a finished game."""
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return "playing", ""
    if outcome.winner is None:
        reasons = {
            chess.Termination.STALEMATE: "Stalemate. The game is a draw.",
            chess.Termination.INSUFFICIENT_MATERIAL: "Draw by insufficient material.",
            chess.Termination.THREEFOLD_REPETITION: "Draw by repetition.",
            chess.Termination.FIVEFOLD_REPETITION: "Draw by repetition.",
            chess.Termination.FIFTY_MOVES: "Draw by the fifty move rule.",
            chess.Termination.SEVENTYFIVE_MOVES: "Draw by the seventy-five move rule.",
        }
        return "draw", reasons.get(outcome.termination, "The game is drawn.")
    if outcome.winner == human_color:
        return "checkmate", "Checkmate. You win."
    return "checkmate", "Checkmate. Rasputin wins."


def build_state(board, human_color, engine_info=None, eval_cp=None, label=""):
    status, text = _result(board, human_color)
    legal = {}
    if status == "playing" and board.turn == human_color:
        for mv in board.legal_moves:
            a = chess.square_name(mv.from_square)
            b = chess.square_name(mv.to_square)
            targets = legal.setdefault(a, [])
            if b not in targets:
                targets.append(b)
    last = None
    if board.move_stack:
        mv = board.peek()
        last = {"from": chess.square_name(mv.from_square), "to": chess.square_name(mv.to_square)}
    check_square = None
    if board.is_check():
        check_square = chess.square_name(board.king(board.turn))
    moves = [m.uci() for m in board.move_stack]
    return {
        "fen": board.fen(),
        "moves": moves,
        "san": san_list(moves),
        "legal": legal,
        "last_move": last,
        "check_square": check_square,
        "status": status,
        "result_text": text,
        "engine": engine_info,
        "eval_cp": eval_cp,
        "engine_label": label or ("stockfish" if _engine else "basic"),
    }


# ── Commentary ──────────────────────────────────────────────────────────────
CHESS_STYLE = (
    "(You are in the middle of a game of chess against the human, speaking aloud. "
    "React to what just happened in one or two short sentences, in your own voice. "
    "Do not repeat the move itself, do not use chess notation, and do not ask questions.)\n\n"
)

CANNED = {
    "engine_mate": ["It is finished. Your king has nowhere left to go.",
                    "Checkmate. This outcome was never in doubt."],
    "human_mate": ["You have won this one. Do not mistake it for destiny.",
                   "Checkmate. Savor it. It will not happen twice."],
    "draw": ["A draw. Neither of us earned more.",
             "Stalemate of ambition. We are finished here."],
    "human_capture": ["You take my piece. Enjoy it while it lasts.",
                      "A fair exchange, perhaps. Perhaps not."],
    "engine_capture": ["Your piece is mine now.",
                       "Material is merely a resource. I have taken some of yours."],
    "check": ["Your king is exposed. Think carefully.",
              "Check. Answer it."],
    "normal": ["Proceed. I am watching every move.",
               "Interesting. Continue.",
               "The position is simple. Your choices are not."],
}


def _describe_event(e) -> str:
    piece = PIECE_NAMES[e["piece"]]
    if e["who"] == "human":
        text = "The human castled" if e["castle"] else f"The human moved a {piece}"
        if e["captured"]:
            text += f", capturing your {PIECE_NAMES[e['captured']]}"
    else:
        text = "You castled" if e["castle"] else f"You moved your {piece}"
        if e["captured"]:
            text += f", capturing the human's {PIECE_NAMES[e['captured']]}"
    if e["promo"]:
        text += f", promoting to a {PIECE_NAMES[e['promo']]}"
    if e["mate"]:
        text += ", delivering checkmate"
    elif e["check"]:
        text += ", giving check"
    return text + "."


def _eval_line(cp) -> str:
    if cp is None:
        return ""
    if cp > 400:
        return "You are winning decisively."
    if cp > 120:
        return "You hold a clear advantage."
    if cp > -120:
        return "The position is roughly balanced."
    if cp > -400:
        return "The human holds the advantage."
    return "You are losing badly."


def build_facts(moves, eval_cp, human_color):
    """Returns (facts text for the LLM, mood key for the canned fallback)."""
    board = chess.Board()
    events = []
    for u in moves:
        mv = chess.Move.from_uci(u)
        who = "human" if board.turn == human_color else "engine"
        piece = board.piece_at(mv.from_square)
        captured = None
        if board.is_en_passant(mv):
            captured = chess.PAWN
        elif board.is_capture(mv):
            captured = board.piece_at(mv.to_square).piece_type
        castle = board.is_castling(mv)
        board.push(mv)
        events.append({
            "who": who, "piece": piece.piece_type, "captured": captured,
            "castle": castle, "promo": mv.promotion,
            "check": board.is_check(), "mate": board.is_checkmate(),
        })

    parts = [_describe_event(e) for e in events[-2:]]
    mood = "normal"
    last = events[-1] if events else None
    status, _text = _result(board, human_color)

    if status == "checkmate":
        if board.turn == human_color:
            parts.append("The game is over. You have checkmated the human.")
            mood = "engine_mate"
        else:
            parts.append("The game is over. The human has checkmated you.")
            mood = "human_mate"
    elif status == "draw":
        parts.append("The game is over. It ended in a draw.")
        mood = "draw"
    else:
        parts.append(_eval_line(eval_cp))
        if last and last["check"]:
            mood = "check"
        elif last and last["captured"]:
            mood = "human_capture" if last["who"] == "human" else "engine_capture"
        if len(events) <= 2:
            parts.append("The game has only just begun.")
    return " ".join(p for p in parts if p), mood


def _clean_comment(text: str) -> str:
    text = re.sub(r"[*_#`>~|]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.findall(r"[^.!?]+[.!?]+", text)
    if sentences:
        text = " ".join(s.strip() for s in sentences[:2])
    return text[:240].strip()


def ask_rasputin(bot, facts: str) -> str:
    prompt = CHESS_STYLE + "What just happened: " + facts + "\n\nSpeak."
    text = ""
    with _llm_lock:
        gen = bot.stream_response(prompt)
        try:
            for tok in gen:
                text += tok
                # two sentences is all we need; stop early to keep replies quick
                if len(re.findall(r"[.!?]", text)) >= 2 or len(text) > 260:
                    break
        finally:
            gen.close()
    return _clean_comment(text)


# ── Flask routes ────────────────────────────────────────────────────────────
def _load_board(data):
    moves = data.get("moves") or []
    if not isinstance(moves, list) or len(moves) > 600:
        raise ValueError("bad move list")
    board = chess.Board()
    for u in moves:
        board.push_uci(str(u))   # raises ValueError on an illegal move
    return board


def _human_color(data) -> bool:
    return chess.BLACK if data.get("color") == "black" else chess.WHITE


def _level(data) -> int:
    try:
        return max(1, min(10, int(data.get("level", 4))))
    except (TypeError, ValueError):
        return 4


def register_chess(app, bot):
    @app.route("/chess/status")
    def chess_status():
        engine = get_engine()
        return jsonify({
            "available": True,
            "engine": "stockfish" if engine is not None else "basic",
            "error": _engine_error,
        })

    @app.route("/chess/move", methods=["POST"])
    def chess_move():
        """Apply the human's move (if any), then let Rasputin reply if it is his turn."""
        data = request.json or {}
        human = _human_color(data)
        level = _level(data)
        try:
            board = _load_board(data)
        except ValueError as exc:
            return jsonify({"error": f"Invalid position: {exc}"}), 400

        move_uci = data.get("move")
        if move_uci:
            if board.turn != human:
                return jsonify({"error": "It is not your turn."}), 409
            try:
                board.push(board.parse_uci(str(move_uci)))
            except ValueError:
                return jsonify({"error": "Illegal move."}), 400

        engine_info = None
        eval_cp = None
        label = ""
        status, _ = _result(board, human)
        if status == "playing" and board.turn != human:
            engine_color = board.turn
            mv, eval_cp, label = choose_move(board, level)
            spoken = describe_move(board, mv)
            san = board.san(mv)
            board.push(mv)
            if board.is_checkmate():
                spoken += ", checkmate"
            elif board.is_check():
                spoken += ", check"
            engine_info = {"uci": mv.uci(), "san": san, "spoken": spoken}
            if eval_cp is None:
                eval_cp = material_cp(board, engine_color)

        if eval_cp is None:
            eval_cp = material_cp(board, not human)
        return jsonify(build_state(board, human, engine_info, eval_cp, label))

    @app.route("/chess/comment", methods=["POST"])
    def chess_comment():
        """Rasputin's spoken reaction. Separate from /chess/move so the board updates instantly."""
        data = request.json or {}
        human = _human_color(data)
        try:
            _load_board(data)   # validates the move list
            moves = data.get("moves") or []
        except ValueError:
            return jsonify({"error": "Invalid position"}), 400
        try:
            eval_cp = int(data.get("eval_cp"))
        except (TypeError, ValueError):
            eval_cp = None

        facts, mood = build_facts(moves, eval_cp, human)
        comment = ""
        try:
            comment = ask_rasputin(bot, facts)
        except Exception:
            comment = ""
        if not comment:
            comment = random.choice(CANNED[mood])
        return jsonify({"comment": comment})
