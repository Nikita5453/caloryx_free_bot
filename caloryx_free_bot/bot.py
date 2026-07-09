import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "caloryx_free.sqlite3"
WEBAPP_DIR = BASE_DIR / "webapp"

GENDER, AGE, HEIGHT, WEIGHT, ACTIVITY, GOAL, FOOD_TEXT, FOOD_CALORIES = range(8)

ACTIVITY_FACTORS = {
    "low": ("Низкая", 1.2),
    "light": ("Легкая", 1.375),
    "medium": ("Средняя", 1.55),
    "high": ("Высокая", 1.725),
}

GOAL_DELTAS = {
    "loss": ("Похудение", -400),
    "maintain": ("Поддержание", 0),
    "gain": ("Набор", 300),
}

FOOD_DB = {
    "овсянка": 88,
    "гречка": 110,
    "рис": 130,
    "макароны": 150,
    "картофель": 87,
    "курица": 165,
    "индейка": 135,
    "говядина": 250,
    "рыба": 150,
    "лосось": 208,
    "тунец": 132,
    "яйцо": 155,
    "творог": 121,
    "йогурт": 70,
    "сыр": 350,
    "молоко": 52,
    "банан": 89,
    "яблоко": 52,
    "апельсин": 47,
    "хлеб": 250,
    "салат": 25,
    "огурец": 15,
    "помидор": 18,
    "авокадо": 160,
    "орехи": 600,
    "шоколад": 540,
    "суп": 55,
}

