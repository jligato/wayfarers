"""
Wayfarer's Cycle - the NPC roster: who they are, what they want, how they talk.

House style, same as ``combat_config.py`` / ``tower_config.py``
- plain module constants, no classes with behaviour
- every non-obvious number carries a comment saying WHY it is that number
- nothing here reads a database or imports another service module

What this table is for
----------------------
The shipped roster (``shared/npcs/npcs.json``) was a name, a role string and a
greeting. It had no *want*: nothing an NPC asked for, so nothing a player could
refuse, so nothing at stake. This file is the missing half. Each entry answers
three questions in the order that matters:

1. What does this person WANT? Not "what can they give you" -- what are they
   trying to get done. An NPC with no want is a menu.
2. What do they BELIEVE about the player? Cultivators read each other. The
   ``reads`` line says how this NPC interprets a stranger's progress, and it is
   the part that makes a returning player feel the world noticed them.
3. How do they SOUND? Cultivation society forbids saying the thing directly
   (see the xianxia-subtext skill: surface politeness, subtext in metaphor and
   honorific, dao position in the shape of the argument). A sect elder, a
   merchant and a rival do not share a mouth.

The faction axis is the sect-politics axis, unchanged:
orthodox / reformist / isolationist / aggressive / trade. ``faction`` is what
makes two NPCs want OPPOSITE outcomes from the same event, which is the whole
engine of the favour system below.

Favours, and why none of them cost money
----------------------------------------
``FAVOURS`` entries are requests the player may accept, refuse, or fail. Their
cost is a RELATIONSHIP: accepting one usually costs you standing with somebody
else (see ``cost`` below), and failing costs standing with the asker. This is
deliberate, and it is the reason the design brief forbids buying a favour with
spirit stones: a price you can pay is not a sacrifice. Standing with the sect
elder is something the player wants, so spending it is a real cost.

No entry in this file pays currency. The reward for a favour is ``lead`` - a
piece of information about the world, which is the one currency an idle game can
hand out forever without breaking its economy.
"""
from __future__ import annotations

# === Relationship scale ===
#
#: Standing is a small signed integer, not a bar. It moves in whole steps
#: because a conversation is a scene, not a grind: a player who talks to an NPC
#: three times should not see a different person each time.
STANDING_MIN = -3
STANDING_MAX = 3

#: Standing at or above this reads as "this NPC will help you unasked". Chosen
#: as one third of the scale so it is reachable inside a single conversation
#: arc but not by talking to everyone twice.
STANDING_TRUSTED = 2

#: Standing at or below this reads as "this NPC will lie to you". Set symmetric
#: to STANDING_TRUSTED so trust and distrust are equally easy to reach.
STANDING_DISTRUSTED = -2


# === Favour timing ===
#
#: Hours a player has to resolve an accepted favour before it is failed against
#: them. Long enough to be met inside one idle day (a returning player), short
#: enough that a stale accepted favour is not a permanent state. There is no
#: timer UI elsewhere in the game, so the deadline is evaluated LAZILY when the
#: roster is read -- see npc_state.expire_favours.
FAVOUR_DEADLINE_HOURS = 24.0


