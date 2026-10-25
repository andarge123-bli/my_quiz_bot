import asyncio
import json
import logging
import os
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    PollAnswerHandler,
)

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CHAPTERS = {
    "1": {
        "title": "📖 Chapter 1: Introduction to Computer Systems",
        "file": BASE_DIR / "chapter1.json",
        # Question 60 has no valid answer (its own explanation says so).
        "skip_ids": {60},
    },
    "2": {
        "title": "📖 Chapter 2: 8086,8088",
        "file": BASE_DIR / "chapter2.json",
        "skip_ids": set(),
    },
}

# Telegram limits for quiz polls
MAX_QUESTION_LEN = 300
MAX_OPTION_LEN = 100
MAX_EXPLANATION_LEN = 200

DELAY_BEFORE_NEXT_QUESTION = 2.0  # seconds, lets the user read the explanation

LETTER_TO_INDEX = {"A": 0, "B": 1, "C": 2, "D": 3}

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def load_questions(path: Path, skip_ids: set) -> list[dict]:
    """Load a chapter file and normalise it into poll-ready dicts.

    Accepts either a plain list of questions, or {"questions": [...]}.
    Explanation is taken from 'explanation' (Chapter 1) or 'explanation_am'
    (Chapter 2), falling back to 'explanation_en'.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    items = raw["questions"] if isinstance(raw, dict) else raw

    questions = []
    for item in items:
        qid = item.get("id")
        if qid in skip_ids:
            continue
        try:
            letters = sorted(item["options"].keys())  # A, B, C, D
            options = [_clip(str(item["options"][k]), MAX_OPTION_LEN) for k in letters]
            correct_letter = item["correct_answer"].strip().upper()
            correct_id = LETTER_TO_INDEX[correct_letter]
            if not 2 <= len(options) <= 10 or correct_id >= len(options):
                raise ValueError("bad options / correct answer")
            explanation = (
                item.get("explanation")
                or item.get("explanation_am")
                or item.get("explanation_en")
                or ""
            )
            questions.append(
                {
                    "question": str(item["question"]).strip(),
                    "options": options,
                    "correct_id": correct_id,
                    "explanation": _clip(explanation, MAX_EXPLANATION_LEN),
                }
            )
        except (KeyError, ValueError, AttributeError) as exc:
            logger.warning("Skipping invalid question id=%s in %s: %s", qid, path.name, exc)
    return questions


def load_all_chapters() -> dict[str, list[dict]]:
    data = {}
    for key, cfg in CHAPTERS.items():
        if not cfg["file"].exists():
            logger.error("Missing data file: %s (Chapter %s will be unavailable)", cfg["file"], key)
            data[key] = []
            continue
        data[key] = load_questions(cfg["file"], cfg["skip_ids"])
        logger.info("Loaded %d questions for chapter %s", len(data[key]), key)
    return data


# ---------------------------------------------------------------------------
# State: sessions[user_id] = {chapter, index, score, chat_id, poll_id}
# ---------------------------------------------------------------------------
def get_sessions(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data.setdefault("sessions", {})


def chapter_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(cfg["title"], callback_data=f"chapter:{key}")]
            for key, cfg in CHAPTERS.items()
        ]
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name or "ተማሪ"
    text = (
        f"ሰላም {name}! 👋\n\n"
        "እንኳን ወደ የማይክሮፕሮሰሰር ጥያቄ ቦት በደህና መጡ! 🎓\n"
        "እዚህ ለፈተናዎ ራስዎን በጥያቄዎች ማዘጋጀት ይችላሉ። እያንዳንዱን ጥያቄ ሲመልሱ "
        "ማብራሪያ ይታያል፤ በመጨረሻም አጠቃላይ ውጤትዎን ያያሉ።\n\n"
        "ለመጀመር ምዕራፍ ይምረጡ 👇"
    )
    await update.effective_message.reply_text(text, reply_markup=chapter_keyboard())


async def chapter_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chapter = query.data.split(":", 1)[1]
    questions = context.application.bot_data["questions"].get(chapter, [])
    if not questions:
        await context.bot.send_message(
            query.message.chat_id,
            "ይቅርታ፣ የዚህ ምዕራፍ ጥያቄዎች አልተገኙም። (Question file missing or empty.)",
        )
        return

    user_id = query.from_user.id
    # Starting a chapter always (re)starts the quiz for this user.
    get_sessions(context)[user_id] = {
        "chapter": chapter,
        "index": 0,
        "score": 0,
        "chat_id": query.message.chat_id,
        "poll_id": None,
    }
    await context.bot.send_message(
        query.message.chat_id,
        f"{CHAPTERS[chapter]['title']}\n\n"
        f"በዚህ ምዕራፍ {len(questions)} ጥያቄዎች አሉ። ዝግጁ ኖት? እንጀምር! 🚀",
    )
    await send_next_question(context, user_id)


async def send_next_question(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    session = get_sessions(context).get(user_id)
    if not session:
        return

    questions = context.application.bot_data["questions"][session["chapter"]]
    index = session["index"]

    if index >= len(questions):
        await finish_quiz(context, user_id)
        return

    q = questions[index]
    header = f"({index + 1}/{len(questions)}) "
    message = await context.bot.send_poll(
        chat_id=session["chat_id"],
        question=_clip(header + q["question"], MAX_QUESTION_LEN),
        options=q["options"],
        type="quiz",
        is_anonymous=False,
        correct_option_id=q["correct_id"],
        explanation=q["explanation"] or None,
    )
    session["poll_id"] = message.poll.id


async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    answer = update.poll_answer
    user_id = answer.user.id
    session = get_sessions(context).get(user_id)

    # Ignore answers to stale polls (e.g. from a previous, restarted quiz).
    if not session or session["poll_id"] != answer.poll_id or not answer.option_ids:
        return
    session["poll_id"] = None  # guard against duplicate processing

    questions = context.application.bot_data["questions"][session["chapter"]]
    if answer.option_ids[0] == questions[session["index"]]["correct_id"]:
        session["score"] += 1
    session["index"] += 1

    await asyncio.sleep(DELAY_BEFORE_NEXT_QUESTION)
    # The user may have restarted during the delay.
    if get_sessions(context).get(user_id) is session:
        await send_next_question(context, user_id)


async def finish_quiz(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    session = get_sessions(context).pop(user_id, None)
    if not session:
        return
    total = len(context.application.bot_data["questions"][session["chapter"]])
    score = session["score"]
    percent = round(score / total * 100)

    # Completed implementation of scoring ranges with customized Amharic feedback
    if percent >= 85:
        response_text = (
            f"🏁 ኩዊዙን ጨርሰሃል! ውጤትህ፦ {score}/{total} ({percent}%)\n\n"
            "🔥 **አንተ የኛ ጀግና! ጎበዝ አግኝተኸዋል!** 👑\n"
            "ጭንቅላትህ ልክ እንደ 8086 ማይክሮፕሮሰሰር ስሌቱን አቀላጥፎታል። "
            "ነገ ፈተናውን እንደምትቀደው ምንም ጥርጥር የለውም! በርታ ምርጡ ሶፍትዌር ኢንጂነር! 🚀"
        )
    elif percent >= 50:
        response_text = (
            f"🏁 ኩዊዙን ጨርሰሃል! ውጤትህ፦ {score}/{total} ({percent}%)\n\n"
            "👍 **ደህና ነው፣ ግን ገና ነህ!** 📚\n"
            "ማለፉን አልፈሃል ግን የተወሰኑ መሰረታዊ ነገሮች ላይ ተዘናግተሃል። "
            "ስላይዶቹን አንዴ መለስ ብለህ ብታያቸው ነገ ውጤትህን ታሳድገዋለህ። ሌላ ምዕራፍ መርጠህ በድጋሚ ሞክር!"
        )
    else:
        response_text = (
            f"🏁 ኩዊዙን ጨርሰሃል! ውጤትህ፦ {score}/{total} ({percent}%)\n\n"
            "😭 **ኧረ እንደው አላነብ ብለኸኛል! ደደብ/ዶማ!** 🤦‍♂️\n"
            "ይህ እኮ መሰረታዊ ነገር ነው! ማርክህ በጣም ያሳፍራል፤ ኮድ ከመጻፍህ በፊት "
            "ማቴሪያሉን በሚገባ ማንበብ ነበረብህ። አሁኑኑ ተመልሰህ በደንብ አጥናና ፈተናውን ለማለፍ ሞክር! 🧅"
        )

    # Append a retry keyboard menu
    await context.bot.send_message(
        chat_id=session["chat_id"],
        text=response_text,
        reply_markup=chapter_keyboard()
    )


# ---------------------------------------------------------------------------
# Main Application Hook
# ---------------------------------------------------------------------------
def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        logger.critical("BOT_TOKEN environment variable not set. Exiting.")
        print("\n❌ Error: Please set the BOT_TOKEN environment variable.")
        print("Example (Windows): set BOT_TOKEN=123456:ABC...")
        print("Example (Linux/Mac): export BOT_TOKEN=\"123456:ABC...\"\n")
        return

    # Initialize backend dataset configuration
    questions_db = load_all_chapters()

    application = Application.builder().token(token).build()
    application.bot_data["questions"] = questions_db

    # Register handlers