def build_main_keyboard(web_app_url: Optional[str] = None) -> ReplyKeyboardMarkup:
    if web_app_url and web_app_url.startswith("https://"):
        rows = [[KeyboardButton("📸 Сканировать", web_app=WebAppInfo(url=web_app_url))]]
    else:
        rows = [["📸 Сканировать"]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


MAIN_KEYBOARD = build_main_keyboard()


@dataclass
class ParsedFood:
    name: str
    grams: int
    kcal_per_100: Optional[int]

    @property
    def calories(self) -> Optional[int]:
        if self.kcal_per_100 is None:
            return None
        return round(self.kcal_per_100 * self.grams / 100)


def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                gender TEXT NOT NULL,
                age INTEGER NOT NULL,
                height INTEGER NOT NULL,
                weight REAL NOT NULL,
                activity TEXT NOT NULL,
                goal TEXT NOT NULL,
                calories_target INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                food_name TEXT NOT NULL,
                grams INTEGER NOT NULL,
                calories INTEGER NOT NULL,
                created_on TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def db_execute(query: str, params: tuple = ()) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(query, params)
        conn.commit()


def db_one(query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchone()


def db_all(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def calc_target(gender: str, age: int, height: int, weight: float, activity: str, goal: str) -> int:
    if gender == "male":
        bmr = 10 * weight + 6.25 * height - 5 * age + 5
    else:
        bmr = 10 * weight + 6.25 * height - 5 * age - 161
    return max(1000, round(bmr * ACTIVITY_FACTORS[activity][1] + GOAL_DELTAS[goal][1]))


def get_profile(user_id: int) -> Optional[sqlite3.Row]:
    return db_one("SELECT * FROM profiles WHERE user_id = ?", (user_id,))


def record_meal(user_id: int, food_name: str, grams: int, calories: int) -> None:
    now = datetime.now()
    db_execute(
        """
        INSERT INTO meals (user_id, food_name, grams, calories, created_on, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            food_name,
            grams,
            calories,
            now.date().isoformat(),
            now.isoformat(timespec="seconds"),
        ),
    )


def parse_food(text: str) -> ParsedFood:
    clean = re.sub(r"\s+", " ", text.lower()).strip()
    grams_match = re.search(r"(\d{2,4})\s*(г|гр|грамм)?$", clean)
    grams = 100
    if grams_match:
        grams = int(grams_match.group(1))
        clean = clean[: grams_match.start()].strip()
    name = clean or "продукт"

    kcal = None
    for key, value in FOOD_DB.items():
        if key in name:
            kcal = value
            break
    return ParsedFood(name=name, grams=grams, kcal_per_100=kcal)


def calories_left_text(user_id: int) -> str:
    profile = get_profile(user_id)
    if not profile:
        return "Сначала настрой профиль: /profile"

    today = date.today().isoformat()
    row = db_one(
        "SELECT COALESCE(SUM(calories), 0) AS total FROM meals WHERE user_id = ? AND created_on = ?",
        (user_id, today),
    )
    eaten = int(row["total"])
    target = int(profile["calories_target"])
    left = target - eaten
    status = "осталось" if left >= 0 else "перебор"
    return (
        f"Сегодня: {eaten} ккал\n"
        f"Цель: {target} ккал\n"
        f"{status.capitalize()}: {abs(left)} ккал"
    )


def profile_text(profile: sqlite3.Row) -> str:
    return (
        "Твой профиль:\n"
        f"Пол: {'мужской' if profile['gender'] == 'male' else 'женский'}\n"
        f"Возраст: {profile['age']}\n"
        f"Рост: {profile['height']} см\n"
        f"Вес: {profile['weight']} кг\n"
        f"Активность: {ACTIVITY_FACTORS[profile['activity']][0]}\n"
        f"Цель: {GOAL_DELTAS[profile['goal']][0]}\n"
        f"Норма: {profile['calories_target']} ккал/день"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    if profile:
        text = (
            "Готово, доступ бесплатный для всех функций.\n"
            "Чтобы открыть приложение, нажми синюю кнопку «📸 Сканировать».\n\n"
            + calories_left_text(user_id)
        )
    else:
        text = (
            "Привет! Я бесплатный бот для дневника калорий.\n\n"
            "Чтобы начать, нажми синюю кнопку «📸 Сканировать». "
            "Профиль и дневник теперь открываются внутри Mini App."
        )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Мужской", callback_data="gender:male"),
                InlineKeyboardButton("Женский", callback_data="gender:female"),
            ]
        ]
    )
    await update.message.reply_text("Выбери пол:", reply_markup=keyboard)
    return GENDER


async def set_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["gender"] = query.data.split(":", 1)[1]
    await query.edit_message_text("Сколько тебе лет? Напиши число.")
    return AGE


async def set_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = int(update.message.text)
        if not 10 <= age <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Напиши возраст числом от 10 до 100.")
        return AGE
    context.user_data["age"] = age
    await update.message.reply_text("Укажи рост в сантиметрах.")
    return HEIGHT


async def set_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        height = int(update.message.text)
        if not 100 <= height <= 240:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Напиши рост числом от 100 до 240.")
        return HEIGHT
    context.user_data["height"] = height
    await update.message.reply_text("Укажи вес в килограммах. Можно с точкой: 72.5")
    return WEIGHT


async def set_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        weight = float(update.message.text.replace(",", "."))
        if not 30 <= weight <= 300:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Напиши вес числом от 30 до 300.")
        return WEIGHT
    context.user_data["weight"] = weight

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=f"activity:{key}")]
            for key, (label, _factor) in ACTIVITY_FACTORS.items()
        ]
    )
    await update.message.reply_text("Какая у тебя активность?", reply_markup=keyboard)
    return ACTIVITY


async def set_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["activity"] = query.data.split(":", 1)[1]
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=f"goal:{key}")]
            for key, (label, _delta) in GOAL_DELTAS.items()
        ]
    )
    await query.edit_message_text("Какая цель?", reply_markup=keyboard)
    return GOAL


async def set_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = context.user_data
    goal = query.data.split(":", 1)[1]
    target = calc_target(
        data["gender"],
        data["age"],
        data["height"],
        data["weight"],
        data["activity"],
        goal,
    )
    now = datetime.now().isoformat(timespec="seconds")

    db_execute(
        """
        INSERT INTO profiles (
            user_id, gender, age, height, weight, activity, goal, calories_target, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            gender = excluded.gender,
            age = excluded.age,
            height = excluded.height,
            weight = excluded.weight,
            activity = excluded.activity,
            goal = excluded.goal,
            calories_target = excluded.calories_target,
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            data["gender"],
            data["age"],
            data["height"],
            data["weight"],
            data["activity"],
            goal,
            target,
            now,
            now,
        ),
    )

    profile = get_profile(user_id)
    await query.edit_message_text(profile_text(profile))
    await context.bot.send_message(
        chat_id=user_id,
        text="Профиль сохранен. Теперь можно добавлять еду.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not get_profile(update.effective_user.id):
        await update.message.reply_text("Сначала настрой профиль: /profile")
        return ConversationHandler.END
    await update.message.reply_text("Напиши продукт и граммы. Например: гречка 200")
    return FOOD_TEXT


async def save_food_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed = parse_food(update.message.text)
    context.user_data["pending_food"] = parsed
    if parsed.calories is None:
        await update.message.reply_text(
            f"Я не знаю калорийность для: {parsed.name}.\n"
            "Напиши калорийность на 100 г числом."
        )
        return FOOD_CALORIES

    await save_meal(update, parsed)
    return ConversationHandler.END


async def save_food_calories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        kcal = int(update.message.text)
        if not 1 <= kcal <= 1000:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Напиши калорийность на 100 г числом от 1 до 1000.")
        return FOOD_CALORIES

    parsed = context.user_data["pending_food"]
    parsed.kcal_per_100 = kcal
    await save_meal(update, parsed)
    return ConversationHandler.END


async def save_meal(update: Update, parsed: ParsedFood) -> None:
    calories = parsed.calories
    record_meal(update.effective_user.id, parsed.name, parsed.grams, calories)
    await update.message.reply_text(
        f"Добавлено: {parsed.name}, {parsed.grams} г, {calories} ккал.\n\n"
        + calories_left_text(update.effective_user.id),
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not get_profile(update.effective_user.id):
        await update.message.reply_text("Сначала настрой профиль: /profile", reply_markup=MAIN_KEYBOARD)
        return

    try:
        payload = json.loads(update.effective_message.web_app_data.data)
        food_name = str(payload["food_name"]).strip()[:80] or "Скан продукта"
        grams = int(payload.get("grams", 100))
        calories = int(payload["calories"])
        if not 1 <= grams <= 5000 or not 1 <= calories <= 10000:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        await update.message.reply_text("Не получилось прочитать данные из приложения.", reply_markup=MAIN_KEYBOARD)
        return

    record_meal(update.effective_user.id, food_name, grams, calories)
    await update.message.reply_text(
        f"Добавлено из приложения: {food_name}, {grams} г, {calories} ккал.\n\n"
        + calories_left_text(update.effective_user.id),
        reply_markup=MAIN_KEYBOARD,
    )


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(calories_left_text(update.effective_user.id), reply_markup=MAIN_KEYBOARD)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db_all(
        """
        SELECT food_name, grams, calories, created_at
        FROM meals
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 10
        """,
        (update.effective_user.id,),
    )
    if not rows:
        await update.message.reply_text("История пока пустая.", reply_markup=MAIN_KEYBOARD)
        return
    lines = ["Последние записи:"]
    for row in rows:
        created = datetime.fromisoformat(row["created_at"]).strftime("%d.%m %H:%M")
        lines.append(f"{created} - {row['food_name']}, {row['grams']} г, {row['calories']} ккал")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db_execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))
    db_execute("DELETE FROM meals WHERE user_id = ?", (user_id,))
    await update.message.reply_text("Профиль и дневник очищены.", reply_markup=MAIN_KEYBOARD)


async def profile_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = get_profile(update.effective_user.id)
    if not profile:
        await update.message.reply_text("Профиль еще не настроен. Запусти /profile")
        return
    await update.message.reply_text(profile_text(profile), reply_markup=MAIN_KEYBOARD)


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.lower().strip()
    if text == "добавить еду":
        await add_command(update, context)
    elif text == "📸 сканировать":
        await update.message.reply_text(
            "Чтобы эта кнопка открывала приложение, добавь публичную https-ссылку в WEB_APP_URL внутри .env.",
            reply_markup=MAIN_KEYBOARD,
        )
    elif text == "сегодня":
        await today_command(update, context)
    elif text == "профиль":
        await profile_show(update, context)
    elif text == "история":
        await history_command(update, context)
    elif get_profile(update.effective_user.id):
        parsed = parse_food(text)
        if parsed.calories is None:
            await update.message.reply_text(
                "Не узнал продукт. Используй /add, чтобы добавить его с калорийностью вручную."
            )
        else:
            await save_meal(update, parsed)
    else:
        await update.message.reply_text("Сначала настрой профиль: /profile", reply_markup=MAIN_KEYBOARD)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, отменил.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


def build_app(token: str) -> Application:
    profile_conv = ConversationHandler(
        entry_points=[CommandHandler("profile", profile_command)],
        states={
            GENDER: [CallbackQueryHandler(set_gender, pattern=r"^gender:")],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_age)],
            HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_height)],
            WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_weight)],
            ACTIVITY: [CallbackQueryHandler(set_activity, pattern=r"^activity:")],
            GOAL: [CallbackQueryHandler(set_goal, pattern=r"^goal:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    food_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_command),
            MessageHandler(filters.Regex(r"^Добавить еду$"), add_command),
        ],
        states={
            FOOD_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_food_text)],
            FOOD_CALORIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_food_calories)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app = Application.builder().token(token).build()
    app.add_handler(profile_conv)
    app.add_handler(food_conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))
    return app