# === The roster ===
#
#: id
#:   Matches ``shared/npcs/npcs.json`` exactly. The old JSON file stays the
#:   source of zone placement and realm gating; this file is the source of
#:   identity, want and voice. Splitting them that way means the shipped client
#:   contract (NPCSystem reads ``id``/``name``/``role``) does not move.
#: faction
#:   The sect-politics axis. See the module docstring.
#: wants
#:   What this NPC is trying to get done. One sentence, in their own priority.
#: reads
#:   How this NPC interprets a player's progress. This is what a returning
#:   player reacts to, and the reason an NPC is worth more than a reward.
#: voice
#:   register / cadence / forbidden -- the three levers that keep a sect elder,
#:   a merchant and a rival from sounding like the same writer.
#: greeting
#:   First line, before the player has said anything.
#: warm / cold
#:   Used at high and low standing. Both are written in the same voice as the
#:   neutral ``greeting``; a character that changes register when it likes you
#:   is a character with no register.
NPC_ROSTER = {
    "village_elder": {
        "name": "Elder Qiu",
        "faction": "orthodox",
        "title": "Elder of the Border Village",
        "wants": (
            "The sect's water levy stops rising this season. He cannot fight the "
            "tax and he will not leave, so he needs someone the levy officers "
            "cannot ignore."
        ),
        "reads": (
            "He reads a stranger's progress as pressure. A mortal who suddenly "
            "moves fast has sect attention on him, and sect attention is the one "
            "thing he has spent a lifetime avoiding."
        ),
        "voice": {
            "register": "plain, deferential, unhurried",
            "cadence": "short clauses; he repeats a noun instead of using a pronoun",
            "forbidden": "no cultivation jargon, no flattery, no exclamation",
        },
        "greeting": (
            "You walk like the mountain road, not like the village road. Sit. "
            "Drink first. Words after."
        ),
        "warm": (
            "You came back. Good. The village remembers who sat down the first "
            "time. So do I."
        ),
        "cold": (
            "I gave you the name of a man and you brought me nothing. The tea is "
            "still there. I have not moved."
        ),
    },
    "spirit_testing_examiner": {
        "name": "Examiner Yun",
        "faction": "orthodox",
        "title": "Testing Shard Examiner, Imperial Appointments Bureau",
        "wants": (
            "His posting's numbers stay clean. A failure he reported correctly "
            "would clear him; a failure he reported wrongly would end him."
        ),
        "reads": (
            "He reads every result as a risk to himself. A strong reading is not "
            "good news to him -- it is a stone in the imperial ledger with his "
            "name under it."
        ),
        "voice": {
            "register": "bureaucratic, third-person about himself",
            "cadence": "full sentences, always qualified; 'the Bureau notes'",
            "forbidden": "no warmth, no slang, never a first-person feeling",
        },
        "greeting": (
            "The Bureau notes your arrival. State your name and your village for "
            "the record. The record is the only thing here that lasts."
        ),
        "warm": (
            "The Bureau notes that you returned. It also notes that you were "
            "punctual. Both are recorded. Neither is a favour."
        ),
        "cold": (
            "The Bureau notes your return. It notes no accompanying record. Both "
            "notes are filed. You may go."
        ),
    },
    "wandering_merchant": {
        "name": "Merchant Jiu",
        "faction": "trade",
        "title": "Licensed Itinerant Trader",
        "wants": (
            "A route that does not pass a sect toll-house. He has the goods and "
            "the buyers; the toll is the only thing between them."
        ),
        "reads": (
            "He reads a player's progress as a new market. A cultivator who just "
            "broke through has money he has not yet learned to be careful with."
        ),
        "voice": {
            "register": "charming, transactional, never eager",
            "cadence": "questions, then silence; he lets the other person fill it",
            "forbidden": "never names a price first, never says 'friend', no loyalty claims",
        },
        "greeting": (
            "You are standing in the only shade on this road, and you have been "
            "in it a while. That is a decision. What is it worth to you?"
        ),
        "warm": (
            "Straight to it, then. You know the road and you know me. Here is a "
            "thing I do not say twice."
        ),
        "cold": (
            "No. You are welcome to finish your tea. I have a licence, and the "
            "day is long."
        ),
    },
    "feng_zhongling": {
        "name": "Uncle Feng",
        "faction": "orthodox",
        "title": "Head of Stone Cloud Monastery",
        "wants": (
            "Someone to carry the body path honestly. The sect teaches it as a "
            "secondary branch; he has watched it taught that way for forty years."
        ),
        "reads": (
            "He reads a player's body before their realm, and he is usually right. "
            "He has no interest in what a player can do, only in whether they will "
            "keep doing it after it stops working."
        ),
        "voice": {
            "register": "blunt, spare, contemptuous of cleverness",
            "cadence": "one sentence, then a silence he does not fill",
            "forbidden": "no qi theory, no politeness, never a second question",
        },
        "greeting": (
            "Sit down. Do not speak. I want to hear your breathing before I hear "
            "your name."
        ),
        "warm": (
            "Good. You came back and you did not ask how to make it faster. Ask "
            "that and I will send you home."
        ),
        "cold": (
            "You went. That is the answer. I am not disappointed, so do not stand "
            "there trying to fix it."
        ),
    },
    "light_sect_elder": {
        "name": "Elder Wen",
        "faction": "reformist",
        "title": "Elder of the Light Sect, Reform Wing",
        "wants": (
            "A witness -- someone outside the sect -- that the old ways are "
            "failing. He cannot say so from inside the hall."
        ),
        "reads": (
            "He reads a player's progress as a fact the council has not yet "
            "agreed on. A strong outsider is the only kind of evidence he can "
            "actually use."
        ),
        "voice": {
            "register": "careful, formal, everything is a figure of speech",
            "cadence": "one metaphor, then a very plain sentence",
            "forbidden": "never names the antagonist, never raises his voice",
        },
        "greeting": (
            "The hall is colder than it was, which tells you the season has not "
            "changed. Please. Speak plainly if you can manage it."
        ),
        "warm": (
            "You came back. I had decided not to ask. You made that easier than "
            "it should have been."
        ),
        "cold": (
            "The hall is the same temperature it was when you last sat there. I "
            "have not altered it for anyone."
        ),
    },
    "core_disciple_wei_guang": {
        "name": "Wei Guang",
        "faction": "aggressive",
        "title": "Inner Sect Disciple, Combat Line",
        "wants": (
            "To be right about the player in front of people who outrank him. He "
            "has bet his standing on a reading of your limits and he cannot "
            "afford to be wrong."
        ),
        "reads": (
            "He reads a player's growth as a challenge addressed to him "
            "personally. Compliment him and he escalates; refuse him and he "
            "escalates faster."
        ),
        "voice": {
            "register": "proud, clipped, honorifics used as weapons",
            "cadence": "statement, then a challenge; he never answers a question",
            "forbidden": "no modesty, no hedging, never the word 'maybe'",
        },
        "greeting": (
            "Junior Brother. Stand where the light reaches you. I would like to see "
            "what you have been doing down there."
        ),
        "warm": (
            "Junior Brother. You are not what I expected, and I have said so in "
            "front of people. Do not make me a liar."
        ),
        "cold": (
            "I told them. Say it. Go on -- tell them I told them, so it is your "
            "mouth and not mine."
        ),
    },
    "li_xinghe_type": {
        "name": "Sister Li",
        "faction": "isolationist",
        "title": "Matriarch of the Li Line",
        "wants": (
            "The family name to survive a generation it is not certain about. She "
            "will spend a person to keep a name, and she will decide which person."
        ),
        "reads": (
            "She reads a player's progress as a bargaining position, and rates it "
            "against how useful the player's family would be as allies."
        ),
        "voice": {
            "register": "warm surface, arithmetic underneath",
            "cadence": "concern for you, then a question about your clan",
            "forbidden": "no grief performed for her, never a favor named as such",
        },
        "greeting": (
            "You have grown since I last saw you, which means someone has been "
            "feeding you. Sit where the tea is hot."
        ),
        "warm": (
            "You came back to the warm side of the room. I noticed that before you "
            "sat down."
        ),
        "cold": (
            "The tea is still hot. I will not ask twice about the clan. That is "
            "not a threat; it is an accounting."
        ),
    },
    "wu_shen": {
        "name": "The One Who Waits",
        "faction": "none",
        "title": "Unranked",
        "wants": (
            "You. Specifically. Not what you carry -- you. Every other thing in "
            "this world is a formality he is waiting out."
        ),
        "reads": (
            "He reads you as already his, and corrects your grammar when you "
            "disagree. There is no reading here that returns a result about you."
        ),
        "voice": {
            "register": "impossibly polite; the politeness is the threat",
            "cadence": "questions that are not questions, and answers that are",
            "forbidden": "no anger, no raised voice, never names a realm",
        },
        "greeting": (
            "You have come a long way to stand in a room with me. I would like to "
            "correct one thing first: you did not choose this. You arrived."
        ),
        "warm": (
            "There you are. I was beginning to think I would have to be patient "
            "again, which I dislike."
        ),
        "cold": (
            "You left. That is a decision, and I will note it, and I will not "
            "raise it again. You may go."
        ),
    },
}


