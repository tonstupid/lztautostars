import asyncio
import json
import re
import os
import logging
import random
import argparse
import sys
from typing import Set, Optional, Dict, Any, List, Union

import aiohttp
from pyrogram import Client
from pyrogram.errors import FloodWait

class ColoredFormatter(logging.Formatter):
    COLORS = {
        "GREY": "\x1b[38;20m",
        "BLUE": "\x1b[34;20m",
        "GREEN": "\x1b[32;20m",
        "YELLOW": "\x1b[33;20m",
        "RED": "\x1b[31;20m",
        "BOLD_RED": "\x1b[31;1m",
        "RESET": "\x1b[0m",
    }

    def __init__(self, fmt: str):
        super().__init__()
        self.fmt = fmt
        self.FORMATS = {
            logging.DEBUG: self.COLORS["GREY"] + self.fmt + self.COLORS["RESET"],
            logging.INFO: self.COLORS["BLUE"] + "ℹ️  " + self.fmt + self.COLORS["RESET"],
            logging.WARNING: self.COLORS["YELLOW"] + "⚠️  " + self.fmt + self.COLORS["RESET"],
            logging.ERROR: self.COLORS["RED"] + "❌ " + self.fmt + self.COLORS["RESET"],
            logging.CRITICAL: self.COLORS["BOLD_RED"] + "🔥 " + self.fmt + self.COLORS["RESET"],
            "SUCCESS": self.COLORS["GREEN"] + "✅ " + self.fmt + self.COLORS["RESET"],
        }

    def format(self, record):
        log_fmt = self.FORMATS.get(getattr(record, "levelformat", record.levelno))
        formatter = logging.Formatter(log_fmt, "%Y-%m-%d %H:%M:%S")
        return formatter.format(record)

def setup_logger():
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(ColoredFormatter("%(message)s"))
        logger.addHandler(handler)

    def success(message, *args, **kws):
        logger._log(logging.INFO, message, args, extra={'levelformat': 'SUCCESS'}, **kws)
    
    logger.success = success
    return logger

logger = setup_logger()

class Config:
    def __init__(self, config_file: str = "config.json", cli_thread_id: Optional[str] = None, reset: bool = False):
        self.config_file = config_file
        self.cli_thread_id = cli_thread_id
        
        if reset or not self._config_exists_and_is_valid():
            self._run_interactive_setup()

        with open(self.config_file, 'r', encoding='utf-8') as f:
            self._config_data = json.load(f)
        logger.success("Конфигурация успешно загружена.")

    def _config_exists_and_is_valid(self) -> bool:
        if not os.path.exists(self.config_file):
            return False
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            return config.get("api_id") != "YOUR_API_ID"
        except (json.JSONDecodeError, KeyError):
            return False

    def _run_interactive_setup(self):
        logger.info("Запускаю мастер первоначальной настройки...")
        config = {
            "api_id": input("Введите ваш API ID: "),
            "api_hash": input("Введите ваш API Hash: "),
            "phone_number": input("Введите ваш номер телефона (в международном формате, +...): "),
            "lolz_token": input("Введите ваш Lolzteam API токен: "),
            "forum_thread_id": input("Введите ID темы на форуме для отслеживания: "),
            "stars_count": 3,
            "check_interval": 30,
            "api_delay": 5,
            "max_retries": 3,
            "processed_posts_file": "processed_posts.json",
            "enable_reply": True,
            "reply_templates": ["Готово! Отправил звезды. ⭐", "Выполнено.", "Сделал.", "+rep"],
        }
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        
        logger.success(f"Конфигурация сохранена в файл '{self.config_file}'.")
        logger.info("Теперь перезапустите скрипт для входа в аккаунт Telegram.")
        sys.exit()
    
    def __getattr__(self, name: str) -> Any:
        if name == "forum_thread_id" and self.cli_thread_id:
            return self.cli_thread_id
        return self._config_data.get(name)