def analyze_food_with_openai(description: str) -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "input": [
            {
                "role": "system",
                "content": (
                    "Ты нутрициологический парсер для дневника калорий. "
                    "По описанию блюда верни только JSON без markdown. "
                    "Оцени готовое блюдо, способ приготовления и размер порции. "
                    "Поля: food_name string, portion_label string, grams integer, "
                    "calories integer, protein integer, fat integer, carbs integer, "
                    "usefulness number от 0 до 10. Если данных мало, сделай разумную оценку."
                ),
            },
            {
                "role": "user",
                "content": description,
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "food_analysis",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "food_name": {"type": "string"},
                        "portion_label": {"type": "string"},
                        "grams": {"type": "integer", "minimum": 1, "maximum": 5000},
                        "calories": {"type": "integer", "minimum": 1, "maximum": 10000},
                        "protein": {"type": "integer", "minimum": 0, "maximum": 1000},
                        "fat": {"type": "integer", "minimum": 0, "maximum": 1000},
                        "carbs": {"type": "integer", "minimum": 0, "maximum": 1000},
                        "usefulness": {"type": "number", "minimum": 0, "maximum": 10},
                    },
                    "required": [
                        "food_name",
                        "portion_label",
                        "grams",
                        "calories",
                        "protein",
                        "fat",
                        "carbs",
                        "usefulness",
                    ],
                },
            }
        },
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        data = json.loads(response.read().decode("utf-8"))

    output_text = data.get("output_text")
    if not output_text:
        chunks = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    chunks.append(content.get("text", ""))
        output_text = "".join(chunks)
    return json.loads(output_text)


