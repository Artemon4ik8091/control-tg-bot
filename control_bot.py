import subprocess
import logging
import html
import asyncio
import re
import os
import random 
import string 
import json 
import io 
from uuid import uuid4 

from telegram import Update, InlineQueryResultArticle, InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton, InlineQueryResultAudio
from telegram.ext import Application, CommandHandler, ContextTypes, InlineQueryHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode

# Для Яндекс.Музыки
from yandex_music import ClientAsync
import aiohttp 

# Установка базовой конфигурации логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Замените 'YOUR_BOT_TOKEN' на ваш реальный токен бота Telegram
TELEGRAM_BOT_TOKEN = 'TELEGRAM_BOT_TOKEN'

# !!! ВАЖНО: Замените [YOUR_TELEGRAM_USER_ID_1] на ваш реальный Telegram User ID !!!
# Добавьте все User ID, которым разрешен доступ, в этот список.
ALLOWED_USER_IDS = [000000000] # Пример: замените на ваш реальный ID. Добавьте сюда свой ID!

# Токен Яндекс.Музыки
# ПОМЕНЯЙТЕ ЭТО НА ВАШ ТОКЕН ЯНДЕКС.МУЗЫКИ
# Инструкцию по получению токена можно найти здесь:
# https://github.com/MarshalX/yandex-music-api/discussions/513#discussioncomment-2729781
YANDEX_MUSIC_TOKEN = "YANDEX_MUSIC_TOKEN"

# Максимальная полезная длина сообщения в Telegram для MarkdownV2
MAX_MESSAGE_LENGTH = 3800

# Директория для сохранения файлов, полученных через Telegram
TELEGRAM_FILES_DIR = os.path.expanduser("TELEGRAM_FILES_DIR")

# Словарь для хранения ожидающих подтверждения команд (shutdown или reboot)
pending_confirmation = {}

# Глобальный клиент Yandex.Music
ym_client: ClientAsync = None