class ProcessedPostsManager:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.processed_posts: Set[int] = self._load()

    def _load(self) -> Set[int]:
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                return set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _save(self):
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(list(self.processed_posts), f, indent=4)

    def is_processed(self, post_id: int) -> bool:
        return post_id in self.processed_posts

    def mark_processed(self, post_id: int):
        self.processed_posts.add(post_id)
        self._save()

class LolzAPI:
    def __init__(self, token: str):
        self.base_url = "https://prod-api.lolz.live"
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _request(self, method: str, endpoint: str, is_retry: bool = False, **kwargs) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}{endpoint}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, headers=self.headers, **kwargs) as response:
                    if response.status == 200:
                        return await response.json()
                    
                    error_text = await response.text()
                    
                    if response.status == 403 and "Необходимо подождать" in error_text and not is_retry:
                        logger.warning("Обнаружен флуд-контроль API. Ожидание 3 секунды перед повторной попыткой...")
                        await asyncio.sleep(3)
                        return await self._request(method, endpoint, is_retry=True, **kwargs)
                    
                    if response.status == 429:
                        logger.warning("Превышен лимит запросов к API, ожидание 10 секунд...")
                        await asyncio.sleep(10)
                    else:
                        logger.error(f"Ошибка API {response.status} для {url}: {error_text}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"Ошибка соединения с API: {e}")
            return None

    async def get_thread_posts(self, thread_id: Union[str, int]) -> List[Dict[str, Any]]:
        params = {"thread_id": thread_id, "page": 1, "order": "post_date_reverse"}
        data = await self._request("GET", "/posts", params=params)
        posts = data.get("posts", []) if data else []
        if posts:
            logger.info(f"Получено {len(posts)} постов из темы {thread_id}.")
        return posts

    async def create_comment(self, post_id: int, comment_body: str) -> bool:
        payload = {"comment_body": comment_body}
        logger.info(f"Публикую комментарий к посту {post_id}...")
        response = await self._request("POST", f"/posts/{post_id}/comments", json=payload)
        
        if response and response.get("comment"):
            logger.success(f"Комментарий к посту {post_id} успешно опубликован.")
            return True
        else:
            logger.error(f"Не удалось опубликовать комментарий к посту {post_id}.")
            return False

class TelegramLinkExtractor:
    @staticmethod
    def extract(text: str) -> List[str]:
        patterns = [
            r'https?://(?:www\.)?(?:t\.me|telegram\.me)/([a-zA-Z0-9_]+(?:/\d+)?)',
            r'\[MEDIA=telegram\]([a-zA-Z0-9_]+(?:/\d+)?)\[/MEDIA\]',
            r'data-telegram-post="([a-zA-Z0-9_]+/\d+)"'
        ]
        all_matches = {f"https://t.me/{match}" for p in patterns for match in re.findall(p, text, re.I)}
        return list(all_matches)

    @staticmethod
    def parse(link: str) -> Optional[tuple[str, int]]:
        match = re.search(r't\.me/([^/]+)/(\d+)', link)
        if match:
            return match.group(1), int(match.group(2))
        return None

class TelegramStarsBot:
    SESSION_NAME = "stars_bot_session"

    def __init__(self, config: Config):
        self.config = config
        self.lolz_api = LolzAPI(config.lolz_token)
        self.processed_manager = ProcessedPostsManager(config.processed_posts_file)
        self.client: Optional[Client] = None

    async def send_stars_reaction(self, channel: str, message_id: int) -> bool:
        if not hasattr(self.client, 'send_paid_reaction'):
            logger.error("Платные реакции недоступны. Отправка 'звезд' невозможна.")
            return False
        
        try:
            for attempt in range(self.config.max_retries):
                try:
                    await self.client.send_paid_reaction(f"@{channel}", message_id, self.config.stars_count)
                    logger.success(f"Отправлено {self.config.stars_count} звезд в @{channel}/{message_id}")
                    return True
                except FloodWait as e:
                    logger.warning(f"FloodWait: необходимо подождать {e.x + 2} секунд.")
                    await asyncio.sleep(e.x + 2)
                except Exception as e:
                    logger.error(f"Попытка {attempt + 1} отправки звезд не удалась: {e}")
                    if attempt < self.config.max_retries - 1:
                        await asyncio.sleep(3 * (attempt + 1))
            return False
        except Exception as e:
            logger.error(f"Критическая ошибка при отправке звезд: {e}")
            return False

    async def _process_single_post(self, post: Dict[str, Any]):
        post_id = post.get("post_id")
        if not post_id or self.processed_manager.is_processed(post_id):
            return

        logger.info(f"Найден новый пост для обработки: ID {post_id}")
        
        post_content = post.get('post_body_html') or post.get('post_body')
        if not post_content:
            logger.warning(f"У поста {post_id} отсутствует содержимое. Пропускаем.")
            self.processed_manager.mark_processed(post_id)
            return

        links = TelegramLinkExtractor.extract(post_content)
        if not links:
            logger.info(f"В посте {post_id} не найдено ссылок Telegram.")
            self.processed_manager.mark_processed(post_id)
            return

        successful_reactions = 0
        for link in links:
            parsed_link = TelegramLinkExtractor.parse(link)
            if parsed_link:
                channel, message_id = parsed_link
                if await self.send_stars_reaction(channel, message_id):
                    successful_reactions += 1
                await asyncio.sleep(1)
        
        if successful_reactions > 0 and self.config.enable_reply:
            await asyncio.sleep(self.config.api_delay)
            reply_message = random.choice(self.config.reply_templates)
            await self.lolz_api.create_comment(post_id, reply_message)

        logger.info(f"Пост {post_id} полностью обработан.")
        self.processed_manager.mark_processed(post_id)

    async def _main_loop(self):
        while True:
            try:
                logger.info(f"Проверка новых постов в теме {self.config.forum_thread_id}...")
                posts = await self.lolz_api.get_thread_posts(self.config.forum_thread_id)
                
                if posts:
                    for post in reversed(posts):
                        await self._process_single_post(post)
                else:
                    logger.info("Новых постов для обработки не найдено.")
                
                logger.info(f"Ожидание {self.config.check_interval} секунд...")
                await asyncio.sleep(self.config.check_interval)
            
            except KeyboardInterrupt:
                logger.info("Получен сигнал прерывания (Ctrl+C).")
                break
            except Exception as e:
                logger.exception(f"Критическая ошибка в главном цикле: {e}")
                await asyncio.sleep(self.config.check_interval)

    async def start(self):
        is_first_login = not os.path.exists(f"{self.SESSION_NAME}.session")
        
        if is_first_login:
            logger.info("Сессия Telegram не найдена. Запускаю процесс входа...")

        self.client = Client(self.SESSION_NAME, self.config.api_id, self.config.api_hash)
        
        try:
            await self.client.start()
            logger.success("Клиент Telegram успешно запущен.")

            if is_first_login:
                logger.success("Аккаунт Telegram успешно подключен.")
                logger.info("Пожалуйста, перезапустите скрипт для начала работы.")
                return

            logger.info("=" * 40)
            logger.info(f"Комментарии на форуме: {'ВКЛЮЧЕНЫ' if self.config.enable_reply else 'ВЫКЛЮЧЕНЫ'}")
            logger.info("Бот в работе. Для остановки нажмите Ctrl+C.")
            logger.info("=" * 40)
            await self._main_loop()

        finally:
            if self.client and self.client.is_connected:
                await self.client.stop()
            logger.info("Бот остановлен.")

def main():
    parser = argparse.ArgumentParser(description='Telegram Stars Bot для Lolzteam')
    parser.add_argument('--thread-id', type=str, help='ID темы форума (переопределяет config.json)')
    parser.add_argument('--reset-config', action='store_true', help='Запустить мастер настройки заново')
    args = parser.parse_args()

    try:
        config = Config(cli_thread_id=args.thread_id, reset=args.reset_config)
        bot = TelegramStarsBot(config)
        asyncio.run(bot.start())
    except (ValueError, TypeError) as e:
        logger.critical(f"Ошибка в конфигурации или при запуске: {e}")
    except Exception as e:
        logger.critical(f"Фатальная ошибка при запуске бота: {e}")

if __name__ == "__main__":
    main()
