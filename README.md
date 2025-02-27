# PsiSummaryBot
Telegram бот для создания подробного саммари сообщений в чате за последние 3 часа.

## Установка
1. Установите зависимости:

pip install python-telegram-bot google-generativeai

2. Заполните `GOOGLE_API_KEY`, `TOKEN` и `ALLOWED_CHAT_IDS` в `PsiSummary.py`.
3. Запустите:

python PsiSummaryBot.py

## Использование
- Добавьте бота в чат.
- Предоставьте боту доступ к истории чата и дайте права админа.
- Вызовите `/summary` или `/summary@PsiSummaryBot`.