class WebAppApiHandler(BaseHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/api/analyze-food":
            self.send_json({"error": "Not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            description = str(payload.get("description", "")).strip()
            if len(description) < 3:
                raise ValueError("description is too short")
            result = analyze_food_with_openai(description[:600])
            self.send_json(result)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, 500)
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "Некорректное описание блюда"}, 400)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            logging.exception("OpenAI HTTP error: %s", body)
            message = "OpenAI API вернул ошибку. Проверьте ключ и баланс."
            try:
                error_payload = json.loads(body)
                message = error_payload.get("error", {}).get("message", message)
            except json.JSONDecodeError:
                pass
            self.send_json({"error": message[:240]}, 502)
        except Exception:
            logging.exception("Food analysis failed")
            self.send_json({"error": "Не получилось обработать блюдо через ИИ"}, 500)

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        logging.info("Mini App API: " + format, *args)


def start_webapp_api() -> None:
    host = os.getenv("WEBAPP_API_HOST", "127.0.0.1")
    port = int(os.getenv("PORT") or os.getenv("WEBAPP_API_PORT", "8787"))
    server = ThreadingHTTPServer((host, port), WebAppApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.info("Mini App API listening on http://%s:%s", host, port)


def main() -> None:
    global MAIN_KEYBOARD

    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Добавьте BOT_TOKEN в .env")
    MAIN_KEYBOARD = build_main_keyboard(os.getenv("WEB_APP_URL"))

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    init_db()
    start_webapp_api()
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    build_app(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
