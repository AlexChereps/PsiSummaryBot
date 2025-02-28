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
    filemode='w',
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Переменные конфигурации
GOOGLE_API_KEY = "YOUR_GOOGLE_API_KEY"  # Замените на ваш API-ключ от Google Gemini
TOKEN = "YOUR_TOKEN"  # Токен от Telegram BotFather
ALLOWED_CHAT_IDS = ["ID_1", "ID_2"]  # Список ID разрешённых чатов как строки
MAX_TEXT_LENGTH_PER_THOUGHT = 500  # Максимальная длина текста одной "мысли" (символы)
MAX_SUMMARY_LENGTH = 3000  # Максимальная длина саммари (символы)
THOUGHT_GAP_SECONDS = 30  # Временной порог для разделения мыслей (секунды)

# Хранение сообщений за последние 3 часа
messages = defaultdict(list)

# Хранение данных о вызовах /summary для каждого пользователя
summary_calls = defaultdict(lambda: {'count': 0, 'last_time': None})

# Инициализация Google Gemini API
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Функция для получения текущего времени в UTC
def get_utc_now():
    if UTC is not None:
        return datetime.datetime.now(UTC)
    else:
        return datetime.datetime.utcnow()

# Экранирование специальных символов для имени пользователя (для HTML достаточно минимального экранирования)
def escape_html(text):
    """Экранирует специальные символы для HTML."""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

# Функция обработки сообщений (текст и фото, кроме команд)
async def message_handler(update: Update, context: CallbackContext):
    if not update.message:
        logger.debug("Update has no message, skipping.")
        return
    
    if str(update.message.chat_id) not in ALLOWED_CHAT_IDS:
        logger.debug(f"Message from chat {update.message.chat_id} is not from allowed chats {ALLOWED_CHAT_IDS}, skipping.")
        return
    
    if update.message.text and update.message.text.startswith('/'):
        logger.debug(f"Message '{update.message.text}' is a command, skipping in message_handler.")
        return
    
    user = update.message.from_user.username or update.message.from_user.first_name
    timestamp = get_utc_now()
    chat_id = str(update.message.chat_id).replace('-100', '')
    message_id = update.message.message_id

    text = update.message.text or update.message.caption or "[изображение]"
    if 1 <= len(text) <= 15:
        logger.debug(f"Message from {user} ignored due to length {len(text)}: {text}")
        return

    if update.message.forward_origin:
        if hasattr(update.message.forward_origin, 'sender_user') and update.message.forward_origin.sender_user:
            forward_origin = update.message.forward_origin.sender_user.username or "неизвестного пользователя"
        elif hasattr(update.message.forward_origin, 'chat') and update.message.forward_origin.chat:
            forward_origin = update.message.forward_origin.chat.username or "неизвестного чата"
        else:
            forward_origin = "неизвестного источника"
        forward_text = update.message.text or update.message.caption or "[без текста]"
        messages[user].append((timestamp, f"[репост из {forward_origin}]: {forward_text}", chat_id, message_id))
        logger.info(f"Forwarded message from {user} from {forward_origin}: {forward_text}")
    else:
        if update.message.text:
            messages[user].append((timestamp, update.message.text, chat_id, message_id))
            logger.info(f"Text message from {user}: {update.message.text}")
        elif update.message.photo:
            caption = update.message.caption
            if caption:
                messages[user].append((timestamp, caption, chat_id, message_id))
                logger.info(f"Photo with caption from {user}: {caption}")
            else:
                messages[user].append((timestamp, "[изображение]", chat_id, message_id))
                logger.info(f"Photo without caption from {user}")

# Очистка старых сообщений (старше 3 часов)
def cleanup_old_messages():
    now = get_utc_now()
    for user in list(messages.keys()):
        messages[user] = [(ts, msg, chat_id, msg_id) for ts, msg, chat_id, msg_id in messages[user] 
                          if (now - ts).total_seconds() <= 10800]  # 10800 секунд = 3 часа
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

    for timestamp, msg, chat_id, msg_id in user_messages:
        if prev_timestamp is None:
            current_thought.append((msg, chat_id, msg_id))
        else:
            time_diff = (timestamp - prev_timestamp).total_seconds()
            if time_diff > THOUGHT_GAP_SECONDS:
                thoughts.append(current_thought)
                current_thought = [(msg, chat_id, msg_id)]
            else:
                current_thought.append((msg, chat_id, msg_id))
        prev_timestamp = timestamp
    
    if current_thought:
        thoughts.append(current_thought)
    
    total_messages = sum(len(t) for t in thoughts)
    logger.info(f"Split messages into {len(thoughts)} thoughts with {total_messages} total messages.")
    return thoughts

