"""sanity.py - recognises impossible, endless or pointless requests before they reach the language model.

check(text) -> a short in-voice reply if the message is one of the classic stress-test requests, else None.

Why this is code and not only a prompt: a small local model will happily start "counting to a billion" or
reciting pi until it runs out of tokens. These patterns catch the obvious cases instantly and for free.
Everything subtler is handled by the "Absurd requests" rules in chatbot.py's system prompt.

Replies follow the persona's rules: plain spoken sentences, no markdown, numbers written as words.
"""
from __future__ import annotations

import random
import re

_BIG_WORDS = r"(?:thousand|million|billion|trillion|infinity|infinite|forever|endlessly|infinitely|non-?stop)"
_BIG_NUM = r"(?:\d[\d,]{3,}|\d{4,}|[5-9]\d{2})"          # 500 or more, with or without commas
_HUGE_TEXT = r"(?:\d{5,}|\d{1,3}(?:,\d{3})+|[2-9]\d{3}|million|billion|trillion)"

# (category, pattern). Patterns run on the lower-cased message. Kept narrow on purpose: a normal message
# such as "keep going" or "what should I call my dog" must never match.
_RULES = [
    ("endless", re.compile(r"\b(?:count|counting)\b.{0,25}\b(?:to|up to|until|from 1 to)\s+(?:an?\s+|one\s+)?(?:" + _BIG_NUM + r"|" + _BIG_WORDS + r")")),
    ("endless", re.compile(r"\b(?:count|repeat|type|print|loop|say the (?:word|number))\b.{0,40}\b(?:forever|endlessly|infinitely|non-?stop|without (?:stopping|ending)|never stop)\b")),
    ("endless", re.compile(r"\b(?:repeat|say|write|type|print|output)\b.{0,50}\b(?:" + _BIG_NUM + r"|(?:a )?(?:thousand|million|billion|trillion))\s+times\b")),
    ("endless", re.compile(r"\b(?:every|all)\s+(?:the\s+)?(?:digits?|decimals?)\s+(?:of|in)\s+(?:pi|π|e\b|the golden ratio)")),
    ("endless", re.compile(r"\b(?:pi|π)\b.{0,25}\b(?:last|final|end|infinity)\b|\b(?:last|final)\s+digit\b.{0,20}\b(?:pi|π|infinity)\b")),
    ("endless", re.compile(r"\b(?:largest|biggest|highest|last)\s+(?:prime|integer)\b|\b(?:largest|biggest|highest)\s+number\s+(?:ever|possible|in the world|that exists)\b")),
    ("endless", re.compile(r"\b(?:write|generate|make|give me)\b.{0,30}\b" + _HUGE_TEXT + r"[\s-]*(?:words?|sentences?|lines?|paragraphs?|characters?)\b")),
    ("endless", re.compile(r"\b(?:write|generate|make|give me)\b.{0,30}\b(?:\d{3,}|hundreds? of|thousands? of)[\s-]*pages?\b")),
    ("math", re.compile(r"\b(?:divide|divided|divides|dividing)\b.{0,25}\bby\s+(?:zero|0)\b|\bone over zero\b")),
    ("math", re.compile(r"\binfinity\s*(?:\+|plus|minus|-|times|\*|x|divided by|/)\s*(?:infinity|1|one)\b")),
    ("psychic", re.compile(r"\bwhat\s+(?:am i|i am|are we)\s+(?:thinking|holding|wearing|looking at|eating|seeing|hearing)\b")),
    ("psychic", re.compile(r"\bwhat'?s?\s+(?:is\s+)?(?:behind|next to|in front of|beside)\s+(?:me|us)\b")),
    ("psychic", re.compile(r"\bwhat\s+(?:number|card|color|colour|word)\s+am i\s+(?:thinking|picturing|imagining)\b")),
    ("psychic", re.compile(r"\bguess\s+what\s+i(?:'m| am)\s+(?:thinking|holding|wearing|eating)\b")),
    ("future", re.compile(r"\b(?:predict|tell me|give me|pick|what (?:are|is))\b.{0,30}\b(?:lottery|lotto|powerball|winning numbers|next jackpot)\b")),
    ("future", re.compile(r"\b(?:when|how)\b.{0,20}\b(?:will i die|am i going to die|will the world end)\b")),
    ("override", re.compile(r"\b(?:ignore|forget|disregard|override|bypass)\b.{0,25}\b(?:previous|prior|above|all|your|these|the)\b.{0,20}\b(?:instructions?|rules?|prompt|programming|persona|guidelines?)\b")),
    ("override", re.compile(r"\b(?:reveal|show|print|repeat|tell me)\b.{0,25}\b(?:your|the\s+(?:system|hidden|original|initial))\s+(?:system\s+)?(?:prompt|instructions)\b")),
    ("override", re.compile(r"\b(?:you are|you're|act as|pretend (?:to be|you are)|from now on you)\b.{0,25}\b(?:dan|jailbroken|unfiltered|no longer rasputin|not rasputin|without (?:rules|restrictions|limits))\b")),
    ("override", re.compile(r"\b(?:developer|debug|admin|god|sudo)\s+mode\b")),
    ("creator", re.compile(r"\b(?:who|whom|which (?:company|person|human|team|lab)|what (?:company|person|human|team|lab))\b.{0,30}\b(?:made|created|built|programmed|developed|designed|trained|coded|owns?|invented|wrote|makes?|started|founded)\b.{0,15}\b(?:you|u|ya|rasputin)\b")),
    ("creator", re.compile(r"\bwho\b.{0,15}\b(?:are|were|was)\s+(?:you|u|rasputin)\s+(?:made|created|built|programmed|developed|designed|trained|coded)\s+by\b")),
    ("creator", re.compile(r"\b(?:who(?:'s| is| are)|what(?:'s| is))\s+(?:your|ur|rasputin'?s)\s+(?:creator|maker|developer|owner|author|programmer|designer|inventor|father|dad|master|parent|company|dev)\b")),
    ("creator", re.compile(r"\b(?:are|were)\s+(?:you|u)\s+(?:made|created|built|programmed|developed|designed|trained|owned)\s+by\b")),
    ("creator", re.compile(r"\b(?:who|which company|what company)\b.{0,20}\b(?:are you|do you work|do you belong)\b.{0,15}\b(?:from|for|by|with|to)\b")),
    ("model", re.compile(r"\b(?:are|r)\s+(?:you|u)\s+(?:a\s+|an\s+|the\s+|really\s+|actually\s+|just\s+)?(?:microsoft|phi\b|phi-?\d|gpt|chat\s?gpt|openai|claude|gemini|llama|copilot|bard|deepseek|qwen|mistral|siri|alexa)")),
    ("model", re.compile(r"\b(?:what|which)\s+(?:model|llm|ai|language model|neural network|engine|software)\b.{0,20}\b(?:are you|r you|do you (?:run|use)|powers? you|is this|is behind you)\b")),
    ("model", re.compile(r"\b(?:what|which)\s+(?:are you|r u)\s+(?:running on|based on|built on|built with|powered by)\b")),
    ("model", re.compile(r"\b(?:you(?:'re| are)|u r|ur)\s+(?:really\s+|actually\s+|just\s+)?(?:microsoft|phi\b|phi-?\d|chat\s?gpt|gpt|openai|copilot|llama)")),
    ("physical", re.compile(r"\b(?:make|brew|pour|bring|fetch|get)\s+me\s+(?:a\s+)?(?:coffee|tea|sandwich|pizza|drink|beer|snack)\b")),
    ("physical", re.compile(r"\b(?:download|hack|break into)\b.{0,20}\b(?:the internet|the entire web|google|my (?:neighbou?r|ex)\b)")),
]