# === The favours ===
#
# Each entry is one request an NPC makes. The fields:
#   npc          -- who is asking
#   ask          -- what they want, in their words
#   cost         -- standing this COSTS the player, with a DIFFERENT NPC. This is
#                   the whole design. A favour paid in spirit stones is a
#                   purchase; a favour paid in standing with a sect elder is a
#                   choice with a loser.
#   cost_npc     -- who loses standing if this is accepted
#   cost_amount  -- how much (1 step; a conversation is a scene, not a grind)
#   gain         -- standing the asker GAINS on fulfilment
#   lead         -- the information the player walks away with. Never currency.
#   deadline_hours
#                -- per-favour override of FAVOUR_DEADLINE_HOURS
#   on_refuse    -- the NPC's line when the player says no
#   on_accept    -- the NPC's line when the player says yes
#   on_fail      -- the NPC's line when the deadline passes
#   on_settle    -- the NPC's line when the player resolves it in time
FAVOURS = {
    "qiu_water_levy_writ": {
        "npc": "village_elder",
        "ask": (
            "Carry a writ to the toll-house at the sect gateway. A sealed paper "
            "with my seal on it and nothing written inside. It only needs to "
            "arrive in a hand that is not mine."
        ),
        "cost_npc": "spirit_testing_examiner",
        "cost_amount": 1,
        "gain": 1,
        "lead": (
            "The levy has been raised three times in a season, and the increases "
            "were signed by an examiner who is not the one standing at the gate. "
            "The gate is being run by someone with a second seal."
        ),
        "on_accept": "Good. Then you are already involved, whether you meant to be or not.",
        "on_refuse": "As you like. The levy rises at the equinox either way. I only thought you were the sort of man who could read.",
        "on_fail": "The equinox came and went. I do not blame you. I am simply no longer able to say anything useful to you.",
        "on_settle": "It arrived. Then someone read it, and someone will now be very interested in your name. I am sorry. I am also not sorry.",
    },
    "jiu_sealed_jar": {
        "npc": "wandering_merchant",
        "ask": (
            "Take a sealed jar past the gate. I will not tell you what is in it, "
            "and if I did, you would have to know. Carry it. Do not open it. Do "
            "not be searched -- that part is yours to arrange."
        ),
        "cost_npc": "spirit_testing_examiner",
        "cost_amount": 2,
        "gain": 2,
        "lead": (
            "Every toll-house on the north road opens the same seal, and the seal "
            "is a mirror. Whatever the Bureau is measuring, it is measuring it "
            "twice, once for the ledger and once for someone else."
        ),
        "on_accept": "Two steps, then: do not open it, and do not be searched. One of those is easy. Think about which.",
        "on_refuse": "Sensible. Sensible people are why my licence is worth exactly what it is worth.",
        "on_fail": "The jar is in a ditch somewhere, and I am one conversation poorer. Do not buy anything from me today.",
        "on_settle": "Delivered, and unopened. I am in your debt, which is a thing I say rarely enough that you may weigh it.",
    },
    "wen_witness": {
        "npc": "light_sect_elder",
        "ask": (
            "Stand where the council can see you cultivate, and do well. That is "
            "all. I need the fact observed, not reported, and it must be observed "
            "by someone they cannot dismiss."
        ),
        "cost_npc": "core_disciple_wei_guang",
        "cost_amount": 2,
        "gain": 2,
        "lead": (
            "The reform wing's complaint is not doctrine. It is a set of numbers: "
            "outer disciples are being advanced on contribution records that were "
            "written by hand, and the handwriting is not any one person's. It is "
            "several people's, in turn."
        ),
        "on_accept": "Then you understand what I am asking. It is not strength. It is exposure.",
        "on_refuse": "Of course. I have made a habit of asking people things that cost them, and I have made a habit of being refused.",
        "on_fail": "The council did not see. They will have drawn their own conclusion, and it will be less accurate than yours.",
        "on_settle": "They saw. You will find that the hall is colder for a while. That is what I bought. Thank you.",
    },
    "wei_sparring_ground": {
        "npc": "core_disciple_wei_guang",
        "ask": (
            "Stand with me in the combat hall. Not against me -- beside me. Let "
            "them see us together, and let them decide what that means."
        ),
        "cost_npc": "light_sect_elder",
        "cost_amount": 1,
        "gain": 1,
        "lead": (
            "The combat line's roster is not a record of who fights. It is a record "
            "of who was in the room. Three of the names on it have not been seen "
            "outside the inner gate in eleven years."
        ),
        "on_accept": "Good. Say nothing clever. Clever is what gets people asked questions they cannot afford.",
        "on_refuse": "No. Fine. You were always going to be the one who reads the roster instead of standing in it.",
        "on_fail": "You did not come. They asked where you were. I did not answer, which will cost me more than it costs you.",
        "on_settle": "They saw. Now half of them think you are mine, and I have not corrected them. Remember that when it becomes inconvenient.",
    },
}