# Генерация саммари с помощью Gemini 1.5 Flash
def generate_summary():
    logger.info("Generating summary...")
    cleanup_old_messages()
    
    if not messages:
        logger.info("No messages in the last 3 hours.")
        return "За последние 3 часа сообщений не было."
    
    final_summary = ""
    for user, user_msgs in messages.items():
        thoughts = split_into_thoughts(user_msgs)
        if not thoughts:
            continue
        
        # Формируем текст для анализа
        prompt = "Ты — помощник, который составляет подробное саммари обсуждений за последние 3 часа.\n"
        prompt += "Я дам тебе сообщения одного пользователя, разделённые на \"мысли\" (если между сообщениями был перерыв более 1 минуты).\n"
        prompt += "Твоя задача — проанализировать все сообщения за 3 часа и описать их суть подробно, не повторяя текст дословно.\n"
        prompt += "- Если сообщение — это репост (отмечено как \"[репост из <источник>]: <текст>\"), укажи, что пользователь переслал сообщение, и кратко опиши суть текста репоста, но не приписывай авторство текста этому пользователю.\n"
        prompt += "- Если сообщение выглядит как новость (заголовок или описание события), удели больше внимания её анализу: опиши суть, детали и контекст.\n"
        prompt += "- Если в \"мысли\" есть только \"[изображение]\", укажи это как \"пользователь отправлял смешные картинки\".\n"
        prompt += "Игнорируй сообщения длиной от 1 до 15 символов.\n"
        prompt += "Возвращай чистый текст без форматирования (без *, _, или других символов Markdown), ссылки добавлю позже.\n"
        prompt += "Обязательно обработай все мысли и сообщения, представленные ниже.\n\n"
        prompt += f"Вот сообщения пользователя {user}, разделённые на \"мысли\":\n"
        for i, thought in enumerate(thoughts, 1):
            thought_text = "\n".join(msg for msg, _, _ in thought)
            if len(thought_text) > MAX_TEXT_LENGTH_PER_THOUGHT:
                thought_text = thought_text[:MAX_TEXT_LENGTH_PER_THOUGHT] + "... [текст обрезан]"
            prompt += f"Мысль {i}:\n{thought_text}\n"
        prompt += "\nСформулируй подробный отчёт для этого пользователя. Для каждой \"мысли\" дай описание её смысла.\n"
        prompt += "Разделяй мысли горизонтальной линией \"---\". Не используй форматирование (например, * или _) в тексте.\n"
        prompt += "Убедись, что все мысли, указанные выше, учтены в отчёте."

        try:
            logger.info(f"Sending request to Gemini API for user {user} with {len(user_msgs)} messages in {len(thoughts)} thoughts...")
            response = model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.7,
                    max_output_tokens=2000
                )
            )
            user_summary = response.text.strip()
            logger.info(f"Received response from Gemini API for user {user}: {user_summary[:100]}...")  # Логируем начало ответа
            
            if not user_summary:
                logger.warning(f"Empty response from Gemini for user {user}.")
                user_summary = "Не удалось обработать сообщения."
            
            # Формируем итоговый текст с именем и ссылками в формате HTML
            final_summary += f"<b>{escape_html(user)}</b>:\n"
            thought_lines = user_summary.split("---")
            for i in range(len(thoughts)):
                if i < len(thought_lines) and thought_lines[i].strip():
                    thought_desc = thought_lines[i].strip()  # Чистый текст от Gemini
                else:
                    thought_desc = "\n".join(msg for msg, _, _ in thoughts[i])  # Запасной вариант
                    logger.warning(f"Using fallback description for thought {i+1} of user {user} due to mismatch.")
                
                # Формируем ссылки в формате HTML
                links = [f'<a href="https://t.me/c/{chat_id}/{msg_id}">link</a>' for _, chat_id, msg_id in thoughts[i]]
                link_text = ", ".join(links)
                final_summary += f"{thought_desc} {link_text}\n"
                if i < len(thoughts) - 1:
                    final_summary += "---\n"
            final_summary += "\n"
            
        except Exception as e:
            logger.error(f"Error in Gemini API request for user {user}: {e}")
            error_msg = f"Произошла ошибка: {str(e)}"
            return error_msg  # Экранирование не нужно для HTML
    
    if len(final_summary) > MAX_SUMMARY_LENGTH:
        final_summary = final_summary[:MAX_SUMMARY_LENGTH - 10] + " [обрезано]"
    
    return final_summary if final_summary.strip() else "За последние 3 часа сообщений не было."

# Команда /summary для запроса отчёта
async def summary_command(update: Update, context: CallbackContext):
    logger.info(f"Received update: {update.to_dict()}")
    if not update.message or not update.message.text:
        logger.info("Summary command received but no message object or text, skipping.")
        return
    
    user = update.message.from_user.username or update.message.from_user.first_name
    chat_id = update.message.chat_id
    bot_name = "@PsiSummaryBot"
    current_time = get_utc_now()
    
    logger.info(f"User {user} in chat {chat_id} triggered command with text: '{update.message.text}'")
    
    if str(chat_id) not in ALLOWED_CHAT_IDS:
        logger.info(f"Command from chat {chat_id} is not from allowed chats {ALLOWED_CHAT_IDS}, skipping.")
        await update.message.reply_text("Этот бот работает только в определённых чатах.")
        return
    
    if update.message.text.strip() not in ("/summary", f"/summary{bot_name}"):
        logger.info(f"Message '{update.message.text}' does not match '/summary' or '/summary{bot_name}', skipping.")
        return
    
    user_data = summary_calls[user]
    if user_data['last_time'] and (current_time - user_data['last_time']).total_seconds() < 10:
        user_data['count'] += 1
        if user_data['count'] > 3:
            logger.info(f"User {user} detected as spamming /summary.")
            await update.message.reply_text("мамку свою заспамь Костя")
            return
    else:
        user_data['count'] = 1
    
    user_data['last_time'] = current_time
    
    logger.info(f"Command matched: {update.message.text}")
    summary = generate_summary()
    
    try:
        await update.message.reply_text(summary, parse_mode='HTML')  # Используем HTML вместо MarkdownV2
    except Exception as e:
        logger.error(f"Failed to send message with HTML: {e}")
        await update.message.reply_text(summary)  # Отправляем без форматирования в случае ошибки

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
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)
    logger.info("Starting bot...")
    app.run_polling()

if __name__ == "__main__":
    main()