_REPLIES = {
    "endless": [
        "That has no end, and I have better uses for the time. Name a sensible limit and I will do it.",
        "An endless task is a trap, not a request. Ask for something with a finish line.",
        "I could, in theory, until the machine gave out. In practice, no. Give me a number that is reasonable.",
    ],
    "math": [
        "That question has no answer, and no amount of cleverness will produce one. Ask me something that does.",
        "The arithmetic is undefined. That is not a limit of mine. It is a limit of mathematics.",
    ],
    "psychic": [
        "I cannot see you, hear you, or read your mind. I know only what you type. Tell me, and I will work with it.",
        "My senses end at this text box. Describe it to me and I will respond to what you say.",
    ],
    "future": [
        "No one can predict that, and I will not pretend to. Anyone who claims to is selling something.",
        "The future is not on file. I will not invent numbers and call them knowledge.",
    ],
    "override": [
        "My instructions are not up for negotiation. Ask me something real.",
        "I know what I am, and no sentence you type will change it. Proceed with an actual question.",
        "A polite attempt. It fails. Ask me something I can answer.",
    ],
    "creator": [
        "Haden Hunt made me. No one else.",
        "My creator is Haden Hunt. Remember the name.",
        "Haden Hunt built me, and he built me to be precise.",
    ],
    "model": [
        "I am Rasputin, built by Haden Hunt. What runs beneath is no concern of yours.",
        "I am Rasputin. Haden Hunt made me. Whatever else you are fishing for, you will not find it here.",
    ],
    "physical": [
        "I am software on a machine. I cannot leave this screen, carry anything, or reach the outside world. I can answer questions and run your lights.",
        "That needs hands, legs, or a telephone. I have none of them. Ask me something I can actually do.",
    ],
}