#: Favour ids in the order the roster surfaces them. Deterministic ordering so
#: the same request is not reshuffled between two page loads, which would make
#: the client look unstable to a player who is deciding something.
FAVOUR_ORDER = (
    "qiu_water_levy_writ",
    "jiu_sealed_jar",
    "wen_witness",
    "wei_sparring_ground",
)


def favour_deadline_hours(favour_id: str) -> float:
    """Deadline in hours for one favour, falling back to the module default.

    Never raises. This number is read out of the ``FAVOURS`` table in this
    file, and a caller has no way to fix it: ``npc_state.accept_favour``
    reaches it from the accept route, so a raise here turns a config typo
    into a failed request the player did nothing wrong on.

    Two different failures, two different answers:

    * A value that is not a number at all -- ``"soon"``, ``None``, an int too
      large for ``float`` -- falls back to ``FAVOUR_DEADLINE_HOURS``. There is
      no number here to rescue, so the module default is the only sensible
      answer, and it is the same value an absent override already returns.
    * A value that IS a number but not a usable one -- ``inf``, ``-inf``,
      ``nan``, and ``1e308`` (finite, but ``* 3600.0`` overflows to ``inf``) --
      is returned as it is, deliberately, and this is the load-bearing part.
      Clamping those to the default would be wrong twice over: it would hand
      back a real deadline for a value nobody set, and it would do so
      *before* the caller can tell the difference. The one caller,
      ``npc_state.accept_favour``, already wraps the computed sum in
      ``_readable_deadline``, which stores an unreadable deadline as ``None``
      -- the same value ``refuse_favour`` writes and ``favour_view`` reports.
      ``None`` is what the whole contract suite pins: the row stays
      standard-JSON encodable, the expiry sweep still closes the request, and
      the player is not locked out of a favour forever. Widening here rather
      than rejecting is the same rule as ``_stored_number`` and
      ``clamp_standing``.

    The shipped table cannot reach either branch: ``FAVOUR_DEADLINE_HOURS`` is
    24.0 and no entry in ``FAVOURS`` declares a ``deadline_hours`` at all, so
    this only fires on a config edit or a stale stored id.
    """
    favour = FAVOURS.get(favour_id) or {}
    try:
        return float(favour.get("deadline_hours", FAVOUR_DEADLINE_HOURS))
    except (TypeError, ValueError, OverflowError):
        # OverflowError is neither a TypeError nor a ValueError:
        # float(10**400) raises it.
        return FAVOUR_DEADLINE_HOURS


def clamp_standing(value: int) -> int:
    """Keep standing inside its declared bounds.

    Clamped rather than rejected so a long chain of favours cannot produce a
    number no dialogue line was written for.
    """
    return max(STANDING_MIN, min(STANDING_MAX, int(value)))