# Функция для экранирования символов для MarkdownV2
def escape_markdown_v2(text: str) -> str:
    """Экранирует символы, специальные для MarkdownV2."""
    if text is None:
        return ""
    # Экранируем все символы, которые могут быть восприняты как MarkdownV2
    return re.sub(r'([_*[\]()~`>#+\-=|{}.!\\])', r'\\\1', text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет приветственное сообщение при получении команды /start."""
    user_id = update.effective_user.id
    logger.info(f"Получена команда /start от пользователя {user_id}")
    await update.message.reply_text('Привет! Я бот для выполнения команд терминала и получения статуса. Используйте /terminal [команда] или /t [команда] для выполнения команд, /status для информации о системе, или @вашБотЮзернейм t [команда] для inline-режима.\n\n'
                                    f'Вы можете отправлять мне файлы, и они будут сохранены в `{escape_markdown_v2(TELEGRAM_FILES_DIR)}`\\.\n' 
                                    f'Для отправки файла с сервера используйте `/send [путь до файла]`\\.\n\n'
                                    f'Теперь я также могу показывать, что играет в Яндекс.Музыке! Используйте `/ymnow`\\, `/ylyrics`\\, `/ylike`\\, `/ydislike`\\.', 
                                    parse_mode=ParseMode.MARKDOWN_V2)

async def check_access(user_id: int, update: Update = None) -> bool:
    """Вспомогательная функция для проверки прав доступа пользователя. 
    Принимает user_id напрямую, что удобно для inline-режима."""
    if user_id not in ALLOWED_USER_IDS:
        logger.warning(f"Пользователь {user_id} попытался получить доступ к привилегированной команде.")
        if update: 
            await update.message.reply_text("У вас нет прав доступа к этой команде.")
        return False
    return True

async def execute_command_logic(command_string: str, user_id: int) -> str:
    """
    Выполняет команду и возвращает её вывод.
    Это центральная логика для выполнения команды.
    Эта функция НЕ должна вызываться для 'reboot' или 'shutdown' (основные проверки будут выше).
    """
    if not command_string:
        return "Пожалуйста, укажите команду для выполнения. Например: ls -la"

    cmd_lower_stripped = command_string.strip().lower()
    if cmd_lower_stripped.startswith("reboot") or cmd_lower_stripped.startswith("shutdown"):
        logger.warning(f"Команда '{command_string}' (reboot/shutdown) проскочила в execute_command_logic для пользователя {user_id}. Этого не должно было произойти.")
        return "Внутренняя ошибка: команда reboot/shutdown не должна была быть передана на выполнение."

    logger.info(f"Выполнение команды '{command_string}' для пользователя {user_id}.")

    try:
        command_parts = command_string.split() 
        
        result = subprocess.run(command_parts, capture_output=True, text=True, check=False)
        
        output = result.stdout
        error_output = result.stderr

        response_text = ""
        if output:
            response_text += f"{output}"
        if error_output:
            if response_text: 
                response_text += "\n" 
            response_text += f"Ошибка (stderr):\n{error_output}"
        
        if not response_text:
            response_text = "Команда выполнена, но не вернула никакого вывода или ошибок."
        
        return response_text

    except FileNotFoundError:
        logger.error(f"Команда '{command_parts[0]}' не найдена.")
        return f"Ошибка: Команда '{command_parts[0]}' не найдена на сервере. Убедитесь, что она установлена и доступна в PATH."
    except Exception as e:
        logger.error(f"Неизвестная ошибка при выполнении '{command_string}': {e}")
        return f"Произошла неизвестная ошибка при выполнении команды: {e}"

async def execute_terminal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик для обычных команд /terminal и /t.
    Включает логику подтверждения для shutdown и reboot.
    """
    user_id = update.effective_user.id
    if not await check_access(user_id, update):
        return

    full_command_string = ' '.join(context.args).strip()
    cmd_lower = full_command_string.lower()

    # --- Обработка команд reboot и shutdown с подтверждением ---
    confirmation_needed = False
    command_type = None

    if cmd_lower.startswith("reboot"):
        command_type = "reboot"
        confirmation_needed = True
        
    elif cmd_lower.startswith("shutdown"):
        command_type = "shutdown"
        confirmation_needed = True
    
    if confirmation_needed:
        if user_id in pending_confirmation and pending_confirmation[user_id] == command_type:
            # Если уже ждем подтверждения для этой команды от пользователя
            await update.message.reply_text(
                f"Вы уже запросили `{escape_markdown_v2(command_type)}` \\(`{escape_markdown_v2(full_command_string)}`\\)\\. "
                "Пожалуйста, нажмите на кнопку подтверждения или отмены\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        # Устанавливаем флаг ожидания подтверждения и тип команды
        pending_confirmation[user_id] = command_type 

        button_text_confirm = "Да, перезагрузить" if command_type == "reboot" else "Да, выключить"
        warning_text = "перезагрузке" if command_type == "reboot" else "выключению"

        keyboard = [
            [
                InlineKeyboardButton(button_text_confirm, callback_data=f"{command_type}_confirm_{user_id}"),
                InlineKeyboardButton("Отмена", callback_data=f"{command_type}_cancel_{user_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"Вы уверены, что хотите выполнить команду `{escape_markdown_v2(command_type)}` \\(`{escape_markdown_v2(full_command_string)}`\\)?\n"
            f"Это приведёт к {warning_text} системы\\.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    # --- Конец обработки команд reboot и shutdown ---
    
    # Для всех остальных команд
    raw_response_text = await execute_command_logic(full_command_string, user_id)
    
    header = f"Команда: `{escape_markdown_v2(full_command_string)}`\nВывод:\n"
    escaped_output_for_md = escape_markdown_v2(raw_response_text) 
    
    markdown_overhead = len(f"```shell\n\n```") 
    effective_chunk_length = MAX_MESSAGE_LENGTH - len(header) - markdown_overhead 
    if effective_chunk_length <= 0: 
        effective_chunk_length = MAX_MESSAGE_LENGTH - 50 

    total_parts = (len(escaped_output_for_md) + effective_chunk_length - 1) // effective_chunk_length
    part_num = 1 

    current_index = 0
    while current_index < len(escaped_output_for_md):
        chunk = escaped_output_for_md[current_index:current_index + effective_chunk_length]
        
        chunk_header_str = ""
        if total_parts > 1:
            chunk_header_str = f"Часть {part_num}/{total_parts}:\n"
            
        message_to_send = f"{header}{chunk_header_str}```shell\n{chunk}\n```"

        await update.message.reply_text(message_to_send, parse_mode=ParseMode.MARKDOWN_V2)
        part_num += 1
        current_index += effective_chunk_length

async def critical_command_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия кнопок подтверждения для shutdown и reboot."""
    query = update.callback_query
    user_id = query.from_user.id
    callback_data = query.data
    
    await query.answer()

    if not await check_access(user_id):
        await query.edit_message_text(escape_markdown_v2("У вас нет прав доступа к этой команде."), parse_mode=ParseMode.MARKDOWN_V2)
        return

    match = re.match(r"^(shutdown|reboot)_(confirm|cancel)_(\d+)$", callback_data)
    if not match:
        await query.edit_message_text(escape_markdown_v2("Неверный запрос подтверждения."), parse_mode=ParseMode.MARKDOWN_V2)
        return

    command_type = match.group(1)
    action = match.group(2)

    if user_id not in pending_confirmation or pending_confirmation[user_id] != command_type:
        await query.edit_message_text(escape_markdown_v2(f"Неожиданное действие. Возможно, запрос на `{command_type}` устарел или был отменён."), parse_mode=ParseMode.MARKDOWN_V2)
        return

    del pending_confirmation[user_id]

    if action == "confirm":
        logger.info(f"Пользователь {user_id} подтвердил команду {command_type}.")
        command_to_execute = []
        response_message = ""

        if command_type == "shutdown":
            command_to_execute = ['sudo', 'shutdown', '-h', 'now']
            response_message = "Подтверждено. Выполняю команду `shutdown -h now`..."
        elif command_type == "reboot":
            command_to_execute = ['sudo', 'reboot']
            response_message = "Подтверждено. Выполняю команду `reboot`..."

        await query.edit_message_text(escape_markdown_v2(response_message), parse_mode=ParseMode.MARKDOWN_V2)
        
        try:
            subprocess.run(command_to_execute, check=True)
            logger.info(f"Команда {' '.join(command_to_execute)} успешно отправлена.")
        except FileNotFoundError:
            await query.edit_message_text(
                escape_markdown_v2(f"Ошибка: Команда `{'` или `'.join(command_to_execute[:2])}` не найдена. "
                "Убедитесь, что sudo установлен и настроен, и команды доступны."),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            logger.error(f"Команда {' '.join(command_to_execute[:2])} не найдена при попытке выполнения.")
        except subprocess.CalledProcessError as e:
            await query.edit_message_text(escape_markdown_v2(f"Ошибка выполнения `{' '.join(command_to_execute)}`: {e.stderr.decode()}"), parse_mode=ParseMode.MARKDOWN_V2)
            logger.error(f"Ошибка выполнения {' '.join(command_to_execute)}: {e}")
        except Exception as e:
            await query.edit_message_text(escape_markdown_v2(f"Неизвестная ошибка при выполнении `{' '.join(command_to_execute)}`: {e}"), parse_mode=ParseMode.MARKDOWN_V2)
            logger.error(f"Неизвестная ошибка при выполнении {' '.join(command_to_execute)}: {e}")

async def get_system_status_message() -> str:
    """Собирает информацию о системе и возвращает отформатированную строку."""
    os_info = "Неизвестно"
    network_name = "Неизвестно"
    local_ip = "Неизвестно"

    try:
        os_release_result = subprocess.run(['lsb_release', '-ds'], capture_output=True, text=True, check=False)
        arch_result = subprocess.run(['uname', '-m'], capture_output=True, text=True, check=False)
        
        if os_release_result.returncode == 0:
            os_info = os_release_result.stdout.strip()
        if arch_result.returncode == 0:
            os_info += f" {arch_result.stdout.strip()}"
        
        try:
            iwgetid_result = subprocess.run(['iwgetid', '-r'], capture_output=True, text=True, check=False)
            if iwgetid_result.returncode == 0:
                network_name = iwgetid_result.stdout.strip()
            else:
                nmcli_result = subprocess.run(['nmcli', '-t', '-f', 'active,ssid', 'dev', 'wifi'], capture_output=True, text=True, check=False)
                if nmcli_result.returncode == 0:
                    for line in nmcli_result.stdout.splitlines():
                        if line.startswith('yes:'):
                            network_name = line[4:].strip()
                            break
        except FileNotFoundError:
            logger.warning("Команды iwgetid или nmcli не найдены для определения имени сети.")
            network_name = "Недоступно (установите iwgetid или nmcli)"


        ip_addr_result = subprocess.run(['ip', '-4', 'addr', 'show', 'wlan0'], capture_output=True, text=True, check=False)
        if ip_addr_result.returncode == 0:
            for line in ip_addr_result.stdout.splitlines():
                if 'inet ' in line:
                    ip_match = line.strip().split(' ')[1]
                    local_ip = ip_match.split('/')[0]
                    break
        else:
            logger.warning("Не удалось получить IP адрес для wlan0.")
            local_ip = "Недоступно (проверьте wlan0)"

    except Exception as e:
        logger.error(f"Ошибка при сборе системной информации: {e}")
        return "Не удалось собрать всю системную информацию."

    status_message = (
        "OrangePI 3 LTS запущен.\n"
        f"Операционная система: {os_info}\n"
        f"Подключено к следующей сети: `{html.escape(network_name)}`\n" 
        f"Локальный IP адрес: `{html.escape(local_ip)}`" 
    )
    return status_message

async def send_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет системный статус по команде /status."""
    user_id = update.effective_user.id
    if not await check_access(user_id, update):
        return
    
    logger.info(f"Получена команда /status от пользователя {user_id}.")
    status_text = await get_system_status_message()
    await update.message.reply_html(status_text) 

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает inline-запросы."""
    query_string = update.inline_query.query
    user_id = update.inline_query.from_user.id
    results = []

    if not await check_access(user_id):
        results.append(
            InlineQueryResultArticle(
                id='1',
                title="Отказано в доступе",
                input_message_content=InputTextMessageContent("У вас нет прав доступа к этой команде."),
                description="Вы не авторизованы для выполнения команд."
            )
        )
        await update.inline_query.answer(results, cache_time=5)
        return

    logger.info(f"Получен inline-запрос от пользователя {user_id}: '{query_string}'")

    # Нормализуем запрос
    query_lower = query_string.strip().lower()
    
    # --- ОБРАБОТКА КОМАНД ТЕРМИНАЛА ---
    if query_lower.startswith("t "):
        command_to_execute_inline = query_string.strip()[2:].strip()

        cmd_lower_inline = command_to_execute_inline.lower()
        if cmd_lower_inline.startswith("reboot") or cmd_lower_inline.startswith("shutdown"):
            results.append(
                InlineQueryResultArticle(
                    id='forbidden_command',
                    title=f"Команда '{cmd_lower_inline.split(' ')[0]}' запрещена в inline-режиме",
                    input_message_content=InputTextMessageContent(
                        escape_markdown_v2(f"Команда `{cmd_lower_inline.split(' ')[0]}` и её вариации запрещены в inline-режиме из соображений безопасности. Пожалуйста, используйте её в личном чате с ботом."),
                        parse_mode=ParseMode.MARKDOWN_V2
                    ),
                    description="Эта команда требует прямой авторизации."
                )
            )
            await update.inline_query.answer(results, cache_time=5)
            return

        if not command_to_execute_inline:
            results.append(
                InlineQueryResultArticle(
                    id='empty_command',
                    title="Укажите команду после 't'",
                    input_message_content=InputTextMessageContent(
                        escape_markdown_v2("Пожалуйста, укажите команду после `t ` (например, `t ls -la`)"), 
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                )
            )
            await update.inline_query.answer(results, cache_time=0)
            return

        await asyncio.sleep(1) # Задержка 1 секунда

        raw_command_output = await execute_command_logic(command_to_execute_inline, user_id)
        
        header_inline = f"Команда: `{escape_markdown_v2(command_to_execute_inline)}`\nВывод:\n"
        escaped_output_for_md_inline = escape_markdown_v2(raw_command_output)

        full_text_to_send_inline = f"{header_inline}```shell\n{escaped_output_for_md_inline}\n```"

        markdown_overhead_inline = len(f"```shell\n\n```")
        effective_inline_chunk_length = MAX_MESSAGE_LENGTH - len(header_inline) - markdown_overhead_inline
        if effective_inline_chunk_length <= 0:
            effective_inline_chunk_length = MAX_MESSAGE_LENGTH - 50 

        if len(escaped_output_for_md_inline) > effective_inline_chunk_length:
            results.append(
                InlineQueryResultArticle(
                    id='long_output',
                    title="Вывод слишком длинный",
                    input_message_content=InputTextMessageContent(
                        escape_markdown_v2(f"Вывод команды '{command_to_execute_inline}' слишком длинный для inline-сообщения. Пожалуйста, используйте команду /terminal {command_to_execute_inline} в личном чате с ботом."),
                        parse_mode=ParseMode.MARKDOWN_V2
                    ),
                    description="Результат превышает лимит. Используйте команду в личном чате."
                )
            )
        else:
            results.append(
                InlineQueryResultArticle(
                    id=str(uuid4()), 
                    title=escape_markdown_v2(f"Выполнить: {command_to_execute_inline}"), # Экранируем title
                    input_message_content=InputTextMessageContent(full_text_to_send_inline, parse_mode=ParseMode.MARKDOWN_V2),
                    description=escape_markdown_v2(f"Результат выполнения: {escaped_output_for_md_inline[:100]}...") # Экранируем description
                )
            )
    
    # --- ОБРАБОТКА КОМАНД ЯНДЕКС.МУЗЫКИ ---
    elif query_lower == "ymnow":
        track, error_code = await get_current_yandex_music_track()
        if error_code:
            title_text, desc_text = get_ym_error_messages(error_code)
            results.append(
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title=escape_markdown_v2(title_text), # Экранируем title
                    input_message_content=InputTextMessageContent(escape_markdown_v2(title_text), parse_mode=ParseMode.MARKDOWN_V2),
                    description=escape_markdown_v2(desc_text) # Экранируем description
                )
            )
        else:
            artists = ", ".join(track.artists_name())
            title = track.title
            if track.version:
                title += f" ({track.version})"
            
            duration_ms = track.duration_ms
            minutes = duration_ms // 1000 // 60
            seconds = duration_ms // 1000 % 60

            caption = (
                f"🎶 Сейчас играет: <b>{html.escape(artists)}</b> - "
                f"<b>{html.escape(title)}</b>\n"
                f"🕐 {minutes:02}:{seconds:02}"
            )
            
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("song.link", url=f"https://song.link/ya/{track.id}")]])
            
            # --- START: New logic for sending audio via inline query ---
            audio_url = None
            try:
                # Get download info for the track
                info = await ym_client.tracks_download_info(track.id, True)
                if info:
                    best_quality_link = None
                    for dl_info in info:
                        if dl_info.codec == 'mp3' and dl_info.bitrate_in_kbps == 320:
                            best_quality_link = dl_info.direct_link
                            break
                        if dl_info.direct_link and not best_quality_link: # Fallback to any direct link if 320kbps MP3 not found
                            best_quality_link = dl_info.direct_link
                    audio_url = best_quality_link
            except Exception as e:
                logger.error(f"Ошибка при получении ссылки на аудио для inline-запроса: {e}")
                # If an error occurs or no direct link, fallback to just text article
                pass # audio_url remains None

            if audio_url:
                results.append(
                    InlineQueryResultAudio(
                        id=str(uuid4()),
                        audio_url=audio_url,
                        title=f"{artists} - {title}", # Title of the audio, not MarkdownV2 escaped here directly for telegram API
                        performer=artists,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        audio_duration=track.duration_ms // 1000, # Duration in seconds
                        reply_markup=reply_markup
                    )
                )
            else:
                # Fallback to InlineQueryResultArticle if audio_url is not available
                results.append(
                    InlineQueryResultArticle(
                        id=str(uuid4()),
                        title=escape_markdown_v2(f"Сейчас играет: {artists} - {title} (нет аудио)"), # Indicate no audio
                        input_message_content=InputTextMessageContent(caption, parse_mode=ParseMode.HTML),
                        description=escape_markdown_v2(f"Длительность: {minutes:02}:{seconds:02} (аудио недоступно)"),
                        reply_markup=reply_markup
                    )
                )
            # --- END: New logic for sending audio via inline query ---

    elif query_lower == "ylyrics":
        track, error_code = await get_current_yandex_music_track()
        if error_code:
            title_text, desc_text = get_ym_error_messages(error_code)
            results.append(
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title=escape_markdown_v2(title_text), # Экранируем title
                    input_message_content=InputTextMessageContent(escape_markdown_v2(title_text), parse_mode=ParseMode.MARKDOWN_V2),
                    description=escape_markdown_v2(desc_text) # Экранируем description
                )
            )
        else:
            try:
                lyrics_obj = await ym_client.tracks_lyrics(track.id)
                async with aiohttp.ClientSession() as session:
                    async with session.get(lyrics_obj.download_url) as request:
                        lyrics_text = await request.text()
                
                # Обрезаем текст, если он слишком длинный для inline-description или input_message_content
                display_lyrics_text = lyrics_text
                if len(display_lyrics_text) > 200: # Для описания
                    display_lyrics_text = display_lyrics_text[:197] + "..."
                
                full_lyrics_input_content = f"📜 Текст песни:\n```\n{escape_markdown_v2(lyrics_text)}\n```"
                # Проверяем общую длину сообщения для InputTextMessageContent
                if len(full_lyrics_input_content) > MAX_MESSAGE_LENGTH:
                     full_lyrics_input_content = escape_markdown_v2("📜 Текст песни слишком длинный. Пожалуйста, используйте команду `/ylyrics` в личном чате.")


                results.append(
                    InlineQueryResultArticle(
                        id=str(uuid4()),
                        title=escape_markdown_v2(f"Текст для: {track.artists_name()[0]} - {track.title}"), # Экранируем title
                        input_message_content=InputTextMessageContent(full_lyrics_input_content, parse_mode=ParseMode.MARKDOWN_V2),
                        description=escape_markdown_v2(display_lyrics_text) # Экранируем description
                    )
                )
            except Exception:
                results.append(
                    InlineQueryResultArticle(
                        id=str(uuid4()),
                        title=escape_markdown_v2("🚫 У трека нет текста!"), # Экранируем title
                        input_message_content=InputTextMessageContent(escape_markdown_v2("У текущего трека нет текста."), parse_mode=ParseMode.MARKDOWN_V2),
                        description=escape_markdown_v2("Текст песни не найден.") # Экранируем description
                    )
                )

    elif query_lower == "ylike":
        track, error_code = await get_current_yandex_music_track()
        if error_code:
            title_text, desc_text = get_ym_error_messages(error_code)
            results.append(
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title=escape_markdown_v2(title_text), # Экранируем title
                    input_message_content=InputTextMessageContent(escape_markdown_v2(title_text), parse_mode=ParseMode.MARKDOWN_V2),
                    description=escape_markdown_v2(desc_text) # Экранируем description
                )
            )
        else:
            try:
                liked_tracks_info = await ym_client.users_likes_tracks()
                liked_tracks = await liked_tracks_info.fetch_tracks_async()
                
                if isinstance(liked_tracks, list) and any(t.id == track.id for t in liked_tracks):
                    results.append(InlineQueryResultArticle(
                        id=str(uuid4()),
                        title=escape_markdown_v2("🚫 Трек уже лайкнут!"), # Экранируем title
                        input_message_content=InputTextMessageContent(escape_markdown_v2("Текущий трек уже находится в ваших лайкнутых."), parse_mode=ParseMode.MARKDOWN_V2),
                        description=escape_markdown_v2(f"'{track.title}' уже лайкнут.") # Экранируем description
                    ))
                else:
                    await track.like_async()
                    results.append(InlineQueryResultArticle(
                        id=str(uuid4()),
                        title=escape_markdown_v2("❤️ Лайкнул трек!"), # Экранируем title
                        input_message_content=InputTextMessageContent(escape_markdown_v2(f"Лайкнул: {track.artists_name()[0]} - {track.title}"), parse_mode=ParseMode.MARKDOWN_V2), # Экранируем всю строку
                        description=escape_markdown_v2(f"Трек '{track.title}' добавлен в лайки.") # Экранируем description
                    ))
            except Exception as e:
                logger.error(f"Ошибка при лайке трека в inline: {e}")
                results.append(InlineQueryResultArticle(
                    id=str(uuid4()),
                    title=escape_markdown_v2("🚫 Ошибка лайка!"), # Экранируем title
                    input_message_content=InputTextMessageContent(escape_markdown_v2(f"Произошла ошибка при лайке трека: {e}"), parse_mode=ParseMode.MARKDOWN_V2),
                    description=escape_markdown_v2("Не удалось лайкнуть трек.") # Экранируем description
                ))

    elif query_lower == "ydislike":
        track, error_code = await get_current_yandex_music_track()
        if error_code:
            title_text, desc_text = get_ym_error_messages(error_code)
            results.append(
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title=escape_markdown_v2(title_text), # Экранируем title
                    input_message_content=InputTextMessageContent(escape_markdown_v2(title_text), parse_mode=ParseMode.MARKDOWN_V2),
                    description=escape_markdown_v2(desc_text) # Экранируем description
                )
            )
        else:
            try:
                liked_tracks_info = await ym_client.users_likes_tracks()
                liked_tracks = await liked_tracks_info.fetch_tracks_async()

                if isinstance(liked_tracks, list) and any(t.id == track.id for t in liked_tracks):
                    await track.dislike_async()
                    results.append(InlineQueryResultArticle(
                        id=str(uuid4()),
                        title=escape_markdown_v2("💔 Дизлайкнул трек!"), # Экранируем title
                        input_message_content=InputTextMessageContent(escape_markdown_v2(f"Дизлайкнул: {track.artists_name()[0]} - {track.title}"), parse_mode=ParseMode.MARKDOWN_V2), # Экранируем всю строку
                        description=escape_markdown_v2(f"Трек '{track.title}' удален из лайков.") # Экранируем description
                    ))
                else:
                    results.append(InlineQueryResultArticle(
                        id=str(uuid4()),
                        title=escape_markdown_v2("🚫 Трек не лайкнут!"), # Экранируем title
                        input_message_content=InputTextMessageContent(escape_markdown_v2("Текущий трек не находится в ваших лайкнутых, чтобы его дизлайкнуть."), parse_mode=ParseMode.MARKDOWN_V2),
                        description=escape_markdown_v2("Текущий трек не лайкнут.") # Экранируем description
                    ))
            except Exception as e:
                logger.error(f"Ошибка при дизлайке трека в inline: {e}")
                results.append(InlineQueryResultArticle(
                    id=str(uuid4()),
                    title=escape_markdown_v2("🚫 Ошибка дизлайка!"), # Экранируем title
                    input_message_content=InputTextMessageContent(escape_markdown_v2(f"Произошла ошибка при дизлайке трека: {e}"), parse_mode=ParseMode.MARKDOWN_V2),
                    description=escape_markdown_v2("Не удалось дизлайкнуть трек.") # Экранируем description
                ))
    else:
        # Если ни одна известная команда не найдена, предложите помощь
        results.append(
            InlineQueryResultArticle(
                id='help',
                title="Доступные инлайн команды:",
                input_message_content=InputTextMessageContent(
                    escape_markdown_v2("Используйте: \n`@вашБотЮзернейм t <команда>` (например, `t ls -la`)\n"
                                     "`@вашБотЮзернейм ymnow` (что сейчас играет)\n"
                                     "`@вашБотЮзернейм ylyrics` (текст песни)\n"
                                     "重點:\n"
                                     "`@вашБотЮзернейм ylike` (лайкнуть трек)\n"
                                     "`@вашБотЮзернейм ydislike` (дизлайкнуть трек)"), 
                    parse_mode=ParseMode.MARKDOWN_V2
                ),
                description=escape_markdown_v2("Помощь по инлайн-командам.") # Экранируем description
            )
        )

    await update.inline_query.answer(results, cache_time=0)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает загрузку файлов (документов и аудио) от пользователя."""
    user_id = update.effective_user.id
    if not await check_access(user_id, update):
        return

    # Определяем, что это - документ или аудиофайл
    file_obj = None
    if update.message.document:
        file_obj = update.message.document
        logger.info(f"Получен документ от пользователя {user_id}: {file_obj.file_name}")
    elif update.message.audio:
        file_obj = update.message.audio
        logger.info(f"Получен аудиофайл от пользователя {user_id}: {file_obj.file_name}")
    else:
        # Это сообщение не является ни документом, ни аудио (хотя фильтр в main() должен это исключить)
        await update.message.reply_text("Это не документ и не аудиофайл.")
        return

    # Извлекаем данные из file_obj. 
    # file_name берем, если оно есть, иначе формируем.
    file_id = file_obj.file_id
    
    if file_obj.file_name:
        file_name_to_save = file_obj.file_name
    else:
        extension = ""
        if hasattr(file_obj, 'mime_type') and file_obj.mime_type:
            extension = file_obj.mime_type.split('/')[-1]
            if extension == 'mpeg':
                extension = 'mp3' 
        
        if not extension or extension not in ['mp3', 'wav', 'ogg', 'flac', 'txt', 'pdf', 'zip', 'rar', 'tar', 'gz', 'bz2', '7z', 'jpg', 'jpeg', 'png', 'gif', 'mp4', 'avi', 'mkv']: 
            extension = 'bin' 
        
        file_name_to_save = f"{file_id}.{extension}"
        logger.warning(f"Имя файла не предоставлено для файла {file_id}. Генерируется имя: {file_name_to_save}")


    file_size = file_obj.file_size

    # Убедимся, что директория для сохранения файлов существует
    os.makedirs(TELEGRAM_FILES_DIR, exist_ok=True)

    file_path = os.path.join(TELEGRAM_FILES_DIR, file_name_to_save)

    try:
        # Получаем объект файла
        new_file = await context.bot.get_file(file_id)
        # Скачиваем файл
        await new_file.download_to_drive(file_path)

        logger.info(f"Файл '{file_name_to_save}' ({file_size} байт) успешно сохранен в '{file_path}' от пользователя {user_id}")
        await update.message.reply_text(
            f"Файл `{escape_markdown_v2(file_name_to_save)}` успешно сохранен в:\n"
            f"`{escape_markdown_v2(file_path)}`",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        logger.error(f"Ошибка при сохранении файла '{file_name_to_save}' от пользователя {user_id}: {e}")
        await update.message.reply_text(
            f"Произошла ошибка при сохранении файла `{escape_markdown_v2(file_name_to_save)}`: "
            f"`{escape_markdown_v2(str(e))}`",
            parse_mode=ParseMode.MARKDOWN_V2
        )

async def send_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет файл пользователю по указанному пути."""
    user_id = update.effective_user.id
    if not await check_access(user_id, update):
        return

    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите путь к файлу. Например: `/send /path/to/your/file.txt`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    file_path_relative = ' '.join(context.args).strip()
    if file_path_relative.startswith('~'):
        file_path = os.path.expanduser(file_path_relative)
    else:
        file_path = file_path_relative

    logger.info(f"Пользователь {user_id} запросил отправку файла: '{file_path}'")

    if not os.path.exists(file_path):
        await update.message.reply_text(f"Ошибка: Файл `{escape_markdown_v2(file_path)}` не найден.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    
    if not os.path.isfile(file_path):
        await update.message.reply_text(f"Ошибка: Путь `{escape_markdown_v2(file_path)}` не является файлом.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    try:
        # Определяем тип файла для отправки
        if file_path.lower().endswith(('.mp3', '.wav', '.ogg', '.flac')):
            await update.message.reply_audio(audio=open(file_path, 'rb'))
            logger.info(f"Аудиофайл '{file_path}' успешно отправлен пользователю {user_id}.")
            return 
        elif file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
            await update.message.reply_photo(photo=open(file_path, 'rb'))
            logger.info(f"Фото '{file_path}' успешно отправлено пользователю {user_id}.")
            return 
        elif file_path.lower().endswith(('.mp4', '.avi', '.mkv')):
            await update.message.reply_video(video=open(file_path, 'rb'))
            logger.info(f"Видео '{file_path}' успешно отправлено пользователю {user_id}.")
            return 
        
        # Если не определили как аудио/фото/видео, отправляем как общий документ
        await update.message.reply_document(document=open(file_path, 'rb'))
        logger.info(f"Файл '{file_path}' успешно отправлен пользователю {user_id}.")
    except Exception as e:
        logger.error(f"Ошибка при отправке файла '{file_path}' пользователю {user_id}: {e}")
        await update.message.reply_text(
            f"Произошла ошибка при отправке файла `{escape_markdown_v2(file_path)}`: "
            f"`{escape_markdown_v2(str(e))}`", 
            parse_mode=ParseMode.MARKDOWN_V2
        )

# --- НОВЫЕ ФУНКЦИИ ДЛЯ ЯНДЕКС.МУЗЫКИ ---

async def get_current_yandex_music_track():
    """Получает текущий играющий трек из Яндекс.Музыки с использованием Ynison API."""
    global ym_client
    if not ym_client:
        logger.error("Клиент Яндекс.Музыки не инициализирован.")
        return None, "error"
    
    if not YANDEX_MUSIC_TOKEN or YANDEX_MUSIC_TOKEN == "YOUR_YANDEX_MUSIC_TOKEN":
        logger.warning("Токен Яндекс.Музыки не указан.")
        return None, "no_token"

    token = YANDEX_MUSIC_TOKEN

    device_info = {
        "app_name": "Chrome",
        "type": 1,
    }

    ws_proto = {
        "Ynison-Device-Id": "".join(
            [random.choice(string.ascii_lowercase) for _ in range(16)]
        ),
        "Ynison-Device-Info": json.dumps(device_info),
    }

    timeout = aiohttp.ClientTimeout(total=15, connect=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Шаг 1: Получение redirect ticket
            try:
                async with session.ws_connect(
                    url="wss://ynison.music.yandex.ru/redirector.YnisonRedirectService/GetRedirectToYnison",
                    headers={
                        "Sec-WebSocket-Protocol": f"Bearer, v2, {json.dumps(ws_proto)}",
                        "Origin": "http://music.yandex.ru",
                        "Authorization": f"OAuth {token}",
                    },
                    timeout=10,
                ) as ws:
                    recv = await ws.receive()
                    data = json.loads(recv.data)

                if "redirect_ticket" not in data or "host" not in data:
                    logger.error(f"Ynison API: Invalid redirect response structure: {data}")
                    return None, "error"
            except asyncio.TimeoutError:
                logger.error("Ynison API: Timeout during redirect ticket acquisition.")
                return None, "error"
            except Exception as e:
                logger.error(f"Ynison API: Error during redirect ticket acquisition: {e}", exc_info=True)
                return None, "error"

            new_ws_proto = ws_proto.copy()
            new_ws_proto["Ynison-Redirect-Ticket"] = data["redirect_ticket"]

            to_send = {
                "update_full_state": {
                    "player_state": {
                        "player_queue": {
                            "current_playable_index": -1,
                            "entity_id": "",
                            "entity_type": "VARIOUS",
                            "playable_list": [],
                            "options": {"repeat_mode": "NONE"},
                            "entity_context": "BASED_ON_ENTITY_BY_DEFAULT",
                            "version": {
                                "device_id": ws_proto["Ynison-Device-Id"],
                                "version": 9021243204784341000,
                                "timestamp_ms": 0,
                            },
                            "from_optional": "",
                        },
                        "status": {
                            "duration_ms": 0,
                            "paused": True,
                            "playback_speed": 1,
                            "progress_ms": 0,
                            "version": {
                                "device_id": ws_proto["Ynison-Device-Id"],
                                "version": 8321822175199937000,
                                "timestamp_ms": 0,
                            },
                        },
                    },
                    "device": {
                        "capabilities": {
                            "can_be_player": True,
                            "can_be_remote_controller": False,
                            "volume_granularity": 16,
                        },
                        "info": {
                            "device_id": ws_proto["Ynison-Device-Id"],
                            "type": "WEB",
                            "title": "Chrome Browser",
                            "app_name": "Chrome",
                        },
                        "volume_info": {"volume": 0},
                        "is_shadow": True,
                    },
                    "is_currently_active": False,
                },
                "rid": str(uuid4()), # Генерируем уникальный RID
                "player_action_timestamp_ms": 0,
                "activity_interception_type": "DO_NOT_INTERCEPT_BY_DEFAULT",
            }
            
            # Шаг 2: Отправка состояния Ynison и получение текущего трека
            try:
                async with session.ws_connect(
                    url=f"wss://{data['host']}/ynison_state.YnisonStateService/PutYnisonState",
                    headers={
                        "Sec-WebSocket-Protocol": f"Bearer, v2, {json.dumps(new_ws_proto)}",
                        "Origin": "http://music.yandex.ru",
                        "Authorization": f"OAuth {token}",
                    },
                    timeout=10,
                    method="GET", # Важно: метод должен быть GET для WebSocket
                ) as ws:
                    await ws.send_str(json.dumps(to_send))
                    recv = await asyncio.wait_for(ws.receive(), timeout=10)
                    ynison = json.loads(recv.data)

                    # Проверяем, приостановлен ли плеер
                    is_paused = ynison.get("player_state", {}).get("status", {}).get("paused", True)
                    if is_paused:
                        logger.info("Ynison API: Плеер приостановлен.")
                        return None, "paused" # Новый код ошибки

                    track_index = ynison.get("player_state", {}).get("player_queue", {}).get("current_playable_index", -1)
                    if track_index == -1:
                        logger.info("Ynison API: Нет активного трека согласно Ynison API (current_playable_index = -1).")
                        return None, "no_track"
                    
                    playable_list = ynison["player_state"]["player_queue"]["playable_list"]
                    if not playable_list or track_index >= len(playable_list):
                        logger.info("Ynison API: Список воспроизведения пуст или индекс за пределами списка.")
                        return None, "no_track"

                    track_info_from_ynison = playable_list[track_index]
                    track_id = track_info_from_ynison.get("playable_id")

                    if not track_id:
                        logger.error(f"Ynison API: Не удалось получить track_id из ответа Ynison: {track_info_from_ynison}")
                        return None, "error"

                    # Используем существующий ym_client для получения полной информации о треке
                    track_details = await ym_client.tracks(track_id)
                    if not track_details or not track_details[0]:
                        logger.info(f"Ynison API: Не удалось получить полную информацию о треке с ID: {track_id}.")
                        return None, "no_track"

                    return track_details[0], None # Возвращаем объект трека и отсутствие ошибки
            except asyncio.TimeoutError:
                logger.error("Ynison API: Таймаут при обновлении состояния Ynison.")
                return None, "error"
            except Exception as e:
                logger.error(f"Ynison API: Ошибка при обновлении состояния Ynison или получении трека: {e}", exc_info=True)
                error_message = str(e).lower()
                # Уточненная проверка ошибок для "Моей волны" или специфических проблем API
                if any(err_msg in error_message for err_msg in ["can't recognize it", "not found", "invalid json", "websocket", "bad status", "failed to fetch"]):
                    logger.info("Ynison API: Возможно, проблема с 'Моей волной' или временный сбой API.")
                    return None, "my_wave"
                return None, "error"

    except aiohttp.ClientConnectorError as e:
        logger.error(f"Ynison API: Сетевая ошибка (ClientConnectorError): {e}")
        return None, "network_error" # Новый код ошибки для сетевых проблем
    except Exception as e:
        logger.error(f"Ynison API: Произошла непредвиденная ошибка: {e}", exc_info=True)
        return None, "error"

def get_ym_error_messages(error_code: str) -> tuple[str, str]:
    """Возвращает заголовок и описание для ошибок Яндекс.Музыки."""
    if error_code == "no_token":
        return "🚫 Токен не указан!", "Укажите токен Яндекс.Музыки в коде бота."
    elif error_code == "no_track":
        return "☹️ Сейчас ничего не играет.", "Нет активного трека."
    elif error_code == "paused":
        return "⏸️ Воспроизведение приостановлено.", "Плеер Яндекс.Музыки в паузе."
    elif error_code == "my_wave":
        return "🤭 Проблема с Моей Волной!", "Не могу распознать трек или временный сбой API."
    elif error_code == "network_error":
        return "🌐 Ошибка сети!", "Проверьте интернет-соединение с Яндекс.Музыкой."
    else:
        return "🚫 Произошла ошибка!", "Пожалуйста, попробуйте позже."


async def ymnow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Получает и отправляет информацию о текущем треке Яндекс.Музыки."""
    user_id = update.effective_user.id
    if not await check_access(user_id, update):
        return

    logger.info(f"Получена команда /ymnow от пользователя {user_id}.")

    if not YANDEX_MUSIC_TOKEN or YANDEX_MUSIC_TOKEN == "YOUR_YANDEX_MUSIC_TOKEN":
        await update.message.reply_text("<b>🚫 Укажите токен Яндекс.Музыки в коде бота!</b>", parse_mode=ParseMode.HTML)
        return

    track, error_code = await get_current_yandex_music_track()

    if error_code:
        title_text, _ = get_ym_error_messages(error_code)
        await update.message.reply_text(f"<b>{title_text}</b>", parse_mode=ParseMode.HTML)
        return

    artists = ", ".join(track.artists_name())
    title = track.title
    if track.version:
        title += f" ({track.version})"
    
    duration_ms = track.duration_ms
    minutes = duration_ms // 1000 // 60
    seconds = duration_ms // 1000 % 60
    
    caption = (
        f"<b>🎶 Сейчас играет: </b>"
        f"<code>{html.escape(artists)}</code><b> - </b>"
        f"<code>{html.escape(title)}</code>\n"
        f"<b>🕐 {minutes:02}:{seconds:02}</b>"
    )

    try:
        # Попытка получить ссылку для скачивания
        info = await ym_client.tracks_download_info(track.id, True)
        direct_link = None
        if info:
            best_quality_link = None
            for dl_info in info:
                if dl_info.codec == 'mp3' and dl_info.bitrate_in_kbps == 320:
                    best_quality_link = dl_info.direct_link
                    break
                if dl_info.direct_link and not best_quality_link:
                    best_quality_link = dl_info.direct_link
            direct_link = best_quality_link
        
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("song.link", url=f"https://song.link/ya/{track.id}")]])

        if direct_link:
            audio_file_data = None
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(direct_link) as resp:
                        resp.raise_for_status() 
                        audio_file_data = io.BytesIO(await resp.read())
                logger.info(f"Аудиофайл '{title}' успешно скачан для отправки.")
                await update.message.reply_audio(
                    audio=audio_file_data, 
                    title=title,
                    performer=artists,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
                logger.info(f"Информация о треке '{title}' с аудио отправлена пользователю {user_id}.")
            except Exception as download_error:
                logger.error(f"Ошибка при скачивании или отправке аудиофайла из Яндекс.Музыки: {download_error}")
                await update.message.reply_text(
                    f"<b>🚫 Ошибка при отправке аудио (не удалось скачать или отправить файл): </b>"
                    f"<code>{html.escape(str(download_error))}</code>\n\n"
                    f"{caption}", 
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
        else:
            await update.message.reply_text(
                caption, 
                parse_mode=ParseMode.HTML, 
                reply_markup=reply_markup
            )
            logger.info(f"Информация о треке '{title}' (без аудио) отправлена пользователю {user_id}.")

    except Exception as e:
        logger.error(f"Ошибка при отправке информации о треке Яндекс.Музыки: {e}")
        await update.message.reply_text(
            f"<b>🚫 Произошла ошибка при отправке информации о треке: </b>"
            f"<code>{html.escape(str(e))}</code>", 
            parse_mode=ParseMode.HTML
        )

async def ylyrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Получает и отправляет текст текущего трека Яндекс.Музыки."""
    user_id = update.effective_user.id
    if not await check_access(user_id, update):
        return

    logger.info(f"Получена команда /ylyrics от пользователя {user_id}.")

    if not YANDEX_MUSIC_TOKEN or YANDEX_MUSIC_TOKEN == "YOUR_YANDEX_MUSIC_TOKEN":
        await update.message.reply_text("<b>🚫 Укажите токен Яндекс.Музыки в коде бота!</b>", parse_mode=ParseMode.HTML)
        return
    
    track, error_code = await get_current_yandex_music_track()

    if error_code:
        title_text, _ = get_ym_error_messages(error_code)
        await update.message.reply_text(f"<b>{title_text}</b>", parse_mode=ParseMode.HTML)
        return

    try:
        lyrics_obj = await ym_client.tracks_lyrics(track.id)
        async with aiohttp.ClientSession() as session:
            async with session.get(lyrics_obj.download_url) as request:
                lyrics_text = await request.text()
        
        reply_text = f"<b>📜 Текст песни: \n{html.escape(lyrics_text)}</b>"
        await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)
        logger.info(f"Текст песни для '{track.title}' отправлен пользователю {user_id}.")
    except Exception:
        await update.message.reply_text("<b>🚫 У трека нет текста!</b>", parse_mode=ParseMode.HTML)
        logger.warning(f"Текст песни для '{track.title}' не найден или произошла ошибка при получении.")

async def ylike_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Лайкает текущий играющий трек Яндекс.Музыки."""
    user_id = update.effective_user.id
    if not await check_access(user_id, update):
        return

    logger.info(f"Получена команда /ylike от пользователя {user_id}.")

    if not YANDEX_MUSIC_TOKEN or YANDEX_MUSIC_TOKEN == "YOUR_YANDEX_MUSIC_TOKEN":
        await update.message.reply_text("<b>🚫 Укажите токен Яндекс.Музыки в коде бота!</b>", parse_mode=ParseMode.HTML)
        return
    
    track, error_code = await get_current_yandex_music_track()

    if error_code:
        title_text, _ = get_ym_error_messages(error_code)
        await update.message.reply_text(f"<b>{title_text}</b>", parse_mode=ParseMode.HTML)
        return

    try:
        liked_tracks_info = await ym_client.users_likes_tracks()
        liked_tracks = await liked_tracks_info.fetch_tracks_async()

        if isinstance(liked_tracks, list) and any(t.id == track.id for t in liked_tracks):
            await update.message.reply_text("<b>🚫 Текущий трек уже лайкнут!</b>", parse_mode=ParseMode.HTML)
        else:
            await track.like_async()
            await update.message.reply_text("<b>❤️ Лайкнул текущий трек!</b>", parse_mode=ParseMode.HTML)
            logger.info(f"Трек '{track.title}' лайкнут пользователем {user_id}.")
    except Exception as e:
        logger.error(f"Ошибка при лайке трека '{track.title}': {e}")
        await update.message.reply_text(
            f"<b>🚫 Произошла ошибка при лайке трека: </b>"
            f"<code>{html.escape(str(e))}</code>", 
            parse_mode=ParseMode.HTML
        )

async def ydislike_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Дизлайкает текущий играющий трек Яндекс.Музыки."""
    user_id = update.effective_user.id
    if not await check_access(user_id, update):
        return

    logger.info(f"Получена команда /ydislike от пользователя {user_id}.")

    if not YANDEX_MUSIC_TOKEN or YANDEX_MUSIC_TOKEN == "YOUR_YANDEX_MUSIC_TOKEN":
        await update.message.reply_text("<b>🚫 Укажите токен Яндекс.Музыки в коде бота!</b>", parse_mode=ParseMode.HTML)
        return
    
    track, error_code = await get_current_yandex_music_track()

    if error_code:
        title_text, _ = get_ym_error_messages(error_code)
        await update.message.reply_text(f"<b>{title_text}</b>", parse_mode=ParseMode.HTML)
        return

    try:
        liked_tracks_info = await ym_client.users_likes_tracks()
        liked_tracks = await liked_tracks_info.fetch_tracks_async()

        if isinstance(liked_tracks, list) and any(t.id == track.id for t in liked_tracks):
            await track.dislike_async()
            await update.message.reply_text("<b>💔 Дизлайкнул текущий трек!</b>", parse_mode=ParseMode.HTML)
            logger.info(f"Трек '{track.title}' дизлайкнут пользователем {user_id}.")
        else:
            await update.message.reply_text("<b>🚫 Текущий трек не лайкнут!</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка при дизлайке трека '{track.title}': {e}")
        await update.message.reply_text(
            f"<b>🚫 Произошла ошибка при дизлайке трека: </b>"
            f"<code>{html.escape(str(e))}</code>", 
            parse_mode=ParseMode.HTML
        )

# --- КОНЕЦ НОВЫХ ФУНКЦИЙ ДЛЯ ЯНДЕКС.МУЗЫКИ ---

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to the user/admin."""
    logger.error("Произошло исключение:", exc_info=context.error)

    error_message = f"Произошла ошибка: `{escape_markdown_v2(str(context.error))}`\n"
    
    if update and update.effective_message:
        if update.effective_message.text:
            error_message += f"Сообщение: `{escape_markdown_v2(update.effective_message.text)}`\n"
        else:
            error_message += f"Сообщение: `(без текстового содержимого)`\n"
        error_message += f"Пользователь: `{update.effective_user.id}`"
        
    if ALLOWED_USER_IDS:
        admin_id = ALLOWED_USER_IDS[0] 
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"Бот обнаружил ошибку\\!\n\n```shell\n{error_message}\n```", 
                parse_mode=ParseMode.MARKDOWN_V2
            )
            logger.info(f"Сообщение об ошибке отправлено администратору {admin_id}")
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение об ошибке администратору {admin_id}: {e}")

async def post_init(application: Application) -> None:
    """Функция, которая вызывается после инициализации бота и перед началом опроса."""
    global ym_client
    logger.info("Бот запущен. Отправляем статус всем разрешенным пользователям.")
    
    status_message = await get_system_status_message()
    
    # Инициализация клиента Яндекс.Музыки здесь
    if YANDEX_MUSIC_TOKEN and YANDEX_MUSIC_TOKEN != "YOUR_YANDEX_MUSIC_TOKEN":
        try:
            ym_client = ClientAsync(YANDEX_MUSIC_TOKEN)
            await ym_client.init()
            logger.info("Клиент Яндекс.Музыки успешно инициализирован.")
        except Exception as e:
            logger.error(f"Ошибка при инициализации клиента Яндекс.Музыки: {e}")
            status_message += f"\n\n⚠ Ошибка инициализации Яндекс.Музыки: `{html.escape(str(e))}`"
            ym_client = None
    else:
        status_message += "\n\n⚠ Токен Яндекс.Музыки не указан. Функции Яндекс.Музыки не будут работать."
        ym_client = None


    for user_id in ALLOWED_USER_IDS: 
        try:
            await application.bot.send_message(chat_id=user_id, text=status_message, parse_mode='HTML')
            logger.info(f"Статус отправлен пользователю {user_id}")
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение о статусе пользователю {user_id}: {e}")
            logger.warning("Убедитесь, что пользователь запустил бота (отправил /start) хотя бы один раз.")


def main() -> None:
    """Запускает бота."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler(["terminal", "t"], execute_terminal_command))
    application.add_handler(CommandHandler("status", send_status))
    application.add_handler(CommandHandler("send", send_file_command)) 
    
    # НОВЫЕ ОБРАБОТЧИКИ ДЛЯ ЯНДЕКС.МУЗЫКИ
    application.add_handler(CommandHandler("ymnow", ymnow_command))
    application.add_handler(CommandHandler("ylyrics", ylyrics_command))
    application.add_handler(CommandHandler("ylike", ylike_command))
    application.add_handler(CommandHandler("ydislike", ydislike_command))
    
    application.add_handler(InlineQueryHandler(inline_query))
    application.add_handler(CallbackQueryHandler(critical_command_confirmation_callback, pattern=r"^(shutdown|reboot)_(confirm|cancel)_\d+$")) 
    
    application.add_handler(MessageHandler(filters.Document.ALL | filters.AUDIO & ~filters.COMMAND, handle_document)) 
    
    application.add_error_handler(error_handler)

    logger.info("Бот запущен и ожидает команд...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
