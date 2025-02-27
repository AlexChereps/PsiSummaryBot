import logging
import datetime
import os
try:
    from datetime import UTC
except ImportError:
    UTC = None
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, CommandHandler, CallbackContext
from collections import defaultdict
import google.generativeai as genai

# Настройка логирования в файл с уровнем INFO
log_file = os.path.join(os.path.dirname(__file__), 'PsiSummary_ExecutionLog.log')
logging.basicConfig(
    filename=log_file,
    filemode='w',  # Перезапись файла при каждом запуске
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Настройка Google Gemini API (вставьте ваш ключ)
GOOGLE_API_KEY = "YOUR_GOOGLE_API_KEY_HERE"  # Замените на ваш API-ключ от Google Gemini
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Хранение сообщений за последние 3 часа
messages = defaultdict(list)

# Максимальная длина текста одной "мысли" (4000 символов)
MAX_TEXT_LENGTH_PER_THOUGHT = 4000

# Временной порог для разделения мыслей (5 минут в секундах)
THOUGHT_GAP_SECONDS = 300

# Список разрешённых чатов (укажите ID ваших чатов, например, ["-1001234567890", "-1009876543210"])
ALLOWED_CHAT_IDS = ["YOUR_CHAT_ID_1_HERE", "YOUR_CHAT_ID_2_HERE"]  # Замените на ID ваших чатов

# Функция для получения текущего времени в UTC
def get_utc_now():
    if UTC is not None:  # Python 3.11+
        return datetime.datetime.now(UTC)
    else:  # Для старых версий Python
        return datetime.datetime.utcnow()

# Функция обработки сообщений (текст и фото, кроме команд)
async def message_handler(update: Update, context: CallbackContext):
    if not update.message:
        logger.debug("Update has no message, skipping.")
        return
    
    # Проверяем, что сообщение из разрешённого чата
    if str(update.message.chat_id) not in ALLOWED_CHAT_IDS:
        logger.debug(f"Message from chat {update.message.chat_id} is not from allowed chats {ALLOWED_CHAT_IDS}, skipping.")
        return
    
    # Пропускаем команды
    if update.message.text and update.message.text.startswith('/'):
        logger.debug(f"Message '{update.message.text}' is a command, skipping in message_handler.")
        return
    
    user = update.message.from_user.username or update.message.from_user.first_name
    timestamp = get_utc_now()

    # Проверяем, является ли сообщение репостом через forward_origin
    if update.message.forward_origin:
        # Определяем источник пересылки
        if hasattr(update.message.forward_origin, 'sender_user') and update.message.forward_origin.sender_user:
            forward_origin = update.message.forward_origin.sender_user.username or "неизвестного пользователя"
        elif hasattr(update.message.forward_origin, 'chat') and update.message.forward_origin.chat:
            forward_origin = update.message.forward_origin.chat.username or "неизвестного чата"
        else:
            forward_origin = "неизвестного источника"
        
        forward_text = update.message.text or update.message.caption or "[без текста]"
        messages[user].append((timestamp, f"[репост из {forward_origin}]: {forward_text}"))
        logger.info(f"Forwarded message from {user} from {forward_origin}: {forward_text}")
    else:
        # Обработка текста
        if update.message.text:
            messages[user].append((timestamp, update.message.text))
            logger.info(f"Text message from {user}: {update.message.text}")
        
        # Обработка фото
        elif update.message.photo:
            caption = update.message.caption
            if caption:
                messages[user].append((timestamp, caption))
                logger.info(f"Photo with caption from {user}: {caption}")
            else:
                messages[user].append((timestamp, "[изображение]"))
                logger.info(f"Photo without caption from {user}")
    
    # Ограничиваем количество сообщений на пользователя (например, 50)
    if len(messages[user]) > 50:
        messages[user] = messages[user][-50:]

# Очистка старых сообщений (старше 3 часов)
def cleanup_old_messages():
    now = get_utc_now()
    for user in list(messages.keys()):
        messages[user] = [(ts, msg) for ts, msg in messages[user] if (now - ts).total_seconds() <= 10800]  # 3 часа = 10800 секунд
        if not messages[user]:
            del messages[user]
    logger.info("Cleaned up old messages.")

# Разделение сообщений на "мысли" по временному разрыву
def split_into_thoughts(user_messages):
    if not user_messages:
        return []
    
    thoughts = []
    current_thought = []
    prev_timestamp = None

    for timestamp, msg in user_messages:
        if prev_timestamp is None:
            current_thought.append(msg)
        else:
            time_diff = (timestamp - prev_timestamp).total_seconds()
            if time_diff > THOUGHT_GAP_SECONDS:  # Новый "мысленный блок" после 5 минут
                thoughts.append(current_thought)
                current_thought = [msg]
            else:
                current_thought.append(msg)
        prev_timestamp = timestamp
    
    if current_thought:
        thoughts.append(current_thought)
    
    logger.info(f"Split messages into {len(thoughts)} thoughts.")
    return thoughts

# Генерация саммари с помощью Gemini 1.5 Flash
def generate_summary():
    logger.info("Generating summary...")
    cleanup_old_messages()
    
    if not messages:
        logger.info("No messages in the last 3 hours.")
        return "За последние 3 часа сообщений не было."
    
    prompt = """
    Ты — помощник, который составляет подробное саммари обсуждений за последние 3 часа. 
    Пользователи обсуждали разные темы в чате и отправляли изображения или пересылали сообщения (репосты). 
    Твоя задача — детально проанализировать, что сказал каждый активный участник, разделяя их высказывания на отдельные "мысли", 
    если между сообщениями был перерыв более 5 минут. 
    - Если сообщение — это репост (отмечено как "[репост из <источник>]: <текст>"), укажи, что пользователь переслал сообщение, 
      и кратко опиши суть текста репоста, но не приписывай авторство текста этому пользователю.
    - Если участник опубликовал новость (текст, который выглядит как заголовок или описание события), 
      удели больше внимания её анализу: опиши суть новости, возможные детали и контекст.
    - Если в "мысли" есть только изображения без текста (отмечено как "[изображение]"), укажи это как "пользователь отправлял смешные картинки".
    Не упоминай тех, кто ничего не говорил. Не повторяй сообщения дословно, а передавай их суть подробно.

    Вот сообщения пользователей, разделённые на "мысли":
    """
    
    for user, user_msgs in messages.items():
        thoughts = split_into_thoughts(user_msgs)
        if not thoughts:
            continue
        prompt += f"\n{user}:\n"
        for i, thought in enumerate(thoughts, 1):
            thought_text = "\n".join(msg for msg in thought)
            if len(thought_text) > MAX_TEXT_LENGTH_PER_THOUGHT:
                thought_text = thought_text[:MAX_TEXT_LENGTH_PER_THOUGHT] + "... [текст обрезан]"
            prompt += f"Мысль {i}:\n{thought_text}\n"
    
    prompt += """
    Сформулируй подробный отчёт. Для каждого участника начинай с новой строки, выделяя его имя жирным шрифтом (например, **Имя**). 
    Для каждой "мысли" дай подробное описание смысла. Если это репост, укажи источник и суть пересланного текста. 
    Если это новость, детализируй её содержание. Разделяй мысли одного участника символом " | ". 
    Если у пользователя только одна мысль, опиши её без разделителей.
    """
    
    try:
        logger.info("Sending request to Gemini API...")
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.7,
                max_output_tokens=2000
            )
        )
        logger.info("Received response from Gemini API.")
        return response.text.strip()
    except Exception as e:
        logger.error(f"Error in Gemini API request: {e}")
        return f"Произошла ошибка: {str(e)}"