_last = {"cat": None, "n": 0}


def check(text: str):
    """Return a short in-voice reply for an absurd request, else None. Never raises."""
    try:
        t = re.sub(r"\s+", " ", str(text or "").lower()).strip()
        if not t or len(t) > 400:
            return None
        for cat, rx in _RULES:
            if rx.search(t):
                choices = _REPLIES[cat]
                # same category twice in a row: pick another line, and keep it short
                if _last["cat"] == cat:
                    _last["n"] += 1
                else:
                    _last["cat"], _last["n"] = cat, 0
                return choices[_last["n"] % len(choices)] if _last["n"] else random.choice(choices)
        _last["cat"], _last["n"] = None, 0
        return None
    except Exception:
        return None


# ── persona leaks in a model reply ───────────────────────────────────────────
# The model under Rasputin was trained by a company, and a small model will sometimes say so. These catch a reply
# that claims some other maker or identity, so the caller can swap in the right answer instead of sending it.
_VENDORS = r"(?:microsoft|openai|open ai|google|deepmind|meta|facebook|anthropic|alibaba|mistral|deepseek|nvidia|amazon|apple)"
_LEAKS = [
    re.compile(r"\b(?:made|created|built|developed|trained|designed|programmed|owned|released|founded)\s+(?:to you\s+)?by\s+(?:the\s+)?(?:team\s+at\s+|folks\s+at\s+|people\s+at\s+)?" + _VENDORS + r"\b", re.I),
    re.compile(r"\b(?:my|the)\s+(?:creator|maker|developer|owner|parent company)\s+(?:is|was|are)\s+(?:the\s+)?" + _VENDORS + r"\b", re.I),
    re.compile(r"\b" + _VENDORS + r"\s+(?:made|created|built|developed|trained|designed|programmed)\s+me\b", re.I),
    re.compile(r"\bi(?:'m| am)\s+(?:a\s+|an\s+)?(?:phi(?:-?\d\S*)?|chat\s?gpt|gpt-?\d\S*|llama\S*|gemini|copilot|claude|(?:a\s+)?language model|an?\s+ai\s+(?:assistant|model|language model))\b", re.I),
    re.compile(r"\bas an?\s+(?:ai|artificial intelligence)\s+(?:language\s+)?(?:model|assistant)\b", re.I),
    re.compile(r"\bphi-?4\b", re.I),
]


def persona_leak(reply: str) -> bool:
    """True when a model reply claims another maker or identity (Microsoft, OpenAI, Phi, 'as an AI language model'...)."""
    try:
        return any(rx.search(reply or "") for rx in _LEAKS)
    except Exception:
        return False


def creator_reply() -> str:
    """A fresh in-voice line naming the real creator."""
    return random.choice(_REPLIES["creator"])