# Команда /summary для запроса отчёта
async def summary_command(update: Update, context: CallbackContext):
    logger.info(f"Received update: {update.to_dict()}")
    if not update.message or not update.message.text:
        logger.info("Summary command received but no message object or text, skipping.")
        return
    
    user = update.message.from_user.username or update.message.from_user.first_name
    chat_id = update.message.chat_id
    bot_name = "@PsiSummaryBot"
    logger.info(f"User {user} in chat {chat_id} triggered command with text: '{update.message.text}'")
    
    # Проверяем, что команда отправлена из разрешённого чата
    if str(chat_id) not in ALLOWED_CHAT_IDS:
        logger.info(f"Command from chat {chat_id} is not from allowed chats {ALLOWED_CHAT_IDS}, skipping.")
        await update.message.reply_text("Этот бот работает только в определённых чатах.")
        return
    
    # Проверяем команду /summary или /summary@PsiSummaryBot
    if update.message.text.strip() in ("/summary", f"/summary{bot_name}"):
        logger.info(f"Command matched: {update.message.text}")
        summary = generate_summary()
        await update.message.reply_text(summary, parse_mode='Markdown')
    else:
        logger.info(f"Message '{update.message.text}' does not match '/summary' or '/summary{bot_name}', skipping.")

# Обработчик ошибок
async def error_handler(update: object, context: CallbackContext):
    logger.error("Exception while handling an update:", exc_info=context.error)
    error_message = f"Произошла ошибка: {str(context.error)}"
    if update and getattr(update, 'message', None):
        await update.message.reply_text(error_message)
    else:
        logger.warning("Cannot send error message to chat: update has no message object.")

# Главная функция для запуска бота
def main():
    TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"  # Замените на ваш токен от Telegram BotFather
    app = Application.builder().token(TOKEN).build()

    # Добавляем обработчики (CommandHandler идёт первым для приоритета)
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO & ~filters.COMMAND, message_handler))
    
    # Добавляем обработчик ошибок
    app.add_error_handler(error_handler)

    # Запуск бота
    logger.info("Starting bot...")
    app.run_polling()

if __name__ == "__main__":
    main()